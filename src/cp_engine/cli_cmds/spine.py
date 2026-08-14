"""Spine verbs: spine, where, spine-stats, spine-frame, sweep, spine-recover, snapshot, snapshots.

Split from cli.py (arch-phase-4, #33). Shared helpers stay in
cp_engine.cli and are called via the module attribute so test
monkeypatches of `cp_engine.cli.<helper>` keep working.
"""

from __future__ import annotations

import sys

import click

import cp_engine.cli as _cli
from cp_engine import mc2_db


def _spine_mc2_client(config):
    """Connect to MC-2 for best-effort read-only facets, or return None.

    Used by `cp spine`'s Source-documents facet, which needs a live client that
    `_load_spine_elements` does not expose. Never raises — a facet must never
    break the sweep — so any failure degrades to None (facet omitted).
    """
    return mc2_db.get_client(config, required=False)


@click.command("spine")
@click.argument("code")
def spine_cmd(code: str) -> None:
    """Print the project spine's full ranked relevance sweep.

    Reads the spine from MC-2 (canonical); falls back to the on-disk markdown
    frontmatter when MC-2 is unreachable so the command works offline. When
    MC-2 serves the read, appends a 'Source documents' facet listing the
    project's rag_assets and which distilled elements link them.
    """
    from datetime import date

    from cp_engine.spine import render_source_documents, render_sweep

    config = _cli._load_config_or_die()
    elements, project_dir = _cli._load_spine_elements(config, code)

    click.echo(render_sweep(code, elements, today=date.today()))

    # Source-documents facet — best-effort, MC-2-only. project_dir is None
    # exactly when MC-2 served the spine read; we reuse that as the signal that
    # MC-2 is reachable, then open a fresh client for the asset query.
    if project_dir is None:
        try:
            client = _spine_mc2_client(config)
            if client is not None:
                assets = _cli.fetch_project_assets(client, code)
                block = render_source_documents(assets, elements)
                if block:
                    click.echo()
                    click.echo(block)
        except Exception as exc:  # noqa: BLE001 — facet must never break the sweep
            click.echo(f"(source-documents facet skipped: {exc})", err=True)


@click.command("where")
@click.argument("code")
def where_cmd(code: str) -> None:
    """Print a grounded "where are we / what's next" answer for a project.

    Composes the live estimate, schedule, derived execution status, and
    substance into one terminal summary where every factual clause carries a
    `[source]` marker. `code` is the project's DIR-SLUG (e.g.
    `ibx-5153-ai-campaign`).

    Needs MC-2 — this reads the live estimate + schedule + substance, so there
    is NO offline fallback (unlike `cp spine`). If MC-2 is unreachable this
    prints a clear error and exits non-zero.
    """
    from datetime import date

    from cp_engine.estimate import fetch_estimate, fetch_schedule
    from cp_engine.sync import BackendUnavailable
    from cp_engine.where import build_where_report, fetch_substance_status

    config = _cli._load_config_or_die()
    try:
        client = mc2_db.get_client(config)
    except BackendUnavailable as exc:
        click.echo(
            f"cp where needs MC-2 (no offline mode — it reads the live "
            f"estimate + schedule + substance): {exc}",
            err=True,
        )
        sys.exit(1)

    # Resolve the dir-slug code → mc_project_id slug-natively: read the project's
    # substance rows by project_code (the slug) and lift project_id (which IS
    # the mc_project_id) out of the same read. The number-parsing resolvers fail
    # on slug codes.
    substance_by_item, mc_project_id = fetch_substance_status(client, code)
    if mc_project_id is None:
        click.echo(
            f"No spine substance for '{code}'. `cp where` resolves the project "
            f"via its substance rows — frame at least one work item "
            f"(cp spine-frame) first, or check the dir-slug code.",
            err=True,
        )
        sys.exit(1)

    estimate = fetch_estimate(client, mc_project_id)
    if estimate is None:
        click.echo(
            f"No default estimate for '{code}' (mc_project_id={mc_project_id}). "
            f"The estimate is the spine's backbone — nothing to ground against.",
            err=True,
        )
        sys.exit(1)
    schedule = fetch_schedule(client, estimate.id)

    click.echo(
        build_where_report(estimate, schedule, substance_by_item, today=date.today())
    )


@click.command("spine-stats")
@click.option(
    "--type", "type_filter", default=None, help="Narrow to one deliverable type."
)
@click.option(
    "--within-days", default=14, show_default=True, help="Due-soon window (days)."
)
def spine_stats_cmd(type_filter: str | None, within_days: int) -> None:
    """Cross-project analytics over the spine (Deliverables).

    Needs MC-2 — this is inherently the cross-project index, so there is no
    offline/disk fallback (unlike `cp spine`). If MC-2 is unreachable this
    prints a clear error and exits non-zero.
    """
    from datetime import date

    from cp_engine.spine_stats import (
        due_soon,
        stage_distribution,
        type_inventory,
    )
    from cp_engine.sync import BackendUnavailable

    config = _cli._load_config_or_die()
    try:
        client = mc2_db.get_client(config)
    except BackendUnavailable as exc:
        click.echo(
            f"cross-project stats need MC-2 (this command has no offline mode): {exc}",
            err=True,
        )
        sys.exit(1)

    inv = type_inventory(client)
    dist = stage_distribution(client)
    soon = due_soon(client, today=date.today(), within_days=within_days)

    # --type narrows the per-type views (inventory + due-soon). The stage
    # distribution stays global by design (a cross-project health readout).
    if type_filter:
        inv = [(t, n) for t, n in inv if t == type_filter]
        soon = [r for r in soon if r.get("type") == type_filter]

    click.echo("Deliverable type inventory:")
    if inv:
        for t, n in inv:
            click.echo(f"  {n:3d}  {t}")
    else:
        click.echo("  (none)")

    stage_header = (
        "Stage distribution (all types):" if type_filter else "Stage distribution:"
    )
    click.echo(f"\n{stage_header}")
    if dist:
        click.echo(
            "  " + " · ".join(f"{stage}: {n}" for stage, n in sorted(dist.items()))
        )
    else:
        click.echo("  (none)")

    click.echo(f"\nDue within {within_days} days (excludes shipped):")
    if soon:
        for r in soon:
            click.echo(
                f"  {r.get('target_date')}  {r.get('project_code')}  "
                f"{r.get('title')}  [{r.get('stage')}]"
            )
    else:
        click.echo("  (none)")


