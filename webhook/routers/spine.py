"""Spine routes: frame-promote an inbox card; promote a transcript to RAG.

Split out of webhook/main.py (arch-phase-4, cp-engine #32).
Behavior-preserving: code moved verbatim; only import paths and
cross-module qualifications changed. Tests monkeypatch THIS module's
names (patching `main.<name>` re-exports has no effect on behavior).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

import git_ops
import observability
import pipeline
import signatures
from fastapi import APIRouter, HTTPException, Request, Response

from cp_engine.mc2_db import Tables

log = logging.getLogger("cp-engine-webhook")

router = APIRouter()


@router.post("/api/spine/promote")
async def spine_promote(request: Request) -> Response:
    """Frame + promote a proposed spine_inbox card (async kickoff, 202).

    The server-side equivalent of the `cp spine-frame` CLI: a human in the mc-2
    web UI clicks "Frame & promote" on a proposed inbox card with a directing
    framing brief. mc-2 has the Supabase DB but NOT a checkout of the cp tenant
    filesystem — only this service clones the tenant — so the markdown write
    (source of truth) + git push happen HERE. mc-2 calls this (signed) as a
    thin proxy.

    The promote is an LLM re-distillation plus a tenant clone + push — far too
    slow to hold an HTTP request open for. So this endpoint mirrors
    /api/spine/promote-transcript exactly: it validates synchronously
    (signature, payload, card, 409 double-submit guard, binding target),
    inserts a `running` spine_promote_runs row (kind='frame_promote',
    card_id=<card id>), returns 202 immediately, and runs the promote in a
    background task that updates the row to done/failed when it finishes.
    mc-2 polls GET /api/spine/promote-runs/{run_id}.

    Background sequence: resolve the target estimate item (name/phase/kind,
    best-effort) → clone tenant (sparse: scope dirs only) → promote_card
    (directed re-distillation under the framing → write/append a live
    `## v<N>` substance version, or CREATE a new authored element on source
    divergence, issue #44) → mirror that project's substance into
    spine_substance (best-effort) → commit + push → flip the card.

    Request body (JSON):
        {
          "card_id": "<project_code>/inbox/<source_ref>",  # required
          "framing": "the human's directing brief",        # required, non-empty
          "est_item_id": "<uuid>"|null,   # optional; defaults to card's guess
          "kind": "deliverable"|"activity"|null,  # optional; defaults to item kind
          "sources": ["..."],             # optional; defaults to [card.source_ref]
          "model": "claude-opus-4-7"      # optional; default pipeline.DEFAULT_MODEL
        }

    Headers:
        X-Webhook-Signature: hex(hmac_sha256(...))
        X-Webhook-Timestamp: (optional, per the phased-rollout gate)

    Response (202):
        {"run_id": "<code>/<uuid>", "status": "running"}

    On success the run row's `result` jsonb carries
        {"version_label", "rel_path", "mirrored", "created_new_element",
         "card_flipped"}.

    The card is flipped to 'promoted' AFTER a successful push (the push is the
    real commit point). If that final flip fails, the version is still durable
    in the repo and the run is recorded done with "card_flipped": false.

    409: the card is already 'promoted' — a duplicate submit (double click /
    retry of an already-landed promote). Re-frame the card to promote again.
    The 409 fires BEFORE any run row is inserted.
    """
    from uuid import uuid4

    from cp_engine.asset_ingest import _utc_now_iso
    from cp_engine.spine_inbox import load_card

    raw_body = await request.body()
    signatures._verify_signature(
        raw_body,
        request.headers.get("x-webhook-signature", ""),
        request.headers.get("x-webhook-timestamp", ""),
    )

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc

    card_id = (payload.get("card_id") or "").strip()
    framing = (payload.get("framing") or "").strip()
    if not card_id:
        raise HTTPException(status_code=400, detail="card_id is required")
    if not framing:
        raise HTTPException(status_code=400, detail="framing must be non-empty")

    est_item_id = payload.get("est_item_id")
    kind = payload.get("kind")
    sources = payload.get("sources") or []
    model = payload.get("model") or pipeline.DEFAULT_MODEL

    client = pipeline._create_supabase_client()
    if client is None:
        raise HTTPException(
            status_code=500, detail="Supabase not configured for spine promote"
        )

    card = load_card(client, card_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"no inbox card '{card_id}'")

    # Double-submit guard (issue #44): a card that already went through the
    # promote flow must not be silently re-promoted — the observed failure mode
    # was the same card promoted twice 17s apart, writing a near-duplicate
    # version. Re-framing the card (which resets its status) is the deliberate
    # path to promote again.
    if card.status == "promoted":
        raise HTTPException(
            status_code=409,
            detail=(
                f"card '{card_id}' is already promoted — refusing a duplicate "
                "promote; re-frame the card to promote it again"
            ),
        )

    # Resolve the target estimate item. Default to the card's guess; 400 if
    # there's still nothing to bind to (the binding key is mandatory).
    target_item_id = est_item_id or card.guessed_est_item_id
    if not target_item_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "no estimate item to bind to: pass est_item_id "
                "(the card carries no guess)"
            ),
        )

    # Everything from here on (estimate resolve, clone, LLM re-distill, push,
    # card flip) is the slow tail — record a running run row and hand off.
    run_id = f"{card.project_code}/{uuid4()}"
    _spine_promote_runs_table(client).insert({
        "id": run_id,
        "project_id": card.project_id,
        "project_code": card.project_code,
        "est_item_id": target_item_id,
        "kind": "frame_promote",
        "card_id": card.id,
        "status": "running",
        "started_at": _utc_now_iso(),
    }).execute()
    pipeline._spawn_background(_run_frame_promote(
        run_id, card,
        framing=framing,
        est_item_id=target_item_id,
        kind=kind,
        sources=list(sources),
        model=model,
    ))
    return Response(
        content=json.dumps({"run_id": run_id, "status": "running"}),
        status_code=202,
        media_type="application/json",
    )


def _frame_promote_in_tree(
    client, tenant_root, card, *,
    framing: str, est_item_id: str, kind: str | None,
    sources: list[str], model: str,
) -> dict:
    """The synchronous heavy body of a frame-promote run (runs in a thread).

    Behavior-preserving move of the pre-async /api/spine/promote tail:
    estimate resolve (best-effort) → promote_card → mirror (best-effort) →
    commit + push → card flip (best-effort, AFTER the push). Returns the
    `result` jsonb recorded on the spine_promote_runs row. Raises on the
    failures that used to 500 the sync route (promote_card, push) — the
    caller records those as a failed run.
    """
    from cp_engine.estimate import fetch_estimate
    from cp_engine.plan_from_transcript import _call_claude
    from cp_engine.spine import find_spine_dir
    from cp_engine.spine_inbox import _INBOX_TABLE, promote_card
    from cp_engine.spine_substance_sync import sync_spine_substance
    from cp_engine.substance import parse_substance

    # The estimate gives the item's name + phase for the file path + frontmatter.
    # Degrade gracefully (unbound-by-name) if the estimator is unreachable —
    # mirrors the CLI's spine_frame_cmd.
    name = phase = None
    resolved_kind = kind
    estimate = None
    try:
        estimate = fetch_estimate(client, card.project_id)
    except Exception as exc:  # noqa: BLE001 — estimator unreachable
        log.warning(
            "spine-promote: estimate fetch failed for %s — proceeding "
            "unbound-by-name: %s", card.project_code, exc,
        )
    if estimate is not None:
        item = estimate.item_by_id(est_item_id)
        if item is not None:
            name = item.name
            resolved_kind = kind or item.kind
            for ph in estimate.phases:
                if any(i.id == est_item_id for i in ph.items):
                    phase = ph.name
                    break
    resolved_kind = resolved_kind or "deliverable"

    config = pipeline._load_tenant_config(tenant_root)
    project_dir = find_spine_dir(config.root, card.project_code)
    src = list(sources) or [card.source_ref]

    # Directed re-distillation + markdown write (source of truth). Let any
    # failure raise — a failed promote must be visible (the run row flips to
    # 'failed' so the UI can show an error), and nothing was pushed yet.
    #
    # flip_card=False means promote_card does NOT flip the card to
    # 'promoted' here. The flip is deferred until AFTER a successful push
    # (below): the git push is the real commit point. If the push fails,
    # the throwaway clone (and its only copy of the markdown) is discarded
    # — but the card stays proposed/framed so the human can retry the
    # click. No data loss. The client IS passed: when the promote's sources
    # diverge from the bound card's (issue #44), promote_card creates a new
    # AUTHORED element, whose rows are MC-2-owned and written directly —
    # for that path the DB row, not the push, is the durable copy (the
    # authored reverse-mirror regenerates the file on any later sync).
    path = promote_card(
        card,
        framing=framing,
        est_item_id=est_item_id,
        kind=resolved_kind,
        project_dir=project_dir,
        sources=src,
        distiller=_call_claude,
        model=model,
        client=client,
        flip_card=False,
        name=name,
        phase=phase,
    )

    # Did the issue-#44 create path fire? promote_card's create-don't-version
    # branch mirrors the new authored element at spine/_authored/<slug>.md;
    # the version path always writes spine/<phase-slug>/<item-slug>.md. The
    # parent-dir name is the discriminator.
    created_new_element = path.parent.name == "_authored"

    # The live version's label, re-parsed off the just-written file.
    version_label = parse_substance(path).live_version().label
    rel_path = str(path.relative_to(tenant_root))

    # Mirror the new version into spine_substance so the UI sees it
    # immediately (idempotent reconcile of ALL of this project's substance,
    # exactly like `cp sync`). Best-effort: a mirror failure must NOT lose
    # the click — the markdown + git push still land and the next sync
    # reconciles. So we record mirrored=false and keep going.
    mirrored = True
    try:
        sync_spine_substance(
            client,
            project_id=card.project_id,
            project_code=card.project_code,
            project_dir=project_dir,
            estimate=estimate,
        )
    except Exception as exc:  # noqa: BLE001 — never lose a successful write
        log.warning(
            "spine-promote: spine_substance mirror failed for %s "
            "(markdown + push will still land): %s",
            card.project_code, exc,
        )
        observability.capture(exc, area="spine_promote_mirror")
        mirrored = False

    # The push IS the commit point — succeeding here means the version is
    # durably in the repo. Any failure raises before the card flips (the run
    # records 'failed'), so the human can safely retry the click (no data loss).
    commit_sha = git_ops._commit_and_push_promote(
        tenant_root=tenant_root,
        project_code=card.project_code,
        version_label=version_label,
        rel_path=rel_path,
    )

    # NOW that the push succeeded, flip the card to 'promoted' — the last,
    # cheapest, most-likely-to-succeed step. If THIS fails the durable work is
    # already done (version is in the repo); worst case the card shows
    # un-promoted and a retry re-distills a duplicate version (benign, and far
    # better than losing the write). So we log loudly but the run is STILL
    # recorded done, with card_flipped=false (mirrors the `mirrored` field).
    card_flipped = True
    try:
        client.table(_INBOX_TABLE).update({"status": "promoted"}).eq(
            "id", card.id
        ).execute()
    except Exception as exc:  # noqa: BLE001 — push already landed; never fail here
        log.error(
            "spine-promote: card flip to 'promoted' FAILED for %s after a "
            "successful push (%s) — version is durable in the repo; card shows "
            "un-promoted and a retry will re-distill a duplicate version: %s",
            card.id, commit_sha[:8], exc,
        )
        observability.capture(exc, area="spine_promote_card_flip")
        card_flipped = False

    log.info(
        "spine-promote: card=%s item=%s %s -> %s (mirrored=%s flipped=%s new=%s)",
        card.id, est_item_id, version_label, commit_sha[:8],
        mirrored, card_flipped, created_new_element,
    )
    return {
        "version_label": version_label,
        "rel_path": rel_path,
        "mirrored": mirrored,
        "created_new_element": created_new_element,
        "card_flipped": card_flipped,
    }


async def _run_frame_promote(
    run_id: str, card, *,
    framing: str, est_item_id: str, kind: str | None,
    sources: list[str], model: str,
) -> None:
    """Background: run the (sync, slow) frame-promote off the event loop, then
    record the outcome on the spine_promote_runs row. Never raises — a failure
    is recorded as status=failed (this is the fire-and-forget tail of
    /api/spine/promote, which has already returned 202 to the caller). Mirrors
    `_run_promote` (the promote-transcript runner) exactly.

    The clone is SPARSE, scoped to the engine's scope dirs (`_SCOPE_DIRS`:
    1p/, firstpersonsf/, canonic/) — provably everything this run touches:
    `_load_tenant_config` reads root-level .cp-engine.toml (cone mode always
    materializes root files), `find_spine_dir` walks exactly `_SCOPE_DIRS`,
    promote_card writes only under the resolved project dir (inside a scope
    dir), the mirror reads only that project dir, and the commit's
    `git add -A` stages only checked-out paths (sparse-excluded entries pass
    through via skip-worktree). sprints/, docs/, meetings/ archives etc. are
    never materialized or blob-fetched.
    """
    from cp_engine.asset_ingest import _utc_now_iso
    from cp_engine.sync import _SCOPE_DIRS

    client = pipeline._create_supabase_client()
    try:
        # The clone exists only for the duration of the threaded promote; the
        # `with` guarantees cleanup (rmtree) on exit, success or failure.
        with git_ops._cloned_tenant(sparse_paths=list(_SCOPE_DIRS)) as tenant_root:
            result = await asyncio.to_thread(
                _frame_promote_in_tree,
                client,
                tenant_root,
                card,
                framing=framing,
                est_item_id=est_item_id,
                kind=kind,
                sources=sources,
                model=model,
            )
        _spine_promote_runs_table(client).update({
            "status": "done",
            "result": result,
            "finished_at": _utc_now_iso(),
        }).eq("id", run_id).execute()
    except Exception as exc:  # noqa: BLE001 — record the failure, never crash the task
        log.warning(
            "spine frame-promote run %s failed: %s", run_id, exc, exc_info=True
        )
        observability.capture(exc, area="spine_frame_promote_run")
        try:
            # Fresh client on the failure path: the original may be the thing
            # that failed (transient supabase/network error).
            _spine_promote_runs_table(pipeline._create_supabase_client()).update(
                {"status": "failed", "error": str(exc), "finished_at": _utc_now_iso()}
            ).eq("id", run_id).execute()
        except Exception:  # noqa: BLE001 — best effort; nothing else to do
            log.error("spine frame-promote run %s: could not record failure", run_id)


def _spine_promote_runs_table(client):
    return client.table(Tables.SPINE_PROMOTE_RUNS)


def _resolve_project_id_for_promote(client, code: str) -> str | None:
    """Resolve a project_code to its `projects.id` for the promote endpoint.

    Thin wrapper over cp_engine.mcp_server._resolve_project_id (a pure
    client+code → projects.id resolver: it does the same two-form code bridge
    the rest of the engine uses, with no MCP/stdio state). Wrapped here as a
    module-level name so tests can monkeypatch the resolve seam without
    reaching into mcp_server, mirroring how the asset-ingest tests stub
    resolve_project_folders.
    """
    from cp_engine.mcp_server import _resolve_project_id
    return _resolve_project_id(client, code)


async def _run_promote(
    run_id: str, code: str, project_id: str, company_id: str | None,
    element_row: dict,
) -> None:
    """Background: run the (sync, slow) transcript promotion off the event loop,
    then record the outcome on the spine_promote_runs row. Never raises — a
    failure is recorded as status=failed (this is the fire-and-forget tail of
    /api/spine/promote-transcript, which has already returned 202 to the caller).

    Unlike /api/assets/ingest (asset ingest pulls from Drive/Dropbox and never
    touches the tenant tree), promotion's source IS a verbatim transcript file
    committed in the tenant tree — the spine row's body is distilled memory, not
    the transcript, so there's no DB copy and a checkout is required. We drive
    the promotion through `git_ops._cloned_tenant()` (the shared clone-on-each-request
    contextmanager that always `rmtree`s in its `finally`), so the clone lives
    exactly as long as `promote_transcript` needs it and is then removed — no
    per-promote disk leak.

    Engagement-only carries through as a *failed run* (not a crash): an
    initiative has company_id=None, and promote_transcript's CONTRACT A returns
    {ok:false, reason:"initiative…"} without touching any file — which we record
    as status=failed with that reason, exactly like any other ok:false."""
    from cp_engine.asset_ingest import _utc_now_iso
    from cp_engine.spine_promote import promote_transcript

    client = pipeline._create_supabase_client()
    # Pass Supabase coords explicitly from the webhook ENV — same discipline as
    # _run_asset_ingest: the container cwd is /app, so the ingest pipeline's
    # lazy cwd-config cred resolution would die ('No .cp-engine.toml at /app').
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
    try:
        if not supabase_url or not supabase_key:
            raise RuntimeError(
                "webhook missing SUPABASE_URL/SUPABASE_SERVICE_KEY env"
            )
        # The clone exists only for the duration of the threaded promote; the
        # `with` guarantees cleanup (rmtree) on exit, success or failure.
        with git_ops._cloned_tenant() as tenant_root:
            result = await asyncio.to_thread(
                promote_transcript,
                client,
                tenant_root,
                code,
                project_id,
                company_id,
                element_row,
                supabase_url=supabase_url,
                supabase_key=supabase_key,
            )
        if result.get("ok"):
            ids = result.get("ids") or []
            patch = {
                "status": "done",
                "asset_id": ids[0] if ids else None,
                "finished_at": _utc_now_iso(),
            }
        else:
            patch = {
                "status": "failed",
                "error": result.get("reason"),
                "finished_at": _utc_now_iso(),
            }
        _spine_promote_runs_table(client).update(patch).eq("id", run_id).execute()
    except Exception as exc:  # noqa: BLE001 — record the failure, never crash the task
        log.warning("spine-promote run %s failed: %s", run_id, exc, exc_info=True)
        observability.capture(exc, area="spine_promote_run")
        try:
            # Fresh client on the failure path: the original may be the thing
            # that failed (transient supabase/network error).
            _spine_promote_runs_table(pipeline._create_supabase_client()).update(
                {"status": "failed", "error": str(exc), "finished_at": _utc_now_iso()}
            ).eq("id", run_id).execute()
        except Exception:  # noqa: BLE001 — best effort; nothing else to do
            log.error("spine-promote run %s: could not record failure", run_id)


@router.post("/api/spine/promote-transcript")
async def spine_promote_transcript(request: Request) -> Response:
    """Fire-and-forget promotion of a spine element's transcript into the RAG store.

    The mc-2 spine dashboard's "Promote transcript" click lands here (signed).
    Promotion embeds the element's underlying transcript into rag_assets so it's
    retrievable via pull_project_source. The embed takes seconds-to-minutes
    (Voyage + Supabase), so this endpoint does NOT block on it: it verifies the
    HMAC, resolves the project + element, inserts a `running` spine_promote_runs
    row, returns 202 immediately, and runs the promotion in a background task
    that updates the row to done/failed when it finishes. mc-2 polls a status
    endpoint keyed on the run_id.

    Mirrors /api/assets/ingest exactly (HMAC, 202-then-background, env creds,
    best-effort run-row updates). Reuses the already-built promote_transcript
    (engagement-only carries through as a failed run, never a crash).

    Request body (JSON):
        {"code": "<project_code>", "key": "<est_item_id>"}  # both required

    Response (202):
        {"run_id": "<code>/<uuid>", "status": "running"}
    """
    from uuid import uuid4

    from cp_engine.asset_ingest import _utc_now_iso, resolve_project_folders_by_id
    from cp_engine.project_sources import resolve_live_element

    raw_body = await request.body()
    signatures._verify_signature(
        raw_body,
        request.headers.get("x-webhook-signature", ""),
        request.headers.get("x-webhook-timestamp", ""),
    )
    payload = json.loads(raw_body)
    code = (payload.get("code") or "").strip()
    key = (payload.get("key") or "").strip()
    if not code or not key:
        raise HTTPException(status_code=400, detail="code and key are required")

    client = pipeline._create_supabase_client()
    if client is None:
        raise HTTPException(
            status_code=500, detail="Supabase not configured for spine promote"
        )

    # Resolve project_id (two-form code bridge), company_id (via folders; None
    # for initiatives — carries through to a failed run), and the element row.
    project_id = _resolve_project_id_for_promote(client, code)
    if project_id is None:
        raise HTTPException(
            status_code=404, detail=f"no MC-2 project resolved for '{code}'"
        )
    folders = resolve_project_folders_by_id(client, project_id)
    company_id = folders.company_id if folders else None
    element_row = resolve_live_element(client, project_id, key)
    if element_row is None:
        raise HTTPException(
            status_code=404,
            detail=f"no single live element matching '{key}' in '{code}'",
        )

    run_id = f"{code}/{uuid4()}"
    _spine_promote_runs_table(client).insert({
        "id": run_id,
        "project_id": project_id,
        "project_code": code,
        "est_item_id": element_row.get("est_item_id"),
        "status": "running",
        "started_at": _utc_now_iso(),
    }).execute()
    pipeline._spawn_background(_run_promote(run_id, code, project_id, company_id, element_row))
    return Response(
        content=json.dumps({"run_id": run_id, "status": "running"}),
        status_code=202,
        media_type="application/json",
    )
