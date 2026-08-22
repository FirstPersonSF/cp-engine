"""One-shot layout migrations: migrate-to-v03, migrate-projects-flat, migrate-accounts.

Split from cli.py (arch-phase-4, #33). Shared helpers stay in
cp_engine.cli and are called via the module attribute so test
monkeypatches of `cp_engine.cli.<helper>` keep working.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from cp_engine.config import ConfigError, load
from cp_engine.sync import SyncError


@click.command(name="migrate-to-v03")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would move without touching the disk.",
)
def migrate_to_v03(dry_run: bool) -> None:
    """One-shot move of v0.2's flat `projects/<code>.md` layout to v0.3's
    per-project working dirs at `<scope>/projects/<code>/cp.md`.

    Uses `git mv` to preserve rename detection. Refuses to run on a dirty
    working tree — commit or stash first. After this runs cleanly, regular
    `cxp sync` keeps using the new layout."""
    try:
        config = load(Path.cwd())
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    from cp_engine.migrate import MigrationError
    from cp_engine.migrate import migrate_to_v03 as _run

    try:
        result = _run(config, dry_run=dry_run)
    except MigrationError as exc:
        click.echo(f"Migration aborted: {exc}", err=True)
        sys.exit(1)
    except SyncError as exc:
        click.echo(f"Migration aborted: {exc}", err=True)
        sys.exit(1)

    label = "Would move" if dry_run else "Moved"
    if not result.moved and not result.skipped:
        click.echo("Nothing to migrate — no v0.2 layout files found.")
        return

    click.echo(f"{label} {len(result.moved)} file(s):")
    for src, dst in result.moved:
        click.echo(f"  {src.relative_to(config.root)} → {dst.relative_to(config.root)}")
    if result.skipped:
        click.echo(f"\nSkipped {len(result.skipped)} file(s) — resolve by hand:")
        for src, reason in result.skipped:
            click.echo(f"  {src.relative_to(config.root)}: {reason}")

    if not dry_run:
        click.echo(
            "\nReview the changes, then commit:\n"
            "  git status\n"
            '  git commit -m "cp-engine v0.3: restructure to per-project working directories"'
        )


@click.command(name="migrate-projects-flat")
@click.option(
    "--tenant-root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Tenant root (default: current directory).",
)
def migrate_projects_flat_cmd(tenant_root: Path | None) -> None:
    """One-shot move of pre-v0.7's `<scope>/projects/<dir>` layout to v0.7's
    `<scope>/<dir>` layout.

    Uses `git mv` to preserve rename detection. Refuses to run on a dirty
    working tree — commit or stash first. Idempotent: re-running on an
    already-migrated tree is a no-op. After this runs cleanly, regular
    `cp sync` keeps using the new layout.

    Also rewrites `.cp-link` files in linked source repos so they point
    at the new working-dir paths."""
    from cp_engine.migrate_flat import MigrateFlatError, migrate_projects_flat

    root = tenant_root or Path.cwd()
    try:
        result = migrate_projects_flat(root)
    except MigrateFlatError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    if result.no_op:
        click.echo("Nothing to migrate (tree already matches v0.7 layout).")
        return

    for src, dst in result.moved_dirs:
        click.echo(f"  moved: {src.relative_to(root)} → {dst.relative_to(root)}")
    for path in result.removed_projects_dirs:
        click.echo(f"  removed empty: {path.relative_to(root)}")
    for path in result.rewrote_cp_links:
        click.echo(f"  rewrote .cp-link: {path}")
    click.echo(
        f"\nDone. Review the staged changes (`git status`) and commit when "
        f"satisfied. Source-repo .cp-link files are not under git control "
        f"so they were updated in place."
    )


@click.command(name="migrate-accounts")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Preview the moves and residue without touching the disk.",
)
def migrate_accounts_cmd(dry_run: bool) -> None:
    """One-shot move of flat `1p/<dir>/` layout to account-nested
    `1p/<company>/<dir>/` layout (Phase 2 of the account restructure).

    Reads MC-2 once to resolve each flat dir's company, `git mv`s into
    `1p/<company-slug>/`, absorbs `_teleflex.md` (if present) into
    `1p/teleflex/cp.md` under a dated "Legacy notes" heading, then runs
    `cp sync` so account `cp.md` files get scaffolded and master-cp
    re-renders with the new Account column.

    Refuses to run on a dirty working tree. Hard-fails if any flat dir
    can't be matched against MC-2 (fix MC-2 or hand-move the dir, then
    re-run). Idempotent: re-running on an already-migrated tree is a
    no-op.

    Doesn't commit; review with `git status` / `git diff` and commit
    yourself."""
    from cp_engine.migrate_accounts import (
        MigrateAccountsError,
        migrate_accounts,
    )

    root = Path.cwd()
    try:
        result = migrate_accounts(root, dry_run=dry_run)
    except MigrateAccountsError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    if result.unresolved_dirs:
        click.echo(
            f"Cannot proceed: {len(result.unresolved_dirs)} dir(s) under 1p/ "
            f"don't match any MC-2 project.", err=True,
        )
        for path in result.unresolved_dirs:
            click.echo(f"  {path.relative_to(root)}", err=True)
        click.echo(
            "\nFix MC-2 (add the project) or hand-move the dir, then re-run.",
            err=True,
        )
        sys.exit(1)

    if result.no_op:
        click.echo("Nothing to migrate (1p/ already matches account-nested layout).")
        return

    label = "Would move" if dry_run else "Moved"
    click.echo(f"{label} {len(result.planned_moves)} dir(s):")
    for old, new in result.planned_moves:
        click.echo(f"  {old.relative_to(root)} → {new.relative_to(root)}")

    if result.absorbed_teleflex:
        verb = "Would absorb" if dry_run else "Absorbed"
        click.echo(f"\n{verb} _teleflex.md into 1p/teleflex/cp.md.")

    if dry_run:
        click.echo(
            "\nWill run: cxp sync (scaffolds account cp.md files, "
            "re-renders master-cp)"
        )
        if result.residue:
            click.echo("\nResidue — these will need hand-fixing post-migration:")
            for hint in result.residue:
                click.echo(f"  {hint}")
        click.echo(
            "\nDry-run — nothing changed. Re-run without --dry-run to execute."
        )
        return

    if result.sync_files_written:
        click.echo(f"\nSync wrote {len(result.sync_files_written)} file(s).")

    if result.residue:
        click.echo("\nResidue — hand-fix these:")
        for hint in result.residue:
            click.echo(f"  {hint}")

    click.echo("\nDone. Review with `git status` / `git diff`, then commit.")


