"""One-shot migration from flat `1p/<dir>/` to account-nested
`1p/<company>/<dir>/` (Phase 2 of the account restructure).

Design: cp/docs/plans/2026-05-22-migrate-accounts-design.md.

What this does:
  - Reads MC-2 once to build a `code → (company_slug, company_name)` map.
  - Walks `<tenant>/1p/` for flat project dirs whose code is in the map;
    plans a `git mv` into `1p/<company_slug>/<dir>/`.
  - Repeats for flat `1p/inactive/<dir>/`, routing them into
    `1p/<company_slug>/inactive/<dir>/` (the per-account inactive bin
    that Phase 1 introduced).
  - Skips dirs that already look like account dirs (their name matches
    a known company slug AND they contain a `cp.md`) — idempotent
    re-run is a no-op.
  - Absorbs `1p/_teleflex.md` (if present) into `1p/teleflex/cp.md`
    under a dated `## Legacy notes` heading.
  - Runs `cp sync` so account cp.md files get scaffolded and master-cp
    re-renders.
  - Surfaces residue (`weekly-cp.md` references, `.cp-link` files)
    for the operator to hand-fix.

Idempotent: running on an already-migrated tree returns `no_op=True`.

Refuses to run on a dirty working tree (same discipline as
`migrate_projects_flat`). `--dry-run` mode is read-only and tolerates
a dirty tree.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from cp_engine.state import INACTIVE_DIR_NAME, company_slug
from cp_engine.sync import Backend

logger = logging.getLogger(__name__)


class MigrateAccountsError(RuntimeError):
    """Raised on migration pre-flight failures or git errors."""


@dataclass(frozen=True)
class MigrateAccountsResult:
    """Outcome of a migrate-accounts run.

    `planned_moves` is what the migration would do; `moved_dirs` is what
    it actually did (empty on dry-run). `unresolved_dirs` is populated
    when pre-flight finds flat dirs whose code can't be matched against
    MC-2 — in that case the migration hard-fails before any disk
    writes, and `moved_dirs` stays empty.
    """

    planned_moves: tuple[tuple[Path, Path], ...]
    moved_dirs: tuple[tuple[Path, Path], ...]
    absorbed_teleflex: bool
    unresolved_dirs: tuple[Path, ...]
    sync_files_written: tuple[Path, ...]
    residue: tuple[str, ...]
    no_op: bool


def _git(args: list[str], *, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MigrateAccountsError(
            f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}"
        )
    return result.stdout


def _require_clean_tree(tenant_root: Path) -> None:
    status = _git(["status", "--porcelain"], cwd=tenant_root).strip()
    if status:
        raise MigrateAccountsError(
            f"Working tree at {tenant_root} is not clean. Commit or stash before "
            f"running migrate-accounts:\n{status}"
        )


def _git_mv(src: Path, dst: Path, *, cwd: Path) -> None:
    """Rename via `git mv`, ensuring the destination's parent exists."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_rel = src.relative_to(cwd)
    dst_rel = dst.relative_to(cwd)
    _git(["mv", str(src_rel), str(dst_rel)], cwd=cwd)


# Project codes look like `<prefix>-<digits>` (`ggl-5168`, `ibx-5153`)
# OR a freeform slug (`mc-2`, `storyos`, `unf-forge`). Working-dir names
# are either the bare code or `<code>-<slug>`. To extract the code from
# a dir name, we try progressively shorter prefixes against the known
# code map — the longest match wins.
def _extract_code_from_dir_name(dir_name: str, known_codes: set[str]) -> str | None:
    """Return the project code embedded in `dir_name`, or None.

    Tries the longest plausible prefix first (so `ggl-5177-event-safety`
    matches `ggl-5177` and not just `ggl`). Compares against the
    known-codes set so dirs like `ggl-5177-event-safety` resolve to
    `ggl-5177` even when no canonical separator marks the boundary.
    """
    if dir_name in known_codes:
        return dir_name
    # Walk segments right-to-left, joining back with `-`.
    parts = dir_name.split("-")
    for i in range(len(parts) - 1, 0, -1):
        candidate = "-".join(parts[:i])
        if candidate in known_codes:
            return candidate
    return None


# Project-code shape: `<lowercase-alpha-prefix>-<digits>(-<slug>)?`.
# Examples: `ggl-5168`, `ibx-5167-ddi-platform-video`, `tel-5113`.
# Used to distinguish flat project dirs (which should resolve against
# MC-2 or hard-fail) from other dirs under 1p/ — hand-created account
# scaffolding, stray notes, etc. — which should be silently skipped.
# Freeform repo slugs (`mc-2`, `storyos`) don't appear under 1p/ so
# this stricter pattern is safe.
_PROJECT_CODE_DIR_NAME_RE = re.compile(r"^[a-z]+-\d+(-[a-z0-9-]+)?$")


