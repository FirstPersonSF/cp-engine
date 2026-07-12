"""1P asset-ingest verbs: ingest-assets, meetings-backfill helpers, promote/demote/list/archive/unarchive.

Split from cli.py (arch-phase-4, #33). Shared helpers stay in
cp_engine.cli and are called via the module attribute so test
monkeypatches of `cp_engine.cli.<helper>` keep working.
"""

from __future__ import annotations

import sys

import click

import cp_engine.cli as _cli


def _echo_run_summary(code: str, run) -> None:
    """Print one project's ingest summary line + any per-file failures."""
    click.echo(
        f"{code}: created={run.created} versioned={run.versioned} "
        f"skipped={run.skipped} deduped={run.deduped} "
        f"superseded={run.superseded} "
        f"unchanged={run.skipped_unchanged} shortcuts={run.skipped_shortcuts} "
        f"failed={run.failed}"
    )
    for name, err in run.failures:
        click.echo(f"  FAIL {name}: {err}", err=True)


@click.command(name="ingest-assets")
@click.argument("code", required=False)
@click.option("--all", "all_", is_flag=True, help="Ingest all active client projects.")
@click.option(
    "--scope",
    type=click.Choice(["1p", "fpsf", "canonic"]),
    help="Narrow --all to a tenant scope. fpsf/canonic are internal "
    "(asset ingest is client-only) → no-op.",
)
@click.option(
    "--no-cache",
    "no_cache",
    is_flag=True,
    help="Bypass the ingest cache: re-scan every file even if its provider "
    "change-token is unchanged since the last ingest (a full re-ingest).",
)
@click.option(
    "--folder",
    default=None,
    help="Scan only this configured ingest folder (narrows the allowlist; "
    "must be a configured folder, else scans nothing). Single-project only.",
)
def ingest_assets_cmd(
    code: str | None,
    all_: bool,
    scope: str | None,
    no_cache: bool,
    folder: str | None,
) -> None:
    """Ingest a project's Drive/Dropbox assets into the asset store.

    `cp ingest-assets ibx-5153` ingests one engagement; `cp ingest-assets
    --all` fans out across every active client project. Exactly one of CODE
    or --all is required. Exits non-zero if any file (or project) failed, so
    cron/CI notices.

    --scope mapping: 1p == --all (client engagements); fpsf/canonic are
    internal and yield nothing (asset ingest is client-only).
    """
    from cp_engine import asset_ingest, asset_ingest_cli

    use_cache = not no_cache
    # Normalize empty --folder to None so `--folder ""` behaves identically to
    # omitting it (an empty string would otherwise reach _effective_allowlist as
    # a no-op fragment — harmless but surprising).
    folder = folder or None

    if bool(code) == bool(all_):
        click.echo(
            "Error: pass exactly one of CODE or --all (got both or neither).",
            err=True,
        )
        sys.exit(2)

    if folder and all_:
        click.echo(
            "Error: --folder is single-project only; cannot combine with --all.",
            err=True,
        )
        sys.exit(2)

    # ── Single project ──
    if code:
        # Clear the in-process listing cache once at the start of this CLI run
        # (matches fan_out_ingest's once-per-invocation clear). For a single
        # project this is mostly hygiene — the one project walks each folder once
        # — but it keeps the "clean per CLI run" contract uniform across both
        # entry points.
        asset_ingest._clear_listing_cache()
        run = asset_ingest.ingest_project_assets(
            code, use_cache=use_cache, only_folder=folder
        )
        if not run.project_found:
            click.echo(f"Error: no MC-2 project resolved for '{code}'.", err=True)
            sys.exit(1)
        _echo_run_summary(code, run)
        if run.failed or run.failures:
            sys.exit(1)
        return

    # ── --all (optionally scoped) ──
    if scope in asset_ingest_cli.INTERNAL_SCOPES:
        click.echo(f"scope={scope}: asset ingest is client-only; nothing to do.")
        return

    # Enumeration goes through the canonical read_projects path (config-driven);
    # the per-project fan-out work still needs an MC-2 client.
    config = _cli._load_config_or_die()
    codes = asset_ingest_cli.active_ingestable_codes(config)
    if not codes:
        click.echo("No active client projects found; nothing to do.")
        return

    client = _cli.build_mc2_client()
    result = asset_ingest_cli.fan_out_ingest(client, codes, use_cache=use_cache)
    for outcome in result.outcomes:
        if outcome.error:
            click.echo(f"{outcome.code}: ERROR {outcome.error}", err=True)
            continue
        _echo_run_summary(outcome.code, outcome)

    click.echo(
        f"TOTAL ({len(result.outcomes)} projects): "
        f"created={result.total_created} versioned={result.total_versioned} "
        f"skipped={result.total_skipped} deduped={result.total_deduped} "
        f"superseded={result.total_superseded} "
        f"unchanged={result.total_skipped_unchanged} "
        f"shortcuts={result.total_skipped_shortcuts} "
        f"failed={result.total_failed}"
    )
    if result.any_failures:
        sys.exit(1)