@click.command("spine-frame")
@click.argument("card_id")
@click.option("--framing", required=True, help="The directing brief for the distillation.")
@click.option(
    "--est-item-id", "est_item_id", default=None,
    help="Estimate work item to bind to (defaults to the card's guess).",
)
@click.option(
    "--kind", default=None,
    help="Work item kind (activity|deliverable); defaults to the estimate item's kind.",
)
@click.option(
    "--source", "sources", multiple=True,
    help="A source ref (repeatable). Defaults to the card's source_ref.",
)
@click.option(
    "--model", default="claude-opus-4-7", show_default=True,
    help="Anthropic model for the directed distillation.",
)
def spine_frame_cmd(card_id, framing, est_item_id, kind, sources, model) -> None:
    """Frame + promote a proposed spine_inbox card into a live substance version.

    Loads the card, runs a directed re-distillation under the framing brief,
    and writes a new live `## v<N>` substance version bound to an estimate work
    item (the card's guessed item by default). Prints the written path.
    """
    from cp_engine.estimate import fetch_estimate
    from cp_engine.plan_from_transcript import _call_claude
    from cp_engine.spine import find_spine_dir
    from cp_engine.spine_inbox import load_card, promote_card
    from cp_engine.sync import BackendUnavailable

    config = _cli._load_config_or_die()
    try:
        client = mc2_db.get_client(config)
    except BackendUnavailable as exc:
        click.echo(f"cp spine-frame needs MC-2: {exc}", err=True)
        sys.exit(1)

    card = load_card(client, card_id)
    if card is None:
        click.echo(f"Error: no inbox card '{card_id}'.", err=True)
        sys.exit(1)

    # Resolve the target estimate item (CLI default = the card's guess). The
    # estimate gives us the item's name + phase for the substance file path +
    # frontmatter; degrade gracefully if the estimator schema is unreachable.
    target_item_id = est_item_id or card.guessed_est_item_id
    if not target_item_id:
        click.echo(
            "Error: no estimate item to bind to. Pass --est-item-id "
            "(the card carries no guess).",
            err=True,
        )
        sys.exit(1)

    name = phase = None
    resolved_kind = kind
    try:
        estimate = fetch_estimate(client, card.project_id)
    except Exception as exc:  # noqa: BLE001 — estimator unreachable
        click.echo(f"(estimate fetch failed — proceeding unbound-by-name: {exc})",
                   err=True)
        estimate = None
    if estimate is not None:
        item = estimate.item_by_id(target_item_id)
        if item is not None:
            name = item.name
            resolved_kind = kind or item.kind
            for ph in estimate.phases:
                if any(i.id == target_item_id for i in ph.items):
                    phase = ph.name
                    break
    resolved_kind = resolved_kind or "deliverable"

    project_dir = find_spine_dir(config.root, card.project_code)
    src = list(sources) or [card.source_ref]

    path = promote_card(
        card,
        framing=framing,
        est_item_id=target_item_id,
        kind=resolved_kind,
        project_dir=project_dir,
        sources=src,
        distiller=_call_claude,
        model=model,
        client=client,
        name=name,
        phase=phase,
    )
    click.echo(f"Wrote {path}")


@click.command("sweep")
@click.argument("code")
@click.option(
    "--model",
    default="claude-opus-4-7",
    help="LLM model for the synthesis.",
)
def sweep_cmd(code: str, model: str) -> None:
    """Sweep a project's whole spine → write a Synthesis-layer readout.

    Loads the spine (MC-2 canonical, disk fallback — same as `cp spine`), runs
    the LLM synthesis, and writes it as a `Synthesis`-layer element at
    `spine/Synthesis/<today>-sweep.md` (idempotent per day — re-running
    overwrites). An empty spine writes nothing.
    """
    from datetime import date

    import frontmatter

    from cp_engine.spine import SpineDirNotFound, find_spine_dir
    from cp_engine.spine_sweep import run_sweep

    config = _cli._load_config_or_die()
    elements, project_dir = _cli._load_spine_elements(config, code)

    # Empty spine: do NOT write a sentinel Synthesis element, do NOT call the LLM.
    if not elements:
        click.echo(f"No spine elements to sweep for {code}.")
        return

    # The injected LLM wrapper around _call_claude.
    from cp_engine.plan_from_transcript import _call_claude

    def _llm(prompt: str) -> str:
        return _call_claude(prompt, model=model, api_key=None)

    today = date.today()
    try:
        result = run_sweep(
            code, elements, today=today, llm=_llm, tenant_root=config.root
        )
    except Exception as exc:  # noqa: BLE001 — LLM/transport failure
        click.echo(
            f"Sweep synthesis failed (is ANTHROPIC_API_KEY set?): {exc}",
            err=True,
        )
        sys.exit(1)

    # Write the Synthesis-layer element. `project_dir` may be None if the
    # elements came from MC-2; resolve it now for the write target.
    if project_dir is None:
        try:
            project_dir = find_spine_dir(config.root, code)
        except SpineDirNotFound as exc:
            click.echo(str(exc), err=True)
            sys.exit(1)
    syn_dir = project_dir / "spine" / "Synthesis"
    syn_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{today.isoformat()}-sweep.md"
    el_id = f"{code}/synthesis/{today.isoformat()}-sweep"
    # The sweep readout is ABOUT the active work, so it serves the active
    # deliverables — this makes the fresh element score hot on the next Lens
    # pass via _serves_active_term (rather than scoring cold with serves=[]).
    from cp_engine.spine import active_deliverable_ids

    serves = sorted(active_deliverable_ids(elements))
    post = frontmatter.Post(
        result.synthesis_text,
        id=el_id,
        project=code,
        layer="Synthesis",
        type="sweep",
        title=f"Whole-project sweep — {today.isoformat()}",
        status="active",
        last_touched=today.isoformat(),
        serves=serves,
    )
    (syn_dir / fname).write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")

    click.echo(result.ranked_table)
    click.echo(f"\nSynthesis written: spine/Synthesis/{fname}")

    # Best-effort: record proposed drift as review_flags on each element's MC-2
    # row (the human confirms later in the UI). Advisory only — an MC-2 failure
    # must NEVER fail the sweep.
    if result.drift_items:

        try:
            client = mc2_db.get_client(config)
        except Exception as exc:  # noqa: BLE001 — advisory, never fail the sweep
            click.echo(
                f"Could not record drift flags (MC-2 unavailable): {exc}",
                err=True,
            )
        else:
            n = _write_drift_flags(client, result.drift_items, today.isoformat())
            click.echo(f"Flagged {n} drifted element(s) for review.")


