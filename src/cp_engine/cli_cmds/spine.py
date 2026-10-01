"""Spine verbs: spine, where, spine-stats, sweep, spine-recover.

Split from cli.py (arch-phase-4, #33). Shared helpers stay in
cp_engine.cli and are called via the module attribute so test
monkeypatches of `cp_engine.cli.<helper>` keep working.
"""

from __future__ import annotations

import sys
from datetime import UTC
from pathlib import Path

import click

import cp_engine.cli as _cli
from cp_engine.clock import tenant_today
from cp_engine import mc2_db


def _spine_mc2_client(config):
    """Connect to MC-2 for best-effort read-only facets, or return None.

    Used by `cp spine`'s Source-documents facet, which needs a live client that
    `_load_spine_elements` does not expose. Never raises — a facet must never
    break the sweep — so any failure degrades to None (facet omitted).
    """
    return mc2_db.get_client(config, required=False)


def _resolve_project_id_loud(client, code: str, *, consequence: str) -> str | None:
    """`mc2_db._resolve_project_id`, degrading to None on a FAILED lookup —
    but saying so (step 3).

    `_resolve_project_id` already returns None for "nothing matches"; an
    exception is a network/schema failure. Swallowing it silently made the
    caller query only the short-code spelling (missing a drifted project's
    dir-slug rows, #151) or report "no project resolves" for a project that
    exists — partial or false answers that looked complete.
    """
    try:
        return mc2_db._resolve_project_id(client, code)
    except Exception as exc:  # noqa: BLE001 — degrade, but loudly
        click.echo(
            f"(WARNING: project-id lookup for '{code}' failed — {consequence}: "
            f"{type(exc).__name__}: {exc})",
            err=True,
        )
        return None


@click.command("spine")
@click.argument("code")
def spine_cmd(code: str) -> None:
    """Print the project spine's full ranked relevance sweep.

    Reads the spine from MC-2 (canonical); falls back to the on-disk markdown
    frontmatter when MC-2 is unreachable so the command works offline. When
    MC-2 serves the read, appends a 'Source documents' facet listing the
    project's rag_assets and which distilled elements link them.
    """

    from cp_engine.spine import render_source_documents, render_sweep

    config = _cli._load_config_or_die()
    elements, project_dir = _cli._load_spine_elements(config, code)

    click.echo(render_sweep(code, elements, today=tenant_today()))

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

    from cp_engine.estimate import fetch_estimate, fetch_schedule
    from cp_engine.sync import BackendUnavailable
    from cp_engine.where import build_where_report, fetch_substance_status

    config = _cli._load_config_or_die()
    try:
        client = mc2_db.get_client(config)
    except BackendUnavailable as exc:
        click.echo(
            f"cxp where needs MC-2 (no offline mode — it reads the live "
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
            f"No spine substance for '{code}'. `cxp where` resolves the project "
            f"via its substance rows — bind at least one work item "
            f"first, or check the dir-slug code.",
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
        build_where_report(estimate, schedule, substance_by_item, today=tenant_today())
    )


