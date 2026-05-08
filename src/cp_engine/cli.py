"""The `cp` CLI — entry point for tenant operations.

Subcommands per spec v02 §2.1:
  cp init     → walk through populating .cp-engine.local.toml
  cp sync     → run one sync cycle locally (same logic as the Action)
  cp render   → re-render generated files (master-cp.md, CLAUDE.md)
  cp status   → show what would change without writing
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from cp_engine.config import CommittedConfigMissing
from cp_engine.init import InitAborted, run_init


@click.group()
@click.version_option(package_name="cp-engine")
def main() -> None:
    """Context Protocol Engine — tenant operations."""


@main.command()
@click.option(
    "--non-interactive",
    is_flag=True,
    help="Don't prompt; mark every committed project as skipped (useful in CI).",
)
def init(non_interactive: bool) -> None:
    """Walk through populating .cp-engine.local.toml interactively."""
    try:
        run_init(Path.cwd(), interactive=not non_interactive)
    except CommittedConfigMissing as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)
    except InitAborted as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    except KeyboardInterrupt:
        click.echo("\nAborted. Partial progress (if any) has been saved.", err=True)
        sys.exit(130)


@main.command()
@click.option("--dry-run", is_flag=True, help="Show diff without writing")
def sync(dry_run: bool) -> None:
    """Run one sync cycle for the current tenant."""
    click.echo(f"cp sync (dry-run={dry_run}) — not implemented yet (lands in v0.1)")
    sys.exit(1)


@main.command()
@click.option("--force-weekly", is_flag=True, help="Re-render weekly-cp.md from template")
def render(force_weekly: bool) -> None:
    """Re-render generated files (master-cp.md, CLAUDE.md)."""
    click.echo(f"cp render (force-weekly={force_weekly}) — not implemented yet (lands in v0.1)")
    sys.exit(1)


@main.command()
def status() -> None:
    """Show what would change on next sync (no writes)."""
    click.echo("cp status — not implemented yet (lands in v0.1)")
    sys.exit(1)


if __name__ == "__main__":
    main()
