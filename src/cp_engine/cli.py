"""The `cxp` CLI — entry point for tenant operations.

Named `cp` until 2026-08-22; renamed because it shadowed /bin/cp on PATH.

Subcommands per spec v02 §2.1:
  cxp init     → walk through populating .cp-engine.local.toml
  cxp sync     → run one sync cycle locally (same logic as the Action)
  cxp render   → re-render generated files (master-cp.md, CLAUDE.md)
  cxp status   → show what would change without writing
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from cp_engine.config import CommittedConfigMissing, ConfigError, load

# Seam re-export: cli_cmds/spine.py calls _cli.fetch_project_assets and tests
# monkeypatch cp_engine.cli.fetch_project_assets — keep it importable here.
from cp_engine.spine_sources import fetch_project_assets  # noqa: F401

# Exit status for "MC-2 refused the Supabase key". Distinct from 1 (general
# failure) and 2 (usage error) so scripts can tell a credential problem apart.
EXIT_SUPABASE_KEY_REJECTED = 3


def _key_rejected_line(reason: str) -> str:
    from cp_engine import mc2_db

    source = mc2_db.creds_source() or "an unknown source"
    # Belt and braces: the reason is server text, but never let any part of
    # the key reach the terminal.
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")
    if key and len(key) >= 8 and key in reason:
        reason = reason.replace(key, "<redacted>")
    return (
        f"cxp: MC-2 rejected the Supabase key ({reason}) — it came from "
        f"{source}. Use the new sb_secret_ key: put it in SUPABASE_SERVICE_KEY "
        "or the [supabase] 1Password refs in .cp-engine.local.toml."
    )


# The cp-hosted verb that does the same job as a `cxp` command, for the
# commands that have one (#413). Named when this machine cannot reach MC-2, so
# the person can carry on through the hosted connector instead of stopping.
HOSTED_FALLBACK = {
    "commitments-sweep": "commitments_sweep",
    "preflight": "preflight",
    "promote-uphill": "promote_uphill",
    "spine": "list_spine_elements",
}


def _backend_unavailable_line(command: str | None, exc: BaseException) -> str:
    """One line for "this machine can't reach MC-2" (#413).

    Names the cause (the credential error, whitespace-collapsed so a
    multi-line `op` message stays on one line) and, where one exists, the
    cp-hosted verb that does the same job without this machine's credentials.
    """
    cause = " ".join(str(exc).split()) or type(exc).__name__
    name = f"cxp {command}" if command else "cxp"
    line = f"{name}: can't reach Mission Control from this machine — {cause}"
    verb = HOSTED_FALLBACK.get(command or "")
    if verb:
        line += (
            f" Or ask the session to run `{verb}` on the cp-hosted connector "
            "instead; it needs no local credentials."
        )
    return line


class _CxpGroup(click.Group):
    """The `cxp` group: turns two credential failures into one clear line.

    A refused Supabase key (2026-10-22: Supabase disabled the legacy
    anon/service_role keys) and an unreachable MC-2 — no credentials, or a
    locked 1Password (#413) — used to end in a long traceback. Each now
    prints one line naming the cause (and, for the second, the cp-hosted
    verb that does the same job) and exits non-zero. Every other exception
    propagates exactly as before.
    """

    def invoke(self, ctx: click.Context):
        from cp_engine.sync import BackendUnavailable

        try:
            return super().invoke(ctx)
        except BackendUnavailable as exc:
            print(
                _backend_unavailable_line(ctx.invoked_subcommand, exc),
                file=sys.stderr,
            )
            sys.exit(1)
        except Exception as exc:
            from cp_engine.mc2_db import supabase_key_rejection

            reason = supabase_key_rejection(exc)
            if reason is None:
                raise
            print(_key_rejected_line(reason), file=sys.stderr)
            sys.exit(EXIT_SUPABASE_KEY_REJECTED)


@click.group(cls=_CxpGroup)
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


def _load_config_for_read(
    code: str | None = None, *, exit_code: int = 1, error_prefix: str = "Config error"
):
    """Load tenant config for a READ-ONLY command; tolerate unrelated clones.

    A configured repo clone that is missing on this machine (say the
    `storyos` checkout) used to stop every command, including read-only
    per-project ones that never open a clone (#413). Here it warns once on
    stderr and carries on — unless the missing clone is the one this command
    acts on: its code IS `code`, or it is attached to `code`'s workstream (a
    `_repo-<name>.md` in its working dir). Commands that write keep
    `_load_config_or_die`, which still fails on any missing path.
    """
    missing: list[tuple[str, Path]] = []
    try:
        config = load(Path.cwd(), missing_paths=missing)
    except CommittedConfigMissing:
        click.echo(
            "No .cp-engine.toml in this directory. Run `cxp init` "
            "or cd into the cp tenant root.",
            err=True,
        )
        sys.exit(exit_code)
    except ConfigError as exc:
        click.echo(f"{error_prefix}: {exc}", err=True)
        sys.exit(exit_code)
    if not missing:
        return config

    attached: set[str] = set()
    if code:
        from cp_engine.spine import SpineDirNotFound, find_spine_dir

        try:
            workdir = find_spine_dir(config.root, code)
        except SpineDirNotFound:
            workdir = None
        if workdir is not None:
            attached = {
                p.name[len("_repo-"):-len(".md")]
                for p in workdir.glob("_repo-*.md")
            }
    seen: set[str] = set()
    for name, path in missing:
        if name in seen:
            continue
        seen.add(name)
        if code and (name == code or name in attached):
            click.echo(
                f"{error_prefix}: the local clone of {name}, which {code} "
                f"uses, is not on this machine ({path}). Fix the path in "
                ".cp-engine.local.toml, or leave it empty to skip it.",
                err=True,
            )
            sys.exit(exit_code)
        click.echo(
            f"(note: the local clone of {name} is not at {path}; carrying "
            "on without it. Fix or empty that path in .cp-engine.local.toml "
            "to silence this.)",
            err=True,
        )
    return config


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
main.add_command(_session_mod.session_group)

# Back-compat import paths: tests (and possibly external callers) import
# these from cp_engine.cli; the implementations moved to cli_cmds/*.
_write_drift_flags = _spine_mod._write_drift_flags
_fetch_clickup_task_ids_for_hashes = _planning_mod._fetch_clickup_task_ids_for_hashes


if __name__ == "__main__":
    main()
