"""1P asset-ingest verbs: ingest-assets, archive-project-assets.

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
@click.option(
    "--allow-empty",
    "allow_empty",
    is_flag=True,
    help="Proceed (exit 0) when the project has no Drive/Dropbox folder "
    "configured, instead of refusing. For scripted use. Single-project only "
    "(--all already skips unconfigured projects with a note).",
)
def ingest_assets_cmd(
    code: str | None,
    all_: bool,
    scope: str | None,
    no_cache: bool,
    folder: str | None,
    allow_empty: bool,
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

    if allow_empty and all_:
        click.echo(
            "Error: --allow-empty is single-project only; --all already skips "
            "unconfigured projects with a note.",
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
        # Confirm gate (#59): an explicitly-requested single-project ingest with
        # no folder configured refuses rather than recording a normal-looking
        # empty run. --allow-empty downgrades the refusal to a note (exit 0)
        # for scripted callers that sweep codes regardless of config state.
        if run.unconfigured_reason:
            if not allow_empty:
                click.echo(
                    f"Error: {code} has no Drive/Dropbox folder configured — "
                    "set folders in MC-2 or pass --allow-empty to proceed. "
                    f"({run.unconfigured_reason})",
                    err=True,
                )
                sys.exit(1)
            click.echo(f"{code}: SKIPPED — {run.unconfigured_reason}")
            return
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
        if outcome.unconfigured:
            # Confirm gate (#59), sweep shape: never hard-fail the fan-out for
            # a config gap — skip the project with a VISIBLE per-project note.
            click.echo(f"{outcome.code}: SKIPPED — {outcome.unconfigured}")
            continue
        _echo_run_summary(outcome.code, outcome)

    click.echo(
        f"TOTAL ({len(result.outcomes)} projects): "
        f"created={result.total_created} versioned={result.total_versioned} "
        f"skipped={result.total_skipped} deduped={result.total_deduped} "
        f"superseded={result.total_superseded} "
        f"unchanged={result.total_skipped_unchanged} "
        f"shortcuts={result.total_skipped_shortcuts} "
        f"failed={result.total_failed} "
        f"unconfigured={result.total_unconfigured}"
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


