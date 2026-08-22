"""The `cxp` CLI — entry point for tenant operations.

Named `cp` until 2026-08-22; renamed because it shadowed /bin/cp on PATH.

Subcommands per spec v02 §2.1:
  cxp init     → walk through populating .cp-engine.local.toml
  cxp sync     → run one sync cycle locally (same logic as the Action)
  cxp render   → re-render generated files (master-cp.md, CLAUDE.md)
  cxp status   → show what would change without writing
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from cp_engine.config import CommittedConfigMissing, ConfigError, load

# Seam re-export: cli_cmds/spine.py calls _cli.fetch_project_assets and tests
# monkeypatch cp_engine.cli.fetch_project_assets — keep it importable here.
from cp_engine.spine_sources import fetch_project_assets  # noqa: F401


@click.group()
@click.version_option(package_name="cp-engine")
def main() -> None:
    """Context Protocol Engine — tenant operations."""


def _load_spine_elements(config, code: str):
    """Load a project's spine elements (MC-2 canonical, disk fallback).

    Returns ``(elements, project_dir)`` where ``project_dir`` is the resolved
    working dir if the disk path was used, else ``None`` (MC-2 served the read).
    Exits 1 via ``SpineDirNotFound`` if the project can't be resolved on disk
    during fallback. Shared by ``cp spine`` and ``cp sweep``.
    """
    from cp_engine import mc2_db
    from cp_engine.spine import (
        SpineDirNotFound,
        find_spine_dir,
        load_spine,
        load_spine_from_mc2,
    )
    from cp_engine.sync import BackendUnavailable

    elements: tuple = ()
    project_dir = None
    reached_mc2 = False
    served_by_mc2 = False  # connected AND query succeeded — MC-2 is the truth source

    try:
        client = mc2_db.get_client(config)  # lightweight: client only, no read
        reached_mc2 = True
    except BackendUnavailable as exc:
        click.echo(f"(MC-2 unavailable: {exc})", err=True)
    except Exception as exc:  # noqa: BLE001 — unexpected connect failure, still degrade
        click.echo(f"(WARNING: MC-2 connect failed unexpectedly: {exc})", err=True)

    if reached_mc2:
        try:
            elements = load_spine_from_mc2(client, code)
            served_by_mc2 = True  # an empty result is still the authoritative answer
        except Exception as exc:  # noqa: BLE001 — connected but query failed = likely a bug
            click.echo(
                f"(WARNING: MC-2 spine read failed — this may be a schema/query bug: {exc})",
                err=True,
            )

    # Reads treat MC-2 as the source of truth. We only touch disk when MC-2 did
    # not serve the read — and when we do, we say so loudly: the result is
    # last-known, unverified state, NOT the authoritative spine.
    if not served_by_mc2:
        click.echo(
            "(MC-2 unreachable — showing last-known markdown-derived state, "
            "unverified.)",
            err=True,
        )
        try:
            project_dir = find_spine_dir(config.root, code)
        except SpineDirNotFound as exc:
            click.echo(str(exc), err=True)
            sys.exit(1)
        elements = load_spine(project_dir)
    elif not elements:
        # MC-2 served the read but the project has no spine elements yet — say
        # so, so an empty render isn't mistaken for a failed/unreachable read.
        click.echo(f"(MC-2 returned 0 spine elements for {code}.)", err=True)

    return elements, project_dir


# ──────────────────────────────────────────────────────────────────────
#  1P asset-ingest verbs (Task C8)
#
#  Thin command bodies — all real work lives in `cp_engine.asset_ingest`
#  (the glue) and `cp_engine.asset_ingest_cli` (client build + --all
#  enumeration + summary formatting). Verbs that need a Supabase client
#  build one via `build_mc2_client`, which reuses the same cred resolver the
#  glue uses internally so the CLI and glue always agree.
# ──────────────────────────────────────────────────────────────────────


def build_mc2_client():
    """Build the MC-2 Supabase client (re-exported for monkeypatch in tests)."""
    from cp_engine.asset_ingest_cli import build_mc2_client as _build

    return _build()


def _load_config_or_die() -> "TenantConfig":  # noqa: F821
    """Load tenant config from cwd; exit with a friendly error on failure."""
    try:
        return load(Path.cwd())
    except CommittedConfigMissing:
        click.echo(
            "No .cp-engine.toml in this directory. Run `cxp init` "
            "or cd into the cp tenant root.",
            err=True,
        )
        sys.exit(1)
    except ConfigError as exc:
        click.echo(f"Config error: {exc}", err=True)
        sys.exit(1)




# ──────────────────────────────────────────────────────────────────────
#  Command registration (arch-phase-4, #33)
#
#  Implementations live in cli_cmds/*; names and behavior are identical
#  to the pre-split flat CLI. Imports sit at the bottom so the submodules
#  can `import cp_engine.cli` for the shared helpers above without a
#  circular-import failure.
# ──────────────────────────────────────────────────────────────────────

from cp_engine.cli_cmds import (  # noqa: E402
    assets as _assets_mod,
)
from cp_engine.cli_cmds import (
    core as _core_mod,
)
from cp_engine.cli_cmds import (
    fathom as _fathom_mod,
)
from cp_engine.cli_cmds import (
    ingest as _ingest_mod,
)
from cp_engine.cli_cmds import (
    migrate as _migrate_mod,
)
from cp_engine.cli_cmds import (
    parse as _parse_mod,
)
from cp_engine.cli_cmds import (
    planning as _planning_mod,
)
from cp_engine.cli_cmds import (
    session as _session_mod,
)
from cp_engine.cli_cmds import (
    slack as _slack_mod,
)
from cp_engine.cli_cmds import (
    spine as _spine_mod,
)
from cp_engine.cli_cmds import (
    workshop as _workshop_mod,
)

for _mod in (
    _core_mod, _migrate_mod, _session_mod, _parse_mod, _spine_mod,
    _ingest_mod, _fathom_mod, _slack_mod, _planning_mod, _assets_mod,
    _workshop_mod,
):
    for _obj in vars(_mod).values():
        if isinstance(_obj, click.Command) and not isinstance(_obj, click.Group):
            main.add_command(_obj)

# Back-compat import paths: tests (and possibly external callers) import
# these from cp_engine.cli; the implementations moved to cli_cmds/*.
_write_drift_flags = _spine_mod._write_drift_flags
_fetch_clickup_task_ids_for_hashes = _planning_mod._fetch_clickup_task_ids_for_hashes


if __name__ == "__main__":
    main()