@click.command("spine-recover")
@click.argument("code")
@click.option(
    "--apply", "apply_", is_flag=True,
    help="Write the recovered rows. Without it, dry-run (prints the plan only).",
)
@click.option(
    "--model", default="claude-opus-4-7",
    help="LLM model for re-distilling source-backed elements.",
)
def spine_recover_cmd(code: str, apply_: bool, model: str) -> None:
    """Re-home a project's LEGACY spine elements into authored rows.

    Reads the project's legacy capitalized-layer disk files, re-distills the
    source-backed ones from their matched rag_assets (carries synthesis
    elements verbatim), and writes them back as AUTHORED `spine_substance` rows
    under the CANONICAL code (`ibx-5153`). Needs MC-2 (resolves the project +
    pulls asset text). Dry-run by default; `--apply` writes.

    `code` is the canonical `<company>-<number>` (the working-dir form).

    Note: re-running `--apply` re-distills source-backed elements afresh
    (overwriting their v1 body with new LLM output — not a content no-op);
    carried elements are fully idempotent.
    """
    import os
    from datetime import datetime, timezone

    from cp_engine.asset_ingest import resolve_project_folders_by_id
    from cp_engine.mc2_db import _resolve_project_id
    from cp_engine.spine import SpineDirNotFound, find_spine_dir
    from cp_engine.spine_recover import load_legacy_elements, plan_element, recover
    from cp_engine.sync import BackendUnavailable

    config = _cli._load_config_or_die()

    # The canonical code drives both the disk working dir AND the recovered rows'
    # project_code — recovery re-homes EVERYTHING under this one code.
    canonical_code = code
    try:
        project_dir = find_spine_dir(config.root, canonical_code)
    except SpineDirNotFound as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    try:
        client = mc2_db.get_client(config)
    except BackendUnavailable as exc:
        click.echo(f"cp spine-recover needs MC-2: {exc}", err=True)
        sys.exit(1)

    project_id = _resolve_project_id(client, canonical_code)
    if project_id is None:
        click.echo(f"No project_id for '{canonical_code}'. Check the code.", err=True)
        sys.exit(1)
    folders = resolve_project_folders_by_id(client, project_id)
    if folders is None:
        click.echo(f"Could not resolve company for '{canonical_code}'.", err=True)
        sys.exit(1)
    company_id = folders.company_id

    # ANTHROPIC_API_KEY is only needed if some element actually needs re-distill.
    # Decide up front so we fail clearly rather than silently carrying everything.
    from cp_engine.project_sources import list_sources

    assets = [a for a in list_sources(client, project_id, company_id) if a.get("title")]
    elements = load_legacy_elements(project_dir)
    if not elements:
        click.echo(f"No legacy spine elements for '{canonical_code}'.")
        return
    needs_redistill = any(
        plan_element(el, assets).mode == "redistill" for el in elements
    )
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if needs_redistill and not api_key:
        click.echo(
            "Some elements are source-backed and need re-distilling, but "
            "ANTHROPIC_API_KEY is not set. Set it and re-run.",
            err=True,
        )
        sys.exit(1)

    # Real distiller + pull_text wrappers (only used when needs_redistill).
    distiller = None
    pull_text = None
    if needs_redistill:
        from cp_engine.plan_from_transcript import _call_claude
        from cp_engine.project_sources import pull_source

        def distiller(prompt: str) -> str:  # noqa: F811 — local wrapper
            return _call_claude(prompt, model=model, api_key=api_key)

        def pull_text(doc_title: str) -> str:  # noqa: F811 — local wrapper
            pulled = pull_source(client, project_id, company_id, doc_title)
            return "\n\n".join(c for c in (pulled.get("chunks") or []) if c)

    report, rows = recover(
        client=client, project_id=project_id, company_id=company_id,
        project_dir=project_dir, canonical_code=canonical_code,
        now_iso=datetime.now(timezone.utc).isoformat(),
        distiller=distiller, pull_text=pull_text, apply=apply_,
    )

    # Readable per-element table.
    click.echo(f"{'mode':<9} {'layer':<16} {'len':>5}  label · asset")
    for r in report:
        asset = f" · {r['asset']}" if r.get("asset") else ""
        click.echo(
            f"{r['mode']:<9} {r['layer']:<16} {r['body_len']:>5}  "
            f"{r['label']}{asset}"
        )

    rebind_count = sum(1 for r in report if r.get("needs_rebind"))
    if rebind_count:
        click.echo(
            f"\n{rebind_count} element(s) flagged needs-rebind "
            "(re-homed as context; re-bind in the estimate if needed)."
        )

    if apply_:
        click.echo(f"\nWrote {len(rows)} rows under {canonical_code}.")
    else:
        click.echo("\nDRY RUN — no rows written; pass --apply to write.")


def _write_drift_flags(client, drift_items, today: str) -> int:
    """Record each proposed drift item as a `review_flag` on its element's MC-2
    row (one open flag per field, via `_merge_flag`). Best-effort per item: a
    single failure is logged and skipped, the rest still land. Returns the
    number of elements successfully flagged."""
    from cp_engine.spine_sync import _merge_flag

    written = 0
    for item in drift_items:
        element_id = item["element_id"]
        field = item["field"]
        try:
            existing = mc2_db.fetch_element_review_flags(client, element_id)
            if existing is None:
                # Unknown element id (e.g. an LLM hallucination) — don't update
                # zero rows and overcount. Skip quietly.
                continue
            flag = {
                "field": field,
                "was": "(sweep)",
                "now": item["observation"],
                "at": today,
                "source": "sweep",
            }
            merged = _merge_flag(existing, field, flag)
            mc2_db.update_element_review_flags(client, element_id, merged)
            written += 1
        except Exception as exc:  # noqa: BLE001 — advisory, skip on failure
            click.echo(
                f"Could not flag drift on {element_id}: {exc}", err=True
            )
    return written