def _resolve_project_id_or_die(client, code: str) -> str:
    """Resolve a cp code to its MC-2 project_id; exit non-zero on miss."""
    from cp_engine import asset_ingest

    folders = asset_ingest.resolve_project_folders(client, code)
    if folders is None:
        click.echo(f"Error: no MC-2 project resolved for '{code}'.", err=True)
        sys.exit(1)
    return folders.project_id


@click.command(name="promote-asset")
@click.argument("asset_id")
def promote_asset_cmd(asset_id: str) -> None:
    """Promote a project-scoped asset to account scope (shared across the company)."""
    from cp_engine import asset_ingest

    client = _cli.build_mc2_client()
    if asset_ingest.promote_asset(client, asset_id):
        click.echo(f"Promoted {asset_id}")
    else:
        click.echo(f"{asset_id} already account-scoped (no-op)")


@click.command(name="demote-asset")
@click.argument("asset_id")
def demote_asset_cmd(asset_id: str) -> None:
    """Demote an account-scoped asset back to project scope."""
    from cp_engine import asset_ingest

    client = _cli.build_mc2_client()
    if asset_ingest.demote_asset(client, asset_id):
        click.echo(f"Demoted {asset_id}")
    else:
        click.echo(f"{asset_id} not account-scoped (no-op)")


@click.command(name="list-promotable")
@click.argument("code")
def list_promotable_cmd(code: str) -> None:
    """List a project's project-scoped assets eligible for promotion review."""
    from cp_engine import asset_ingest

    client = _cli.build_mc2_client()
    project_id = _resolve_project_id_or_die(client, code)
    rows = asset_ingest.list_promotable(client, project_id)
    if not rows:
        click.echo("(no promotable assets)")
        return
    for row in rows:
        click.echo(
            f"{row.get('id')}  "
            f"{row.get('title') or '(untitled)'}  "
            f"[{row.get('classifier_decision') or '—'}]"
        )


@click.command(name="archive-project-assets")
@click.argument("code")
def archive_project_assets_cmd(code: str) -> None:
    """Archive a project's un-promoted assets (on project close)."""
    from cp_engine import asset_ingest

    client = _cli.build_mc2_client()
    project_id = _resolve_project_id_or_die(client, code)
    n = asset_ingest.archive_project_assets(client, project_id)
    click.echo(
        f"Archived {n} project-scoped assets for {code} "
        "(account assets unaffected)"
    )


@click.command(name="assets-dedupe")
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    help="Execute the plan (default is a pure-read dry run).",
)
def assets_dedupe_cmd(apply_: bool) -> None:
    """Clean up same-title duplicate assets tenant-wide (#57 backlog).

    Finds groups of ACTIVE rag_assets sharing an owner + title
    (case-insensitive), keeps the newest, chains it to its predecessors via
    prev_asset_id, and retires the older copies (status='superseded' +
    chunks/embeddings deleted). Groups whose older copy is referenced by a
    spine element's `sources` are BLOCKED: reported, never touched.

    DRY-RUN BY DEFAULT — prints the plan and exits. Pass --apply to execute.
    """
    from cp_engine import asset_dedupe

    client = _cli.build_mc2_client()
    assets = asset_dedupe.fetch_active_assets(client)
    referenced = asset_dedupe.fetch_spine_referenced_asset_ids(client)
    groups = asset_dedupe.plan_dedupe(assets, referenced)

    if not groups:
        click.echo("No same-title duplicate groups found. Nothing to do.")
        return

    actionable = [g for g in groups if not g.blocked and g.losers]
    blocked = [g for g in groups if g.blocked]

    for g in groups:
        tag = "BLOCKED" if g.blocked else "group"
        click.echo(
            f"[{tag}] {g.title!r} ({g.owner_col}={g.owner_id}) — "
            f"{1 + len(g.losers) + len(g.blocked_refs)} copies"
        )
        click.echo(
            f"  keep   {g.keeper.get('id')}  created {g.keeper.get('created_at')}"
        )
        for r in g.losers:
            click.echo(
                f"  retire {r.get('id')}  created {r.get('created_at')}"
            )
        for r in g.blocked_refs:
            click.echo(
                f"  HOLD   {r.get('id')}  created {r.get('created_at')} "
                "— referenced by spine sources; rebind the element first"
            )

    click.echo(
        f"\n{len(actionable)} actionable group(s), {len(blocked)} blocked "
        f"group(s), {sum(len(g.losers) for g in actionable)} asset(s) to retire."
    )
    if not apply_:
        click.echo("Dry run — nothing changed. Re-run with --apply to execute.")
        return

    counts = asset_dedupe.apply_dedupe(client, groups)
    click.echo(
        f"Applied: {counts['groups']} group(s) cleaned, "
        f"{counts['retired']} asset(s) retired, "
        f"{counts['chained']} keeper(s) chained, "
        f"{counts['blocked']} group(s) skipped (spine-referenced)."
    )


@click.command(name="unarchive-project-assets")
@click.argument("code")
def unarchive_project_assets_cmd(code: str) -> None:
    """Restore a project's archived assets back to project scope."""
    from cp_engine import asset_ingest

    client = _cli.build_mc2_client()
    project_id = _resolve_project_id_or_die(client, code)
    n = asset_ingest.unarchive_project_assets(client, project_id)
    click.echo(f"Restored {n} archived assets for {code}")