@click.command("spine-stats")
@click.option(
    "--type", "type_filter", default=None, help="Narrow to one deliverable type."
)
@click.option(
    "--within-days", default=14, show_default=True, help="Due-soon window (days)."
)
def spine_stats_cmd(type_filter: str | None, within_days: int) -> None:
    """Cross-project analytics over the spine (Deliverables). RETIRED.

    Reports nothing since mc-2 migration 072 dropped the `spine_elements`
    table these stats were computed from; it now explains that and exits 0
    rather than crashing on PGRST205. See `cp_engine/spine_stats.py` for what
    reconstructing them would require.
    """

    from cp_engine.spine_stats import (
        ELEMENTS_TABLE_RETIRED,
        due_soon,
        stage_distribution,
        type_inventory,
    )
    from cp_engine.sync import BackendUnavailable

    if ELEMENTS_TABLE_RETIRED:
        # Say why, once, instead of printing three "(none)" blocks that read
        # as "your tenant has no deliverables". Exit 0: nothing failed here,
        # the report is retired. See cp_engine/spine_stats.py.
        click.echo(
            "cross-project spine stats are unavailable: these reports were "
            "built on the `spine_elements` table, dropped in mc-2 migration "
            "072. The replacement (`spine_substance`) does not carry the "
            "type / stage / target_date columns they report on, so they need "
            "redesigning rather than repointing. See cp_engine/spine_stats.py.",
            err=True,
        )
        return

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
    soon = due_soon(client, today=tenant_today(), within_days=within_days)

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
    the LLM synthesis, and writes it to MC-2 as an authored `Synthesis`
    element `_authored/<today>-sweep` (idempotent per day — re-running
    overwrites), rendered to `spine/_authored/<today>-sweep.md`. An empty
    spine writes nothing.
    """

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

    today = tenant_today()
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
    # Step 4c: spine/ is rendered from MC-2, so the readout is written to
    # MC-2 as an authored Synthesis element FIRST and its file rendered from
    # those rows — a file dropped into spine/ by hand (what this did before)
    # would be quarantined and removed by the next sync. One element per day:
    # a same-day re-run overwrites v1.
    from cp_engine.authored_element import build_create_rows
    from cp_engine.spine import active_deliverable_ids
    from cp_engine.spine_inbox import _render_element_file
    from cp_engine.sync import _read_mc_id

    # The readout is ABOUT the active work, so it serves the active
    # deliverables — the fresh element scores hot on the next Lens pass via
    # _serves_active_term (rather than scoring cold with serves=[]).
    serves = sorted(active_deliverable_ids(elements))
    try:
        client = mc2_db.get_client(config)
    except Exception as exc:  # noqa: BLE001 — nowhere to write the readout
        click.echo(result.ranked_table)
        click.echo(f"\nSynthesis NOT written — MC-2 unavailable: {exc}", err=True)
        sys.exit(1)
    project_id = _read_mc_id(project_dir / "cp.md") or mc2_db._resolve_project_id(
        client, code)
    if not project_id:
        click.echo(f"Synthesis NOT written — no MC-2 project for {code}.", err=True)
        sys.exit(1)
    est_item_id = f"_authored/{today.isoformat()}-sweep"
    rows = build_create_rows(
        project_id=project_id, project_code=project_dir.name,
        label=f"Whole-project sweep — {today.isoformat()}", type_="Synthesis",
        body=result.synthesis_text, serves=serves, now_iso=today.isoformat(),
    )
    for r in rows:
        r["est_item_id"] = est_item_id
        r["id"] = f"{project_dir.name}/{est_item_id}/{r['version_label']}"
    client.table(mc2_db.Tables.SPINE_SUBSTANCE).upsert(rows, on_conflict="id").execute()
    path = _render_element_file(client, project_id, est_item_id,
                                project_dir=project_dir, writer="cxp sweep",
                                origin="authored")

    click.echo(result.ranked_table)
    click.echo(f"\nSynthesis written: {est_item_id} (MC-2) → "
               f"{path.relative_to(project_dir)}")

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
    from datetime import datetime

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
        click.echo(f"cxp spine-recover needs MC-2: {exc}", err=True)
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
        now_iso=datetime.now(UTC).isoformat(),
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

    # #314 — a distill whose phrases are not its source's is shown BEFORE the
    # write line, so a dry run is where it is caught, not seven weeks later.
    low = [r for r in report if r.get("fidelity_low")]
    if low:
        click.echo(f"\n{len(low)} element(s) FAILED the distill-fidelity check:")
        for r in low:
            click.echo(f"  ! {r['label']} · {r.get('asset') or '?'} — "
                       f"{r.get('fidelity_reason')}")
        click.echo("  Re-distilled bodies land marked machine-derived; "
                   "read these against their source before relying on them.")

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
    from cp_engine.spine import SpineDirNotFound, find_spine_dir, workstream_docs
    from cp_engine.spine_lint import lint_cp_placeholders, lint_spine_rows
    from cp_engine.sync import BackendUnavailable

    config = _cli._load_config_or_die()
    warnings: list[str] = []

    try:
        client = mc2_db.get_client(config)
    except BackendUnavailable as exc:
        click.echo(f"cxp spine-lint needs MC-2 for the spine checks: {exc}",
                   err=True)
        sys.exit(1)

    # Accept the same codes the MCP verbs do (#151): resolve the caller's
    # code to the working dir and query every project_code spelling the
    # spine may be keyed under — short code AND dir-slug. Drifted projects
    # carry live rows under both (the ibx-5153 case), so this is a set,
    # not a translation.
    mc2_id = _resolve_project_id_loud(
        client, code,
        consequence="dir-slug rows and the Source-reviewed check may be missed",
    )
    try:
        spine_dir = find_spine_dir(config.root, code, mc2_id=mc2_id)
    except SpineDirNotFound:
        spine_dir = None
    codes = [code]
    if spine_dir is not None and spine_dir.name != code:
        codes.append(spine_dir.name)

    # ONE assembly, shared with the hosted server (#280). Which tables, which
    # columns, the one-live-per-element discipline and the per-check degradation
    # now live in `run_all_lints`; a second copy here would drift, which is the
    # #172/#178 lesson and the reason project_state.py reuses the engine's own
    # merge rather than restating it.
    from cp_engine.spine_lint import run_all_lints

    cp_md_text = None
    cp_md = None
    try:
        cp_md = (spine_dir or find_spine_dir(config.root, code)) / "cp.md"
        if cp_md.is_file():
            cp_md_text = cp_md.read_text(encoding="utf-8")
    except (SpineDirNotFound, OSError) as exc:
        # The lint still runs on the spine, but "clean" must not imply the
        # cp.md checks (placeholders, Exec Summary budgets) passed.
        click.echo(f"  (cp.md checks skipped — cp.md not readable: {exc})",
                   err=True)

    # Dangling `Source reviewed:` references (#324): the workstream's docs
    # against every title its source store has held. Skipped (not guessed)
    # when the directory or the MC-2 row cannot be found.
    source_titles = None
    if spine_dir is not None and mc2_id:
        try:
            from cp_engine.project_sources import ingested_source_titles

            proj = (
                client.table(mc2_db.Tables.PROJECTS).select("company_id")
                .eq("id", mc2_id).limit(1).execute().data
            ) or []
            source_titles = ingested_source_titles(
                client, mc2_id, (proj[0].get("company_id") if proj else None)
            )
        except Exception as exc:  # noqa: BLE001 — advisory; say it was skipped
            click.echo(f"  (Source reviewed check skipped: {exc})", err=True)

    ws_docs, ws_files = (
        workstream_docs(spine_dir) if spine_dir is not None else (None, set())
    )
    warnings = run_all_lints(client, codes, cp_md_text=cp_md_text,
                             workstream_docs=ws_docs, local_files=ws_files,
                             source_titles=source_titles)
    # Stamp vs. state (#251). Local only — it reads git history, which the
    # hosted server's shallow tree does not have, so it stays out of the
    # shared `run_all_lints`.
    if cp_md_text is not None and cp_md is not None:
        from cp_engine.exec_summary_freshness import partial_refresh_warning

        stale_state = partial_refresh_warning(cp_md)
        if stale_state:
            warnings.append(stale_state)


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
        click.echo(f"cxp seal-sweep needs MC-2: {exc}", err=True)
        sys.exit(1)

    # Same dual-spelling resolution as spine-lint (#151): a drifted project
    # carries live rows under both the short code and the dir slug.
    mc2_id = _resolve_project_id_loud(
        client, code, consequence="rows under the dir-slug spelling may be missed",
    )
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
        today=today.date() if today else tenant_today(),
        within_days=within if within is not None else DEFAULT_SHIPPED_WITHIN_DAYS,
        all_deliverables=all_deliverables,
    )
    click.echo(render_sweep(rounds, code=code))


@click.command("stub-sweep")
@click.argument("code")
@click.option(
    "--verify", "verify_target", metavar="EST_ITEM_ID", default=None,
    help="Check whether the stubs routed to this target are safe to retire: "
         "every source already attached there, and no stub carrying a typed "
         "edge. Checks BOTH — a source-only check passes a card whose edge "
         "the retire would destroy (#276).",
)
def stub_sweep_cmd(code: str, verify_target: str | None = None) -> None:
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
        click.echo(f"cxp stub-sweep needs MC-2: {exc}", err=True)
        sys.exit(1)

    mc2_id = _resolve_project_id_loud(
        client, code, consequence="rows under the dir-slug spelling may be missed",
    )
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

    # The DOCUMENTS' arrival dates, which is what `postdates_target` must
    # compare against — NOT the stub rows' `version_date`, which a backfill
    # sets to the backfill date (#274). Best-effort: without it the check
    # stays silent rather than reporting a date it did not measure.
    source_dates: dict[str, str] = {}
    # Only uuid ids name a `rag_assets` row. A hand-authored source's id is
    # an `_authored/<slug>` key; one of those in the `in_` made PostgREST
    # answer 22P02 for the WHOLE lookup, which the except below swallowed —
    # so on ibx-5153 the arrived-after check was silent for every stub.
    from cp_engine.stub_sweep import rag_asset_ids

    asset_ids = rag_asset_ids(rows)
    if asset_ids:
        try:
            for a in (
                client.table(mc2_db.Tables.RAG_ASSETS)
                .select("id, created_at")
                .in_("id", asset_ids)
                .execute()
                .data
            ) or []:
                created = str(a.get("created_at") or "")
                if a.get("id") and created:
                    source_dates[str(a["id"])] = created[:10]
        except Exception:  # noqa: BLE001 — advisory; a failed lookup must not
            # break the sweep, it just silences one check.
            click.echo(
                "  (could not resolve source arrival dates; "
                "the arrived-after check is silent)", err=True
            )

    stubs = find_stubs(rows, relations, source_dates=source_dates)

    if verify_target:
        from cp_engine.stub_sweep import verify_transfer

        target = next(
            (r for r in rows if r.get("est_item_id") == verify_target), None
        )
        if target is None:
            click.echo(
                f"No live element {verify_target!r} in this project.", err=True
            )
            sys.exit(1)
        click.echo(
            verify_transfer(
                stubs,
                verify_target,
                list(target.get("sources") or []),
                relations=relations,
            )
        )
        return

    click.echo(render_sweep(stubs, code=code))


@click.command("feeds-sweep")
@click.argument("code")
def feeds_sweep_cmd(code: str) -> None:
    """The feeds edges this project's routing implies (#174).

    A source routed to a deliverable's slot, or to an activity that already
    feeds a deliverable, is proposed as `informs` -> that deliverable — unless
    it arrived AFTER the work it was routed to (#270), or the pair already has
    an edge in any status (a dismissed proposal stays dismissed).

    READ-ONLY. Routing proposes these at the moment it happens, as `proposed`
    edges a human confirms in the Suggestions inbox; this reports what the
    rule sees across the whole project, for a backlog the routing-time hook
    never saw.
    """
    from cp_engine.feeds_propose import fetch_inputs, propose_feeds, render_report
    from cp_engine.sync import BackendUnavailable

    config = _cli._load_config_or_die()
    try:
        client = mc2_db.get_client(config)
    except BackendUnavailable as exc:
        click.echo(f"cxp feeds-sweep needs MC-2: {exc}", err=True)
        sys.exit(1)
    project_id = _resolve_project_id_loud(
        client, code, consequence="cannot sweep",
    )
    if not project_id:
        click.echo(f"No project resolves for '{code}'.", err=True)
        sys.exit(1)
    rows, relations, source_dates = fetch_inputs(client, project_id)
    if not rows:
        click.echo(f"No live spine for '{code}'.", err=True)
        sys.exit(1)
    proposals, skipped = propose_feeds(rows, relations, source_dates=source_dates)
    click.echo(render_report(proposals, skipped, code=code))


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

    Also reports a stamp that overstates the summary (#251): Where it stands,
    Next up and Blockers all last changed 14+ days before `· updated`, read
    from the file's git history. Silent when there is no full history.
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
    # Stamp vs. state (#251): a one-field refresh advances the stamp for the
    # whole summary. Read from git history; silent when it cannot tell.
    from cp_engine.exec_summary_freshness import partial_refresh_warning

    stale_state = partial_refresh_warning(cp_md)
    if stale_state:
        warnings.append(stale_state)
    if warnings:
        click.echo(f"{code} — {len(warnings)} exec-summary warning(s):")
        for w in warnings:
            click.echo(f"  {w}")
    else:
        click.echo(f"{code} — exec-summary within budget.")


@click.command("wrap")
@click.argument("code")
@click.option(
    "--bundle",
    is_flag=True,
    help="Emit the wrap-report bundle as JSON for in-session synthesis.",
)
@click.option(
    "--facts-docx",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Also write the FACTS as a Word doc at this path (the readable "
    "half; the authored prose is added by /cp-wrap).",
)
@click.option(
    "--tail-days",
    default=14,
    show_default=True,
    help="Closing-window width used for the meeting tail-share signal.",
)
def wrap_cmd(
    code: str, bundle: bool, facts_docx: Path | None, tail_days: int
) -> None:
    """Emit the deterministic raw material for a close-out wrap report.

    Gathers the facts a hand-written retro forgets to look up — duration,
    budget, RECORDED HOURS by person, meeting cadence and where it fell in
    the timeline, deliverables, what each shipped round absorbed, open
    commitments — and prints them as JSON for the model to synthesize
    against a fixed section contract (`/cp-wrap`).

    Facts only: this verb writes no prose and mutates nothing. The report
    itself is authored in-session so it can be defended and revised live —
    the same split as `cp prep-planning --bundle`.

    Run it before the terminal Exec Summary is written, so that summary can
    quote the report.
    """
    import json

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
        "generated": tenant_today().isoformat(),
    }

    # Word output: the FACTS half only. The authored prose comes from
    # /cp-wrap, which re-invokes the same builder with its sections — a
    # facts-only doc is still useful on its own (it is the scope
    # conversation's evidence), and it keeps this verb prose-free.
    if facts_docx is not None:
        from cp_engine.wrap_docx import (
            WrapSection,
            build_wrap_docx,
            effort_table,
            facts_table,
        )

        sections = [WrapSection("Project facts", table=facts_table(payload))]
        eff_rows = effort_table(payload)
        if eff_rows:
            sections.append(WrapSection(
                "Effort by person",
                body="Allocated hours from MC-2's planning record — NOT "
                     "timesheet actuals.",
                table=eff_rows,
            ))
        if payload["not_assessable_from_data"]:
            sections.append(WrapSection(
                "Not assessable from data — fill these in",
                body="These are judgement calls and commercial facts the "
                     "system cannot know. Left blank deliberately rather "
                     "than guessed.",
                blanks=payload["not_assessable_from_data"],
            ))
        written = build_wrap_docx(
            title=f"{b.name or b.code} — Wrap Report",
            subtitle=f"{b.code} · facts as of {payload['generated']}",
            sections=sections,
            out_path=facts_docx,
        )
        click.echo(f"Facts written: {written}")

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


@click.command("card-kinds")
@click.argument("code", required=False)
@click.option("--apply", "do_apply", is_flag=True,
              help="Write the plan. Without this, prints what would change.")
def card_kinds_cmd(code: str | None, do_apply: bool) -> None:
    """Persist `card_kind` where it follows from structure (#179 / mig 140).

    `card_class.classify()` has always read a `card_kind` column that did not
    exist until mig 140, so every row inferred forever and the module's
    "migration aid with a known end date" could never end. This sets the
    column for rows whose kind is STRUCTURAL — the engagement element, the
    Deliverables layer, and item-vs-context placement — and leaves genuinely
    ambiguous rows NULL so `is_ambiguous()` keeps meaning something.

    Dry-run by default. Pass CODE to scope to one project.
    """
    from cp_engine.card_kind_write import run

    config = _cli._load_config_or_die()

    try:
        plan, written = run(config=config, project_code=code, apply=do_apply)
    except Exception as exc:  # noqa: BLE001
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    for kind, n in sorted(plan.counts.items(), key=lambda kv: -kv[1]):
        click.echo(f"  {kind:<12} {n:>4}")
    cards = sum(n for k, n in plan.counts.items() if k != "attachment")
    click.echo(f"\n  {cards} cards, {plan.counts.get('attachment', 0)} attachments")

    if plan.already_set:
        click.echo(f"  {plan.already_set} already set (skipped)")
    if plan.left_null:
        click.echo(f"  {len(plan.left_null)} left NULL — genuinely ambiguous:")
        for row_id, why in plan.left_null[:10]:
            click.echo(f"    {row_id} — {why}")
        if len(plan.left_null) > 10:
            click.echo(f"    … and {len(plan.left_null) - 10} more")

    if do_apply:
        click.echo(f"\nWrote card_kind on {written} rows.")
    elif plan.to_set:
        click.echo(f"\nDry run — {len(plan.to_set)} rows would change. "
                   "Re-run with --apply to write.")
    else:
        click.echo("\nNothing to do.")