@click.command("snapshot")
@click.argument("ref")  # "<code>/<deliverable-id>"
@click.option("--label", required=True, help="Human name for this snapshot.")
@click.option("--reason", default=None, help="Why this turn mattered.")
def snapshot_cmd(ref: str, label: str, reason: str | None) -> None:
    """Freeze a deliverable's current body+spine as a named snapshot.

    Writes the frozen markdown into a `<el>.snapshots/` sibling folder (the
    source of truth) and best-effort upserts an index row into MC-2's
    `spine_snapshots` table. An MC-2 failure is logged to stderr but never
    fails the command — the on-disk file is canonical.
    """
    from datetime import date

    from cp_engine.spine import SpineDirNotFound, find_spine_dir
    from cp_engine.spine_snapshot import (
        DeliverableNotFound,
        build_snapshot,
        git_head_commit,
        resolve_deliverable_file,
        working_copy_is_dirty,
    )

    config = _cli._load_config_or_die()
    # `ref` IS the deliverable id; its first path segment is the project code
    # (spine element ids are code-prefixed by convention, e.g.
    # "ibx-5153/deliverable/pos" → code "ibx-5153").
    code = ref.split("/", 1)[0]
    deliverable_id = ref
    try:
        project_dir = find_spine_dir(config.root, code)
    except SpineDirNotFound as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    try:
        working_file = resolve_deliverable_file(project_dir, deliverable_id)
    except DeliverableNotFound as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    working_text = working_file.read_text()
    commit = git_head_commit(config.root)
    dirty = working_copy_is_dirty(working_file, config.root)
    # Resolve the stable MC-2 project uuid off the cp.md `MC-id:` stamp so the
    # row can be code-rehomed later (mig 078). None for an unstamped project —
    # project_id is nullable.
    from cp_engine.sync import _read_mc_id
    project_id = _read_mc_id(project_dir / "cp.md")
    snap = build_snapshot(
        working_text=working_text,
        deliverable_id=deliverable_id,
        project_code=code,
        label=label,
        reason=reason,
        commit=commit,
        working_copy_dirty=dirty,
        created=date.today(),
        project_id=project_id,
    )

    snap_dir = working_file.parent / f"{working_file.stem}.snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    target = snap_dir / snap.filename
    base = target
    n = 2
    while target.exists():
        target = base.with_name(base.stem + f"-{n}" + base.suffix)
        n += 1
    target.write_text(snap.frozen_text)

    row = dict(snap.row)
    row["rel_path"] = str(target.relative_to(config.root))
    # Recompute id from the FINAL filename so the row and the on-disk file
    # agree even after same-day collision resolution.
    row["id"] = f"{deliverable_id}@{target.stem}"
    from cp_engine.sync import BackendUnavailable

    try:
        client = mc2_db.get_client(config)
        mc2_db.upsert_spine_snapshot(client, row)
    except BackendUnavailable as exc:
        # Expected when offline / no creds — the file is the source of truth.
        click.echo(f"(snapshot saved to disk; MC-2 index skipped — {exc})", err=True)
    except Exception as exc:  # noqa: BLE001 — connected but the upsert failed = likely a bug
        click.echo(
            f"(WARNING: snapshot saved to disk, but the MC-2 index upsert "
            f"failed — this may be a schema/query bug: {exc})",
            err=True,
        )

    click.echo(f"Snapshot saved: {target.relative_to(config.root)}")


@click.command("snapshots")
@click.argument("ref")  # "<code>/<deliverable-id>"
def snapshots_cmd(ref: str) -> None:
    """List a deliverable's snapshots (newest first), from disk."""
    import frontmatter
    import yaml

    from cp_engine.spine import SpineDirNotFound, find_spine_dir
    from cp_engine.spine_snapshot import (
        DeliverableNotFound,
        resolve_deliverable_file,
    )

    config = _cli._load_config_or_die()
    code = ref.split("/", 1)[0]
    try:
        project_dir = find_spine_dir(config.root, code)
        working_file = resolve_deliverable_file(project_dir, ref)
    except (SpineDirNotFound, DeliverableNotFound) as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    snap_dir = working_file.parent / f"{working_file.stem}.snapshots"
    if not snap_dir.is_dir():
        click.echo(f"No snapshots for {ref}.")
        return
    snaps = []
    for md in snap_dir.glob("*.md"):
        try:
            meta = frontmatter.load(str(md)).metadata.get("snapshot", {})
        except yaml.YAMLError:
            continue  # skip malformed snapshot files
        if isinstance(meta, dict):
            snaps.append((str(meta.get("created", "")), meta, md.name))
    snaps.sort(key=lambda s: (s[0], s[2]), reverse=True)  # newest first
    click.echo(f"{ref} — {len(snaps)} snapshot(s):")
    for created, meta, fname in snaps:
        reason = meta.get("reason") or ""
        commit = meta.get("commit") or "—"
        click.echo(f"  {created}  {meta.get('label','')}  [{commit}]  {reason}")




