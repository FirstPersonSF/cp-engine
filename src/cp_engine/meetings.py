"""Bridge Fathom meetings into the project/RAG world.

Meetings are tagged in MC-2's ``fathom_meetings.project_tags`` (a ``text[]``)
with human DISPLAY STRINGS, e.g. ``["IBX 5167 DDI Platform Video"]`` — not a
resolved project id. This module resolves those tags to a ``projects.id``.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from cp_engine.asset_ingest import _utc_now_iso
from cp_engine.spine_promote import ingest_single_file

_SKIP_TAGS = {"untagged", ""}


def _safe(s: str) -> str:
    """Sanitize an identifier into a filesystem-safe name (mirrors
    spine_promote._safe / asset_ingest._stable_dir_for's `re.sub`)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(s))


def stamp_meeting_asset(client, *, project_id: str, source_file_id: str,
                        title: str, file_path: str, meta: dict) -> dict:
    """Stamp the just-ingested meeting-summary rag_assets row with provenance.

    Locates the active row by (project_id, file_path, status='active') — the
    same locate-key spine_promote / the folder-scan stamp use — and writes
    source_provider='fathom', source_file_id=<recording_id>, scope='project',
    and `meta` (the kind discriminator). Because the summary is written to a
    STABLE file_path keyed on recording_id, re-embedding lands on the same row,
    so this update is idempotent (re-stamps, never duplicates). No `SELECT *`;
    fields set directly via `.update()` (matching stamp_promoted_asset).
    """
    # Full `meta` overwrite is safe here: the row is freshly ingested by the
    # caller immediately before this stamp, so there is no prior caller-set meta
    # (e.g. a change_token) to clobber.
    resp = (
        client.table("rag_assets")
        .update({
            "source_provider": "fathom",
            "source_file_id": source_file_id,
            "source_path": None,
            "scope": "project",
            "meta": meta,
        })
        .eq("project_id", project_id)
        .eq("file_path", file_path)
        .eq("status", "active")
        .execute()
    )
    rows = getattr(resp, "data", None) or []
    return {"stamped": bool(rows), "title": title, "ids": [r.get("id") for r in rows]}


def embed_meeting_summary(client, meeting_row, project_id, *,
                          ingest=ingest_single_file, stamp=stamp_meeting_asset,
                          supabase_url, supabase_key, force=False) -> dict:
    """Embed a tagged meeting's summary text into rag_assets for `project_id`.

    Returns `{"ok": True, "asset_id": ...}` on success, else
    `{"ok": False, "reason": ...}`. NEVER raises — any exception is wrapped.

    Skip conditions (return ok:False WITHOUT any ingest/stamp work):
      - `summary` empty/whitespace → "meeting has no summary".
      - `summary_embedded_at` set and not `force` → "summary already embedded".
      - `recording_id` missing/falsy → "meeting has no recording_id" (it's the
        stable-path + provenance key; a None would corrupt both — fail first,
        mirroring promote_transcript's est_item_id guard).

    The summary is written to a STABLE temp path keyed on recording_id
    (`<tmp>/meeting-embed/<recording_id>/summary.md`), so re-embedding lands on
    the same rag_assets row (idempotent). The SAME path is passed to both
    `ingest` and `stamp`; after stamping we VERIFY exactly one row matched
    (stamped True and len(ids)==1) before write-back, else ok:False — otherwise
    "reported success but nothing was stamped". On success, write
    `fathom_meetings.summary_embedded_at = now()` (located by recording_id).

    The `meta={'kind':'meeting_summary'}` discriminator is load-bearing: it lets
    retrieval separate summary-recall from transcript-depth.
    """
    try:
        summary = meeting_row.get("summary")
        if not summary or not summary.strip():
            return {"ok": False, "reason": "meeting has no summary"}

        if meeting_row.get("summary_embedded_at") and not force:
            return {"ok": False, "reason": "summary already embedded"}

        recording_id = meeting_row.get("recording_id")
        if not recording_id:
            return {"ok": False, "reason": "meeting has no recording_id"}

        title = meeting_row.get("title") or "Meeting summary"

        # STABLE dest path keyed on recording_id, under the SYSTEM temp dir
        # (mirroring spine_promote): deterministic per recording_id, so a
        # re-embed lands on the same rag_assets row.
        dest_dir = Path(tempfile.gettempdir()) / "meeting-embed" / _safe(recording_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "summary.md"
        dest.write_text(summary, encoding="utf-8")
        stable_path = str(dest)

        ingest(stable_path, project_id, title,
               supabase_url=supabase_url, supabase_key=supabase_key)

        # SAME stable_path to stamp, then verify exactly one match.
        st = stamp(client, project_id=project_id, source_file_id=str(recording_id),
                   title=title, file_path=stable_path,
                   meta={"kind": "meeting_summary"})
        ids = st.get("ids") or []
        if not st.get("stamped") or len(ids) != 1:
            reason = ("stamp matched no row" if not st.get("stamped")
                      else f"stamp matched {len(ids)} rows")
            return {"ok": False, "reason": reason}

        # Mark the meeting embedded (located by the stable recording_id key).
        (
            client.table("fathom_meetings")
            .update({"summary_embedded_at": _utc_now_iso()})
            .eq("recording_id", recording_id)
            .execute()
        )

        return {"ok": True, "asset_id": ids[0]}
    except Exception as exc:  # never raises
        return {"ok": False, "reason": str(exc)}


def flatten_transcript(transcript) -> str:
    """Flatten a Fathom JSONB transcript (array of segment objects) to text.

    Each segment is ``{"text", "speaker": {"display_name"}, "timestamp"}``; any
    field may be absent. Each kept segment becomes a line
    ``[<timestamp>] <speaker>: <text>`` — the ``[<timestamp>]`` bracket is
    omitted when there's no timestamp, and the speaker degrades to ``"Unknown"``
    when there's no ``speaker.display_name``. Segments with no ``text`` (or
    whitespace-only ``text``) are skipped; kept text is stripped. Lines are
    joined with newlines; ``None``/empty → ``""``.
    """
    lines = []
    for seg in transcript or []:
        if not isinstance(seg, dict):
            continue
        text = seg.get("text")
        if not text or not text.strip():
            continue
        text = text.strip()
        speaker = (seg.get("speaker") or {}).get("display_name") or "Unknown"
        ts = seg.get("timestamp")
        prefix = f"[{ts}] " if ts else ""
        lines.append(f"{prefix}{speaker}: {text}")
    return "\n".join(lines)


def promote_meeting_transcript(client, meeting_row, project_id, company_id, *,
                               ingest=ingest_single_file,
                               stamp=stamp_meeting_asset,
                               supabase_url, supabase_key, force=False) -> dict:
    """Promote a tagged meeting's FULL transcript into rag_assets for `project_id`.

    Mirrors `embed_meeting_summary` + `spine_promote.promote_transcript`. Returns
    `{"ok": True, "asset_id": ...}` on success, else `{"ok": False, "reason": ...}`.
    NEVER raises — any exception is wrapped.

    CONTRACT A — engagement gate FIRST (before any work): if `company_id is None`
    (an initiative) return ok:False with NO file/ingest work; initiative promotion
    is deferred in v1.

    Skip guards (return ok:False, no ingest/stamp work):
      - `recording_id` missing/falsy → it's the stable-path + provenance key; a
        None would corrupt both, so fail first.
      - `transcript` None or no segments → "meeting has no transcript".
      - `transcript_promoted_at` set and not `force` → "transcript already promoted".

    The flattened transcript is written to a STABLE temp path keyed on
    recording_id (`<tmp>/meeting-promote/<recording_id>/transcript.md`), so a
    re-promote lands on the same rag_assets row (idempotent). The SAME path goes
    to BOTH `ingest` and `stamp`; after stamping we VERIFY exactly one row matched
    (stamped True, len(ids)==1) before the write-back, else ok:False.

    The `meta={'kind':'meeting_transcript'}` discriminator is load-bearing: it
    separates deep transcript chunks from summary recall.

    NOTE: near-identical to `embed_meeting_summary` (stable path → ingest → stamp
    → verify-one → write-back → never-raises). Two copies is acceptable; if a
    THIRD meeting-asset variant lands (e.g. action-items, chapters), extract a
    shared `_promote_meeting_asset(..., source_text, subdir, meta_kind, column)`
    helper rather than adding another near-copy.
    """
    try:
        # CONTRACT A — engagement gate, before any work.
        if company_id is None:
            return {"ok": False, "reason": "initiative promotion not yet supported"}

        recording_id = meeting_row.get("recording_id")
        if not recording_id:
            return {"ok": False, "reason": "meeting has no recording_id"}

        # Check the already-promoted flag BEFORE flattening, so a re-promote-skip
        # pays no flatten cost (mirrors embed_meeting_summary's ordering).
        if meeting_row.get("transcript_promoted_at") and not force:
            return {"ok": False, "reason": "transcript already promoted"}

        text = flatten_transcript(meeting_row.get("transcript"))
        if not text:
            return {"ok": False, "reason": "meeting has no transcript"}

        title = meeting_row.get("title") or "Meeting transcript"

        # STABLE dest path keyed on recording_id, under the SYSTEM temp dir:
        # deterministic per recording_id, so a re-promote lands on the same row.
        dest_dir = Path(tempfile.gettempdir()) / "meeting-promote" / _safe(recording_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "transcript.md"
        dest.write_text(text, encoding="utf-8")
        stable_path = str(dest)

        ingest(stable_path, project_id, title,
               supabase_url=supabase_url, supabase_key=supabase_key)

        # SAME stable_path to stamp, then verify exactly one match.
        st = stamp(client, project_id=project_id, source_file_id=str(recording_id),
                   title=title, file_path=stable_path,
                   meta={"kind": "meeting_transcript"})
        ids = st.get("ids") or []
        if not st.get("stamped") or len(ids) != 1:
            reason = ("stamp matched no row" if not st.get("stamped")
                      else f"stamp matched {len(ids)} rows")
            return {"ok": False, "reason": reason}

        # Mark the meeting promoted (located by the stable recording_id key).
        (
            client.table("fathom_meetings")
            .update({"transcript_promoted_at": _utc_now_iso()})
            .eq("recording_id", recording_id)
            .execute()
        )

        return {"ok": True, "asset_id": ids[0]}
    except Exception as exc:  # never raises
        return {"ok": False, "reason": str(exc)}


def _default_resolver(client, code):
    from cp_engine.mcp_server import _resolve_project_id

    return _resolve_project_id(client, code)


# Minimum classifier confidence to AUTO-attach a meeting to a work item. At or
# above this we assign; below it we leave the work item unassigned (the UI shows
# "needs assignment") for later manual classification — always reclassifiable.
WORK_ITEM_ASSIGN_THRESHOLD = 0.75


def assign_work_item(meeting_row, project_id, *, classifier,
                     threshold=WORK_ITEM_ASSIGN_THRESHOLD):
    """Confidence-gate a meeting's work-item assignment via an injected classifier.

    The real classifier (an LLM routing a meeting to one of the project's
    estimator work items) is a separate webhook-layer task; here it is always
    INJECTED. This function is a PURE gate: NO LLM, NO Supabase, NO network.

    Returns `(work_item_id, confidence)`:
      - Auto-assign only on HIGH confidence (`confidence >= threshold`, inclusive)
        → `(work_item_id, confidence)`.
      - Below threshold → `(None, confidence)`: the work item is dropped (left
        unassigned for manual classification) but the best-guess confidence is
        RETAINED so a caller/UI can show "best guess 0.6, not auto-assigned".
      - No candidate (classifier returns None), no work_item_id, or a None
        confidence → `(None, None)`.

    The classifier returns either `None` (no candidate) or a candidate in one of
    two shapes, both supported defensively:
      - a 2-tuple `(work_item_id, confidence)`, or
      - a dict with keys `work_item_id` + `confidence`.
    Both are normalized to `(work_item_id, confidence)` before gating.

    NEVER raises — any exception (including a misbehaving classifier) is wrapped
    and yields `(None, None)`.
    """
    try:
        candidate = classifier(meeting_row, project_id)
        if candidate is None:
            return None, None

        if isinstance(candidate, dict):
            work_item_id = candidate.get("work_item_id")
            confidence = candidate.get("confidence")
        else:
            work_item_id, confidence = candidate

        if not work_item_id or confidence is None:
            return None, None

        if confidence >= threshold:
            return work_item_id, confidence
        # Below threshold: leave unassigned but surface the best-guess confidence.
        return None, confidence
    except Exception:  # never raises
        return None, None


def _default_assigner(meeting_row, project_id):
    """Default work-item assigner: no auto-assign. The real high-confidence
    classifier is a separate task; this seam keeps `link_meeting` testable."""
    return None, None


def _default_rescope(client, meeting_row, new_project_id):
    """Default re-scope cascade: a no-op. The real cascade (moving rag_assets +
    the meeting's project_id on a retag) is a separate task."""
    return {"ok": True, "rescoped": False}


def rescope_meeting(client, meeting_row, new_project_id, *,
                    new_company_id=None) -> dict:
    """RETAG re-scope cascade: move a meeting + its RAG rows to `new_project_id`.

    This is the real implementation behind the `rescope` seam that `link_meeting`
    defaults to a no-op (`_default_rescope`). NEVER raises — any exception is
    wrapped as `{"ok": False, "reason": ...}`.

    1. Guard: falsy `recording_id` → `{"ok": False, "reason": ...}`, no work
       (it's the locate key for both UPDATEs; a None would silently no-op / match
       broadly, mirroring the sibling guards in this module).
    2. No-op: if the stored `project_id` already equals `new_project_id` this is
       not actually a retag → `{"ok": True, "rescoped": False, "reason":
       "project unchanged"}`, no UPDATEs.
    3. ONE UPDATE on `rag_assets` (project_id=new) located by
       (source_provider='fathom', source_file_id=str(recording_id)) — NOT by
       project_id. The summary + transcript rows SHARE that source_file_id, so a
       single UPDATE moves both kinds; `asset_chunks` reference their parent by
       `asset_id` and carry no project scope of their own, so they follow
       automatically — NO re-embedding is ever needed on a retag.

       `company_id` is rewritten ONLY when `new_company_id` is provided. A None
       `new_company_id` (the default — and what `link_meeting` passes, calling
       this seam positionally with no company arg) leaves the existing company
       association INTACT: re-scoping the project must never erase company_id,
       which is load-bearing for account-scoped retrieval (the read RPC matches
       `a.company_id = p_company_id`).

       The locate key deliberately EXCLUDES project_id: that's what makes the
       cascade idempotent / no-ghost. Re-running with the same target (even with
       a stale row still showing the old project) re-finds the rows and
       harmlessly re-sets the same new project_id; after a success no rag_assets
       row for this recording_id remains pointing at the old project.
    4. ONE UPDATE on `fathom_meetings` (project_id=new, work_item_id=None,
       work_item_confidence=None) located by recording_id. The OLD project's
       work item is invalid for the new project, so we CLEAR it — the new
       project's work item is unknown until re-assigned.

       NOTE: `link_meeting` ALSO writes project_id/work_item_id after calling
       this; that's fine and idempotent. Clearing work_item_id here is the
       correct default — `link_meeting`'s assigner re-derives it for the new
       project (or leaves it None) on the subsequent write.
    """
    try:
        recording_id = meeting_row.get("recording_id")
        if not recording_id:
            return {"ok": False, "reason": "meeting has no recording_id"}

        old = meeting_row.get("project_id")
        if old == new_project_id:
            return {"ok": True, "rescoped": False, "reason": "project unchanged"}

        # Move BOTH rag_assets rows (summary + transcript) in one UPDATE. Located
        # by the scope-INDEPENDENT fathom source key so already-moved rows are
        # still found (idempotent / no-ghost). Chunks follow by asset_id. Only
        # rewrite company_id when actually provided — a None must NOT erase the
        # existing company association (load-bearing for account-scoped recall).
        ra_payload = {"project_id": new_project_id}
        if new_company_id is not None:
            ra_payload["company_id"] = new_company_id
        resp = (
            client.table("rag_assets")
            .update(ra_payload)
            .eq("source_provider", "fathom")
            .eq("source_file_id", str(recording_id))
            .execute()
        )
        rows = getattr(resp, "data", None)
        assets_moved = len(rows) if rows is not None else None

        # Re-scope the meeting row; clear the now-invalid old work item.
        (
            client.table("fathom_meetings")
            .update({
                "project_id": new_project_id,
                "work_item_id": None,
                "work_item_confidence": None,
            })
            .eq("recording_id", recording_id)
            .execute()
        )

        return {"ok": True, "rescoped": True, "old_project_id": old,
                "new_project_id": new_project_id, "assets_moved": assets_moved}
    except Exception as exc:  # never raises
        return {"ok": False, "reason": str(exc)}


def link_meeting(client, meeting_row, *,
                 resolver=_default_resolver,
                 assigner=_default_assigner,
                 rescope=_default_rescope,
                 embed=embed_meeting_summary,
                 supabase_url, supabase_key) -> dict:
    """Orchestrate one meeting's link: resolve → assign → write link → embed.

    The webhook flow calls this per meeting. NEVER raises — any exception is
    wrapped as `{"ok": False, "reason": ...}`.

    1. Resolve `meeting_row["project_tags"]` to a project via
       `resolve_meeting_project` (module-level name, so it's monkeypatchable;
       it carries its own default resolver — `resolver` is threaded through).
    2. Untagged / unresolvable (no project): do NO work — no embed, no link
       write, no rescope. Return `{ok:True, linked:False, reason:...}`.
    3. Retag: if a DIFFERENT project was already stored on the row, call
       `rescope(client, meeting_row, new_project_id)` and surface its result
       under `"rescope"`. The cascade owns moving rag_assets; we still write the
       link columns below (AFTER the cascade) so the final project_id is
       consistent and the write stays idempotent.
    4. Assign: `assigner(meeting_row, new_project_id) -> (work_item_id, conf)`.
    5. Write the three link columns on `fathom_meetings` (located by
       recording_id): project_id, work_item_id, work_item_confidence.
    6. Embed the summary via the injected `embed`; surface under `"embed"`.
    """
    try:
        new_project_id, matched_tag = resolve_meeting_project(
            client, meeting_row.get("project_tags"), resolver=resolver)

        if new_project_id is None:
            # NOTE: this branch treats "never tagged" and "tag removed"
            # identically — a meeting previously linked to a project that gets
            # UN-tagged keeps its stale project_id/work_item_id/summary link. We
            # do NOT clear a prior link here; the un-link/clear path is deferred
            # and belongs with the rescope cascade task.
            return {"ok": True, "linked": False,
                    "reason": "no resolvable project tag"}

        # Once we know we're going to write, a falsy recording_id must fail
        # FIRST: it's the locate key for the link-column UPDATE, and an
        # `.eq("recording_id", None)` would be a silent no-op / broad match.
        # Mirrors the sibling guards in embed_meeting_summary / promote_transcript.
        recording_id = meeting_row.get("recording_id")
        if not recording_id:
            return {"ok": False, "reason": "meeting has no recording_id"}

        out = {"ok": True, "linked": True, "project_id": new_project_id,
               "matched_tag": matched_tag}

        # Retag: stored project differs from the freshly resolved one. Run the
        # cascade FIRST so our link-column write below lands the final state.
        stored = meeting_row.get("project_id")
        if stored and stored != new_project_id:
            out["rescope"] = rescope(client, meeting_row, new_project_id)

        work_item_id, confidence = assigner(meeting_row, new_project_id)
        out["work_item_id"] = work_item_id
        out["confidence"] = confidence

        # Write the link columns by recording_id (idempotent).
        (
            client.table("fathom_meetings")
            .update({
                "project_id": new_project_id,
                "work_item_id": work_item_id,
                "work_item_confidence": confidence,
            })
            .eq("recording_id", recording_id)
            .execute()
        )

        out["embed"] = embed(client, meeting_row, new_project_id,
                             supabase_url=supabase_url, supabase_key=supabase_key)
        return out
    except Exception as exc:  # never raises
        return {"ok": False, "reason": str(exc)}


def resolve_meeting_project(
    client,
    project_tags,
    *,
    resolver=_default_resolver,
) -> tuple[str | None, str | None]:
    """Resolve a meeting's ``project_tags`` to a ``(project_id, matched_tag)``.

    Iterates ``project_tags`` (may be ``None`` or empty), skipping any tag that
    is empty/whitespace or an "untagged" marker. The first tag that ``resolver``
    maps to a truthy project id wins. Returns ``(None, None)`` if none resolve.
    """
    for tag in project_tags or []:
        if not tag or tag.strip().lower() in _SKIP_TAGS:
            continue
        project_id = resolver(client, tag)
        if project_id:
            return project_id, tag
    return None, None