def _looks_like_project_code_dir(name: str) -> bool:
    """True if `name` matches the `<prefix>-<digits>(-<slug>)?` shape
    of a client engagement working dir under `1p/`."""
    return bool(_PROJECT_CODE_DIR_NAME_RE.match(name))


def migrate_accounts(
    tenant_root: Path,
    *,
    dry_run: bool = False,
    backend_factory: Callable[[str], Backend] | None = None,
    now: datetime | None = None,
) -> MigrateAccountsResult:
    """Migrate `1p/<dir>/` → `1p/<company>/<dir>/` in place.

    See module docstring for the full flow. Pre-conditions:
      - `tenant_root` is a git repo
      - working tree is clean (unless `dry_run=True`)
      - `.cp-engine.toml` loads cleanly
      - the configured backend resolves every flat 1p/ dir's code

    Post-conditions (on a non-dry-run, successful invocation):
      - all flat `1p/<dir>/` paths have moved to `1p/<slug>/<dir>/`
      - all flat `1p/inactive/<dir>/` paths have moved to
        `1p/<slug>/inactive/<dir>/`
      - `1p/_teleflex.md` (if present) is absorbed into
        `1p/teleflex/cp.md` and the old file is git-removed
      - `cp sync` has been run; account `cp.md` files are scaffolded
        and master-cp has the new Account column
      - all moves are staged but NOT committed — caller commits
    """
    if not dry_run:
        _require_clean_tree(tenant_root)

    # Pre-flight: load config + read backend.
    from cp_engine.config import load
    try:
        config = load(tenant_root)
    except Exception as exc:
        raise MigrateAccountsError(
            f"Cannot load .cp-engine.toml at {tenant_root}: {exc}"
        ) from exc

    factory = backend_factory or _default_backend_factory
    backend = factory(config.sync.backend)
    projects = backend.read_projects(config)
    # Map code → (slug, display_name) for every client project. Slug
    # comes from company_slug(company_name) so it matches what sync
    # uses on the live side.
    code_to_account: dict[str, tuple[str, str]] = {}
    known_slugs: set[str] = set()
    for p in projects:
        if p.company_kind != "client":
            continue
        slug = company_slug(p.company_name)
        display = p.company_name or slug
        code_to_account[p.code] = (slug, display)
        known_slugs.add(slug)

    one_p_root = tenant_root / "1p"
    if not one_p_root.is_dir():
        # Nothing to migrate — fresh tenant or non-1p tenant.
        return MigrateAccountsResult(
            planned_moves=(), moved_dirs=(), absorbed_teleflex=False,
            unresolved_dirs=(), sync_files_written=(), residue=(),
            no_op=True,
        )

    # Plan moves. Walk `1p/` for direct child dirs; skip account dirs
    # (name matches a known slug AND contains cp.md) and skip the flat
    # `inactive/` bin (handled separately below).
    planned: list[tuple[Path, Path]] = []
    unresolved: list[Path] = []
    known_codes = set(code_to_account)

    for child in sorted(one_p_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name == INACTIVE_DIR_NAME:
            continue  # handled separately
        if child.name in known_slugs and (child / "cp.md").exists():
            continue  # already-migrated account dir
        # Distinguish "looks like a project code → must resolve or
        # hard-fail" from "doesn't look like a project code → silently
        # skip." The latter covers hand-created account dirs that have
        # no live cp.md yet (e.g. a client whose only project is
        # archived in MC-2), stray notes dirs, and any other
        # non-project content under 1p/.
        if not _looks_like_project_code_dir(child.name):
            continue
        code = _extract_code_from_dir_name(child.name, known_codes)
        if code is None:
            unresolved.append(child)
            continue
        slug, _display = code_to_account[code]
        new_path = one_p_root / slug / child.name
        planned.append((child, new_path))

    # Flat inactive dirs: `1p/inactive/<dir>/` → `1p/<slug>/inactive/<dir>/`.
    flat_inactive = one_p_root / INACTIVE_DIR_NAME
    if flat_inactive.is_dir():
        for child in sorted(flat_inactive.iterdir()):
            if not child.is_dir():
                continue
            code = _extract_code_from_dir_name(child.name, known_codes)
            if code is None:
                unresolved.append(child)
                continue
            slug, _display = code_to_account[code]
            new_path = one_p_root / slug / INACTIVE_DIR_NAME / child.name
            planned.append((child, new_path))

    if unresolved:
        # Hard-fail before any disk writes. Surface the list for the
        # operator to fix MC-2 or hand-move.
        return MigrateAccountsResult(
            planned_moves=tuple(planned),
            moved_dirs=(),
            absorbed_teleflex=False,
            unresolved_dirs=tuple(unresolved),
            sync_files_written=(),
            residue=(),
            no_op=False,
        )

    if not planned and not (one_p_root / "_teleflex.md").exists():
        return MigrateAccountsResult(
            planned_moves=(), moved_dirs=(), absorbed_teleflex=False,
            unresolved_dirs=(), sync_files_written=(), residue=(),
            no_op=True,
        )

    if dry_run:
        return MigrateAccountsResult(
            planned_moves=tuple(planned),
            moved_dirs=(),
            absorbed_teleflex=(one_p_root / "_teleflex.md").exists(),
            unresolved_dirs=(),
            sync_files_written=(),
            residue=_compute_residue(tenant_root, code_to_account),
            no_op=False,
        )

    # Execute moves.
    moved: list[tuple[Path, Path]] = []
    for old, new in planned:
        _git_mv(old, new, cwd=tenant_root)
        moved.append((old, new))

    # Run `cp sync` so account cp.md files get scaffolded before the
    # _teleflex absorb (which appends below those files's engine
    # regions).
    from cp_engine.sync import sync_tenant
    sync_result = sync_tenant(config, backend_factory=factory, now=now)
    sync_files_written = sync_result.files_written

    # Absorb `_teleflex.md` if present.
    absorbed_teleflex = False
    teleflex_md = one_p_root / "_teleflex.md"
    teleflex_cp = one_p_root / "teleflex" / "cp.md"
    if teleflex_md.exists() and teleflex_cp.exists():
        absorbed_teleflex = True
        legacy_body = teleflex_md.read_text()
        today_iso = (now or datetime.now(timezone.utc)).date().isoformat()
        heading = f"\n## Legacy notes (from _teleflex.md, migrated {today_iso})\n\n"
        existing = teleflex_cp.read_text()
        teleflex_cp.write_text(existing.rstrip() + "\n" + heading + legacy_body)
        # `git rm` the old file so the operator's commit picks it up.
        _git(["rm", str(teleflex_md.relative_to(tenant_root))], cwd=tenant_root)

    return MigrateAccountsResult(
        planned_moves=tuple(planned),
        moved_dirs=tuple(moved),
        absorbed_teleflex=absorbed_teleflex,
        unresolved_dirs=(),
        sync_files_written=tuple(sync_files_written),
        residue=_compute_residue(tenant_root, code_to_account),
        no_op=False,
    )


# Match `cp/1p/<code>` or bare `1p/<code>` references in weekly-cp.md
# and similar handwritten files. The tail allows a leading underscore
# so the legacy `_teleflex.md` account-summary file is caught alongside
# project-code references. Used by the residue scan to surface paths
# the operator should hand-fix.
_OLD_PATH_RE = re.compile(r"(?:cp/)?1p/(_?[a-z0-9][a-z0-9_.-]*)")


def _compute_residue(
    tenant_root: Path,
    code_to_account: dict[str, tuple[str, str]],
) -> tuple[str, ...]:
    """Surface human-readable hints for the operator to hand-fix.

    Scans `weekly-cp.md` (the only handwritten file with a high chance
    of stale 1p/ paths) for references that still point at the flat
    layout. `.cp-link` files in linked source repos would also matter
    but they live outside the tenant tree; we leave that to the
    Phase 3 manual checklist.
    """
    hints: list[str] = []
    weekly = tenant_root / "weekly-cp.md"
    if not weekly.is_file():
        return ()
    known_codes = set(code_to_account)
    for i, line in enumerate(weekly.read_text().splitlines(), start=1):
        for m in _OLD_PATH_RE.finditer(line):
            tail = m.group(1)
            # `_teleflex.md` is the legacy account-summary file the
            # migration absorbs separately — surface its reference too.
            if tail == "_teleflex.md":
                hints.append(f"weekly-cp.md L{i}: {m.group(0)} (absorbed into 1p/teleflex/cp.md)")
                continue
            # Reverse-extract a project code from the tail and check if
            # it's a known one. If yes, this is a stale flat path.
            code = _extract_code_from_dir_name(tail, known_codes)
            if code is not None:
                slug, _display = code_to_account[code]
                hints.append(
                    f"weekly-cp.md L{i}: {m.group(0)} → now under 1p/{slug}/"
                )
    return tuple(hints)


# Single backend resolver lives in sync (arch-phase-4, #34); the module-level
# name is kept so callers/tests patching `migrate_accounts._default_backend_factory`
# keep working.
from cp_engine.sync import _default_backend_factory  # noqa: E402