@click.command("spine-lint")
@click.argument("code")
def spine_lint_cmd(code: str) -> None:
    """Warn-only spine hygiene lint for one project (#69).

    Flags: important-yet-unbound-serving-nothing elements; Agreements whose
    body says "attach as source" with an empty sources array; scaffold
    template placeholders still in cp.md; Exec Summary fields over their
    per-field word budgets. Run per touched project at
    `wrap up`, alongside word-count discipline. NEVER blocks or fixes —
    prints findings and exits 0 either way (non-zero only when the project
    can't be read at all).
    """
    from cp_engine.exec_summary_lint import lint_exec_summary
    from cp_engine.spine import SpineDirNotFound, find_spine_dir
    from cp_engine.spine_lint import lint_cp_placeholders, lint_spine_rows
    from cp_engine.sync import BackendUnavailable

    config = _cli._load_config_or_die()
    warnings: list[str] = []

    try:
        client = mc2_db.get_client(config)
    except BackendUnavailable as exc:
        click.echo(f"cp spine-lint needs MC-2 for the spine checks: {exc}",
                   err=True)
        sys.exit(1)

    # Accept the same codes the MCP verbs do (#151): resolve the caller's
    # code to the working dir and query every project_code spelling the
    # spine may be keyed under — short code AND dir-slug. Drifted projects
    # carry live rows under both (the ibx-5153 case), so this is a set,
    # not a translation.
    try:
        mc2_id = mc2_db._resolve_project_id(client, code)
    except Exception:  # noqa: BLE001 — resolution is best-effort; lint survives
        mc2_id = None
    try:
        spine_dir = find_spine_dir(config.root, code, mc2_id=mc2_id)
    except SpineDirNotFound:
        spine_dir = None
    codes = [code]
    if spine_dir is not None and spine_dir.name != code:
        codes.append(spine_dir.name)

    all_rows = (
        client.table(mc2_db.Tables.SPINE_SUBSTANCE)
        .select(mc2_db.SPINE_LINT_COLUMNS)
        .in_("project_code", codes)
        .eq("status", "live")
        .execute()
        .data
    ) or []
    # Same one-live-per-element discipline as the read paths (#113) — a
    # double-live element must warn once, not twice.
    from cp_engine.project_sources import _one_live_per_element
    rows = _one_live_per_element([r for r in all_rows if not r.get("archived")])
    if not rows:
        tried = "' / '".join(codes)
        click.echo(
            f"No live spine for '{tried}' — no live spine_substance rows "
            "under any spelling of this project's code.",
            err=True,
        )
        sys.exit(1)
    warnings.extend(lint_spine_rows(rows))

    # Spec-v04 lifecycle checks (#149) — best-effort: a project with no
    # edges (or a read failure on the relations table) lints the rest.
    from cp_engine.spine_lint import lint_lifecycle
    relations_all: list[dict] = []
    try:
        # Every active edge, not a kind subset: check 7 (dead-end activity)
        # reads `informs`/`derives_from`, so filtering to the lifecycle kinds
        # made it see no feeds edges at all and flag every activity. #176's
        # referrer check needs the full set for the same reason.
        relations_all = (
            client.table(mc2_db.Tables.SPINE_RELATIONS)
            .select("kind, from_item_id, to_item_id")
            .in_("project_code", codes)
            .eq("status", "active")
            .execute()
            .data
        ) or []
        warnings.extend(lint_lifecycle(rows, relations_all))
    except Exception:  # noqa: BLE001 — lifecycle checks degrade, lint survives
        pass

    # Curation checks (#112 P3 + #158 gaps 2–4): scaffold Brief, time-bound
    # cards past their moment, raw pastes on distillation layers, unlayered /
    # instruction-shaped elements. Same warn-only surface.
    from cp_engine.spine_lint import lint_curation
    warnings.extend(lint_curation(rows))

    # Archive integrity (#176) — best-effort, and it needs the rows every
    # other check filters out: archived elements, at every status. A dangling
    # pointer and a half-archived element are both invisible to a live-only
    # read, which is exactly why they survived.
    from cp_engine.spine_lint import lint_archived_referrers, lint_partial_archive
    try:
        every_row = (
            client.table(mc2_db.Tables.SPINE_SUBSTANCE)
            .select(mc2_db.SPINE_LINT_COLUMNS)
            .in_("project_code", codes)
            .execute()
            .data
        ) or []
        archived_rows = [r for r in every_row if r.get("archived")]
        warnings.extend(
            lint_archived_referrers(rows, archived_rows, relations_all)
        )
        warnings.extend(lint_partial_archive(every_row))
    except Exception:  # noqa: BLE001 — archive checks degrade, lint survives
        pass

    # cp.md placeholder check — best-effort, offline (skip silently when the
    # working dir doesn't resolve; the spine checks already ran).
    try:
        cp_md = (spine_dir or find_spine_dir(config.root, code)) / "cp.md"
        if cp_md.is_file():
            cp_md_text = cp_md.read_text(encoding="utf-8")
            warnings.extend(lint_cp_placeholders(cp_md_text))
            warnings.extend(lint_exec_summary(cp_md_text))
    except (SpineDirNotFound, OSError):
        pass

    if warnings:
        click.echo(f"{code} — {len(warnings)} spine-lint warning(s):")
        for w in warnings:
            click.echo(f"  {w}")
    else:
        click.echo(f"{code} — spine lint clean.")


@click.command("seal-sweep")
@click.argument("code")
@click.option(
    "--all",
    "all_deliverables",
    is_flag=True,
    help="Every deliverable regardless of version date, not just recent ones. "
    "Use for a project that has never been swept.",
)
@click.option(
    "--within",
    type=int,
    default=None,
    metavar="DAYS",
    help="Recency window for 'shipped a version' (default 14).",
)
@click.option(
    "--today",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Override today's date (YYYY-MM-DD). Useful for testing.",
)
def seal_sweep_cmd(
    code: str, all_deliverables: bool, within: int | None, today
) -> None:
    """Ask what a shipped round consumed — the seal prompt (#175).

    For each deliverable that shipped a version recently, lists the elements
    it plausibly absorbed (`derives_from` / `informs` / `responds_to` edges
    into it), skipping anything already absorbed and anything still feeding a
    deliverable that has NOT shipped. Prints the `seal_to_deliverable` call
    for what survives review.

    READ-ONLY. Sealing moves elements out of the default read, so it stays a
    deliberate act on the `cp-hosted` connector — this makes the decision
    cheap, not automatic. Run per touched project at `wrap up`, alongside
    spine-lint and the commitments sweep.
    """
    from datetime import date as _date

    from cp_engine.seal_sweep import (
        DEFAULT_SHIPPED_WITHIN_DAYS,
        build_rounds,
        render_sweep,
    )
    from cp_engine.spine import SpineDirNotFound, find_spine_dir
    from cp_engine.sync import BackendUnavailable

    config = _cli._load_config_or_die()
    try:
        client = mc2_db.get_client(config)
    except BackendUnavailable as exc:
        click.echo(f"cp seal-sweep needs MC-2: {exc}", err=True)
        sys.exit(1)

    # Same dual-spelling resolution as spine-lint (#151): a drifted project
    # carries live rows under both the short code and the dir slug.
    try:
        mc2_id = mc2_db._resolve_project_id(client, code)
    except Exception:  # noqa: BLE001 — resolution is best-effort
        mc2_id = None
    try:
        spine_dir = find_spine_dir(config.root, code, mc2_id=mc2_id)
    except SpineDirNotFound:
        spine_dir = None
    codes = [code]
    if spine_dir is not None and spine_dir.name != code:
        codes.append(spine_dir.name)

    rows = (
        client.table(mc2_db.Tables.SPINE_SUBSTANCE)
        .select(mc2_db.SEAL_SWEEP_COLUMNS)
        .in_("project_code", codes)
        .eq("status", "live")
        .execute()
        .data
    ) or []
    rows = [r for r in rows if not r.get("archived")]
    if not rows:
        tried = "' / '".join(codes)
        click.echo(f"No live spine for '{tried}'.", err=True)
        sys.exit(1)

    relations = (
        client.table(mc2_db.Tables.SPINE_RELATIONS)
        .select("kind, from_item_id, to_item_id")
        .in_("project_code", codes)
        .eq("status", "active")
        .execute()
        .data
    ) or []

    rounds = build_rounds(
        rows,
        relations,
        today=today.date() if today else _date.today(),
        within_days=within if within is not None else DEFAULT_SHIPPED_WITHIN_DAYS,
        all_deliverables=all_deliverables,
    )
    click.echo(render_sweep(rounds, code=code))


@click.command("stub-sweep")
@click.argument("code")
def stub_sweep_cmd(code: str) -> None:
    """Empty Source-material cards, and where their provenance belongs (#178).

    A card whose body is only the ingest's boilerplate ("Ingested document:
    **X** (doc)") is a wrapper around a rag_asset already in its own `sources`
    array. But most carry a `serves` binding — the routing judgement saying
    which activity or slot the document belongs to — so the move is a
    TRANSFER, not a delete: attach the source to what the stub served, then
    retire the card.

    Stubs routed to a bare estimate slot have nothing to attach to; they are
    reported and nothing is proposed for them.

    READ-ONLY. Retiring a card and moving its provenance is a real mutation —
    this makes the decision cheap, not automatic.
    """
    from cp_engine.spine import SpineDirNotFound, find_spine_dir
    from cp_engine.stub_sweep import find_stubs, render_sweep
    from cp_engine.sync import BackendUnavailable

    config = _cli._load_config_or_die()
    try:
        client = mc2_db.get_client(config)
    except BackendUnavailable as exc:
        click.echo(f"cp stub-sweep needs MC-2: {exc}", err=True)
        sys.exit(1)

    try:
        mc2_id = mc2_db._resolve_project_id(client, code)
    except Exception:  # noqa: BLE001 — resolution is best-effort
        mc2_id = None
    try:
        spine_dir = find_spine_dir(config.root, code, mc2_id=mc2_id)
    except SpineDirNotFound:
        spine_dir = None
    codes = [code]
    if spine_dir is not None and spine_dir.name != code:
        codes.append(spine_dir.name)

    rows = (
        client.table(mc2_db.Tables.SPINE_SUBSTANCE)
        .select(mc2_db.STUB_SWEEP_COLUMNS)
        .in_("project_code", codes)
        .eq("status", "live")
        .execute()
        .data
    ) or []
    rows = [r for r in rows if not r.get("archived")]
    if not rows:
        tried = "' / '".join(codes)
        click.echo(f"No live spine for '{tried}'.", err=True)
        sys.exit(1)

    relations = (
        client.table(mc2_db.Tables.SPINE_RELATIONS)
        .select("kind, from_item_id, to_item_id")
        .in_("project_code", codes)
        .eq("status", "active")
        .execute()
        .data
    ) or []

    click.echo(render_sweep(find_stubs(rows, relations), code=code))


@click.command("exec-lint")
@click.argument("code")
def exec_lint_cmd(code: str) -> None:
    """Warn-only Exec Summary per-field word-budget lint for one project.

    Budgets: Status ≤ 100 words; Where it stands ≤ 5 bullets and ≤ 40
    words/bullet; Next up ≤ 6 bullets; Blockers ≤ 5 bullets. Reads only the
    project's cp.md (offline — no MC-2). NEVER blocks or edits — prints
    findings and exits 0 either way (non-zero only when the project's cp.md
    can't be read at all). Also folded into `cp spine-lint` and echoed after
    `cp render`, alongside word-count discipline.
    """
    from cp_engine.exec_summary_lint import lint_exec_summary
    from cp_engine.spine import SpineDirNotFound, find_spine_dir

    config = _cli._load_config_or_die()
    try:
        cp_md = find_spine_dir(config.root, code) / "cp.md"
        cp_md_text = cp_md.read_text(encoding="utf-8")
    except (SpineDirNotFound, OSError) as exc:
        click.echo(f"Could not read cp.md for '{code}': {exc}", err=True)
        sys.exit(1)

    warnings = lint_exec_summary(cp_md_text)
    if warnings:
        click.echo(f"{code} — {len(warnings)} exec-summary budget warning(s):")
        for w in warnings:
            click.echo(f"  {w}")
    else:
        click.echo(f"{code} — exec-summary within budget.")


@click.command("close")
@click.argument("code")
@click.option(
    "--force",
    is_flag=True,
    help="Scaffold anyway even if the project's MC-2 status is still active.",
)
@click.option(
    "--overwrite",
    is_flag=True,
    help="Regenerate over today's existing checklist (discards any checked "
    "boxes / notes added to it).",
)
def close_cmd(code: str, force: bool, overwrite: bool) -> None:
    """Scaffold the close-out ritual checklist for a wrapping project.

    Emits `<workdir>/close-out-<code>-<date>.md` — a checklist working file
    for the human/agent close-out ritual (final synthesis, terminal Exec
    Summary, commitment sweep, stub retires, account promotion, dir hygiene).
    NEVER mutates project data: no spine writes, no commitment closes.

    Refuses on projects whose MC-2 status is still active-like (engagement
    Deal/Open, initiative Active, repo Active) unless --force. Works offline
    for parked dirs (the inactive/ location is itself evidence of closure);
    a live dir with MC-2 unreachable needs --force.
    """
    from datetime import date

    from cp_engine import close_out as _close
    from cp_engine.spine import SpineDirNotFound

    config = _cli._load_config_or_die()
    try:
        workdir, parked = _close.find_close_workdir(config.root, code)
    except SpineDirNotFound as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    # ── Same-day re-run guard: never clobber a worked checklist ──────
    out_path = workdir / f"close-out-{code}-{date.today().isoformat()}.md"
    if out_path.exists() and not overwrite:
        click.echo(
            f"REFUSING: {out_path} already exists — it may hold checked "
            "boxes and notes from an in-progress ritual. Re-run with "
            "--overwrite to regenerate it from scratch.",
            err=True,
        )
        sys.exit(1)

    client = mc2_db.get_client(config, required=False)

    # ── Status gate ──────────────────────────────────────────────────
    resolved = _close.fetch_item_status(client, code) if client is not None else None
    if resolved is not None:
        kind, status = resolved
        status_label = f"{status or 'unknown'} ({kind})"
        if _close.is_active_like(kind, status) and not force:
            click.echo(
                f"REFUSING: '{code}' is still active in MC-2 "
                f"(status={status!r}, kind={kind}). Close it there first, "
                "or re-run with --force if you really mean to scaffold the "
                "close-out ritual for a live project.",
                err=True,
            )
            sys.exit(1)
    else:
        where = "MC-2 unreachable" if client is None else "no MC-2 row"
        status_label = f"unverified — {where}"
        if not parked and not force:
            click.echo(
                f"REFUSING: cannot verify '{code}' is inactive ({where}) and "
                "its working dir is still live (not under inactive/). Re-run "
                "with --force to scaffold anyway.",
                err=True,
            )
            sys.exit(1)
        click.echo(f"(status unverified: {where} — proceeding: "
                   f"{'dir is parked under inactive/' if parked else '--force'})",
                   err=True)

    # ── Spine read (MC-2 canonical; disk mirror when unreachable) ────
    elements: list = []
    if client is not None:
        try:
            # spine_substance keys on the dir-slug project_code == dir name.
            elements = [
                _close.element_from_row(r)
                for r in _close.fetch_live_spine_rows(client, workdir.name)
            ]
        except Exception as exc:  # noqa: BLE001 — degrade loudly to the mirror
            click.echo(f"(WARNING: MC-2 spine read failed: {exc} — falling "
                       "back to the on-disk mirror)", err=True)
            client_spine_ok = False
        else:
            client_spine_ok = True
    else:
        client_spine_ok = False
    if not client_spine_ok:
        click.echo("(spine from on-disk mirror — last-known, unverified)",
                   err=True)
        # Substance-aware mirror read: modern lowercase phase dirs +
        # spine/_authored/ (where nearly all authored elements live) +
        # legacy capitalized layer dirs. NOT legacy-only load_spine, which
        # is blind on modern projects.
        elements = _close.load_mirror_elements(workdir)

    # ── Commitments (live only — no offline fallback) ────────────────
    commitments = None
    if client is not None:
        try:
            commitments = _close.fetch_open_commitments(client, code)
        except Exception as exc:  # noqa: BLE001 — checklist must still emit
            click.echo(f"(WARNING: commitments read failed: {exc})", err=True)

    root_files = sorted(
        p.name for p in workdir.iterdir()
        if p.is_file() and not p.name.startswith("close-out-")
    )
    text = _close.build_close_checklist(
        _close.CloseChecklistInputs(
            code=code,
            status_label=status_label,
            elements=elements,
            spine_verified=client_spine_ok,
            commitments=commitments,
            workdir_root_files=root_files,
        ),
        today=date.today(),
    )
    out_path.write_text(text, encoding="utf-8")

    n_open = len(commitments) if commitments else 0
    click.echo(f"Close-out checklist written: {out_path}")
    click.echo(
        f"  {len(elements)} live spine element(s) · "
        f"{len(_close.synthesis_targets(elements))} synthesis target(s) · "
        f"{len(_close.thin_stubs(elements))} stub candidate(s) · "
        f"{len(_close.promotion_candidates(elements))} promotion candidate(s) · "
        f"{n_open} open commitment(s)"
    )
    click.echo("Nothing was mutated — work the checklist, then commit it "
               "with the close-out edits.")


@click.command("wrap")
@click.argument("code")
@click.option(
    "--bundle",
    is_flag=True,
    help="Emit the wrap-report bundle as JSON for in-session synthesis.",
)
@click.option(
    "--tail-days",
    default=14,
    show_default=True,
    help="Closing-window width used for the meeting tail-share signal.",
)
def wrap_cmd(code: str, bundle: bool, tail_days: int) -> None:
    """Emit the deterministic raw material for a close-out wrap report.

    Gathers the facts a hand-written retro forgets to look up — duration,
    budget, RECORDED HOURS by person, meeting cadence and where it fell in
    the timeline, deliverables, what each shipped round absorbed, open
    commitments — and prints them as JSON for the model to synthesize
    against a fixed section contract (`/cp-wrap`).

    Facts only: this verb writes no prose and mutates nothing. The report
    itself is authored in-session so it can be defended and revised live —
    the same split as `cp prep-planning --bundle`.

    Pairs with `cp close`: run `wrap` FIRST (it produces the learning
    artifact), then `close` (the hygiene ritual), so the terminal Exec
    Summary can quote the report.
    """
    import json
    from datetime import date

    from cp_engine import close_out as _close
    from cp_engine import wrap_report as _wrap
    from cp_engine.mc2_db import Tables
    from cp_engine.spine import SpineDirNotFound

    config = _cli._load_config_or_die()
    try:
        workdir, _parked = _close.find_close_workdir(config.root, code)
    except SpineDirNotFound as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)

    client = mc2_db.get_client(config, required=False)
    if client is None:
        click.echo(
            "REFUSING: MC-2 is unreachable and the wrap bundle is mostly "
            "MC-2 facts (hours, budget, meeting cadence). A bundle built "
            "from the disk mirror alone would understate effort — which is "
            "the exact failure this verb exists to prevent.",
            err=True,
        )
        sys.exit(1)

    # ── Project facts ────────────────────────────────────────────────
    row: dict = {}
    try:
        rows = (
            client.table(Tables.PROJECTS)
            .select(
                "id, code, name, number, mc_status, start_date, budget, "
                "target_profit_pct, account_manager"
            )
            .eq("code", workdir.name)
            .execute()
            .data
        ) or []
        if not rows:
            # `projects.code` is the SHORT code, not the dir slug (the known
            # code-vs-slug bridge). Fall back to the numeric suffix.
            digits = "".join(ch for ch in code if ch.isdigit())
            if digits:
                rows = (
                    client.table(Tables.PROJECTS)
                    .select(
                        "id, code, name, number, mc_status, start_date, "
                        "budget, target_profit_pct, account_manager"
                    )
                    .eq("number", int(digits))
                    .execute()
                    .data
                ) or []
        row = rows[0] if rows else {}
    except Exception as exc:  # noqa: BLE001 — degrade loudly, keep going
        click.echo(f"(WARNING: project row read failed: {exc})", err=True)

    project_id = row.get("id")

    # ── Meetings ─────────────────────────────────────────────────────
    meetings = _wrap.MeetingLoad(tail_days=tail_days)
    if project_id:
        try:
            mrows = (
                client.table(Tables.FATHOM_MEETINGS)
                .select("id, meeting_date, title, duration_minutes")
                .eq("project_id", project_id)
                .execute()
                .data
            ) or []
            meetings = _wrap.summarize_meetings(mrows, tail_days=tail_days)
        except Exception as exc:  # noqa: BLE001
            click.echo(f"(WARNING: meeting read failed: {exc})", err=True)

    # ── Effort (allocated hours, NOT timesheet actuals) ───────────────
    effort = _wrap.EffortSummary(verified=False)
    if project_id:
        try:
            arows = (
                client.table(Tables.SPRINT_ALLOCATIONS)
                .select("entity_id, hours, week_start")
                .eq("project_id", project_id)
                .execute()
                .data
            ) or []
            names = {
                str(e["id"]): e.get("name") or "unattributed"
                for e in (
                    client.table(Tables.ENTITIES)
                    .select("id, name")
                    .execute()
                    .data
                    or []
                )
            }
            effort = _wrap.summarize_effort(arows, names)
        except Exception as exc:  # noqa: BLE001
            click.echo(f"(WARNING: allocation read failed: {exc})", err=True)

    # ── Spine: deliverables + what each round absorbed ────────────────
    elements: list = []
    spine_ok = True
    try:
        elements = [
            _close.element_from_row(r)
            for r in _close.fetch_live_spine_rows(client, workdir.name)
        ]
    except Exception as exc:  # noqa: BLE001
        click.echo(f"(WARNING: spine read failed: {exc})", err=True)
        spine_ok = False

    deliverables = [e.title for e in elements if (e.layer or "") == "Deliverables"]

    commitments = None
    try:
        commitments = _close.fetch_open_commitments(client, code)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"(WARNING: commitments read failed: {exc})", err=True)

    feedback = sorted(
        p.name
        for p in (workdir / "feedback-on-deck").glob("*.md")
    ) if (workdir / "feedback-on-deck").is_dir() else []

    b = _wrap.WrapBundle(
        code=code,
        name=str(row.get("name") or ""),
        status=str(row.get("mc_status") or ""),
        start_date=_wrap._as_date(row.get("start_date")),
        budget=float(row["budget"]) if row.get("budget") else None,
        target_profit_pct=(
            float(row["target_profit_pct"]) if row.get("target_profit_pct") else None
        ),
        account_manager=str(row.get("account_manager") or ""),
        meetings=meetings,
        effort=effort,
        deliverables=deliverables,
        open_commitments=commitments,
        feedback_artifacts=feedback,
        spine_verified=spine_ok,
    )

    payload = {
        "code": b.code,
        "name": b.name,
        "status": b.status,
        "account_manager": b.account_manager,
        "start_date": b.start_date.isoformat() if b.start_date else None,
        "duration_days": b.duration_days,
        "duration_weeks": b.duration_weeks,
        "budget": b.budget,
        "target_profit_pct": b.target_profit_pct,
        "effort": {
            "verified": b.effort.verified,
            "note": (
                "ALLOCATED hours from sprint_allocations — MC-2's planning "
                "record, NOT timesheet actuals. Say so in the report; "
                "presenting an allocation as an actual overstates precision."
            ),
            "total_hours": b.effort.total_hours,
            "weeks": b.effort.weeks,
            "by_person": [{"name": n, "hours": h} for n, h in b.effort.by_person],
        },
        "budget_per_hour": b.budget_per_hour,
        "meetings": {
            "count": b.meetings.count,
            "total_hours": b.meetings.total_hours,
            "first": b.meetings.first.isoformat() if b.meetings.first else None,
            "last": b.meetings.last.isoformat() if b.meetings.last else None,
            "tail_days": b.meetings.tail_days,
            "tail_share": round(b.meetings.tail_share, 3),
            "tail_hours": round(b.meetings.tail_minutes / 60.0, 1),
            "head_hours": round(b.meetings.head_minutes / 60.0, 1),
            "heaviest_days": [
                {"date": d, "meetings": c, "minutes": m}
                for d, c, m in b.meetings.heaviest_days
            ],
        },
        "deliverables": b.deliverables,
        "feedback_artifacts": b.feedback_artifacts,
        "open_commitments": b.open_commitments,
        "spine_verified": b.spine_verified,
        "learning_axes": [{"key": k, "prompt": p} for k, p in _wrap.LEARNING_AXES],
        "human_entry_fields": list(_wrap.HUMAN_ENTRY_FIELDS),
        "not_assessable_from_data": _wrap.unanswerable_fields(b),
        "generated": date.today().isoformat(),
    }

    if bundle:
        click.echo(json.dumps(payload, indent=2))
        return

    # Human-readable default: the headline signals, not the whole payload.
    click.echo(f"{b.code} — {b.name or '(unnamed)'}  [{b.status or 'status?'}]")
    if b.duration_weeks is not None:
        click.echo(f"  duration      : {b.duration_weeks} weeks "
                   f"(from {b.start_date})")
    if b.budget:
        click.echo(f"  budget        : ${b.budget:,.0f}"
                   + (f" · target {b.target_profit_pct:.0f}%"
                      if b.target_profit_pct else ""))
    if b.effort.verified and b.effort.total_hours:
        click.echo(f"  hours (alloc) : {b.effort.total_hours} across "
                   f"{len(b.effort.by_person)} people, {b.effort.weeks} weeks")
        for n, h in b.effort.by_person:
            click.echo(f"                  {n}: {h}h")
        if b.budget_per_hour:
            click.echo(f"  $/hour        : ${b.budget_per_hour}")
    else:
        click.echo("  hours         : UNREAD — do not report a margin")
    click.echo(f"  meetings      : {b.meetings.count} · "
               f"{b.meetings.total_hours}h total · "
               f"{b.meetings.tail_share:.0%} in the last "
               f"{b.meetings.tail_days} days")
    click.echo(f"  deliverables  : {len(b.deliverables)}")
    click.echo(f"  feedback docs : {len(b.feedback_artifacts)}")
    n_open = len(b.open_commitments) if b.open_commitments else 0
    click.echo(f"  open commits  : {n_open}")
    click.echo("")
    click.echo("Run with --bundle for the JSON the model synthesizes from "
               "(see /cp-wrap). Nothing was mutated.")
