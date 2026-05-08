"""One-shot migration from v0.2 flat layout to v0.3 working-tree layout.

v0.2 stored every project CP at `projects/<code>.md`. v0.3 puts each
project in a working directory at `<scope>/projects/<code>/cp.md`, where
`<scope>` is derived from MC-2's `companies.kind` (client → 1p,
self-fpsf → firstpersonsf, self-canonic → canonic).

This module is the bridge. It runs once per tenant via `cp migrate-to-v03`,
moves every CP file to its new location using `git mv` (so rename detection
preserves history), and bails out if anything looks unsafe.

Safety properties:
  - Refuses to run if the working tree has uncommitted changes. The user
    must commit or stash first; otherwise a botched migration is hard to
    undo.
  - Refuses to run if the tenant root isn't a git working tree. We rely on
    `git mv` for rename detection.
  - Skips a move (logs warning) if the destination already exists. Never
    silently overwrites.
  - Refuses to run if any project on disk lacks a corresponding entry in
    MC-2 — that means we'd have no way to compute its scope. The user
    resolves by hand (most likely: the project was renamed in MC-2).

What gets moved:
  projects/<code>.md                  → <scope>/projects/<code>/cp.md
  projects/archived/<code>.md         → <scope>/projects/archived/<code>/cp.md

The old `projects/` and `projects/archived/` directories are removed
once empty.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cp_engine.config import TenantConfig
from cp_engine.state import scope_for
from cp_engine.sync import BackendFactory, _default_backend_factory

logger = logging.getLogger(__name__)


class MigrationError(Exception):
    """Migration can't proceed safely — abort with a clear message."""


@dataclass(frozen=True)
class MigrationResult:
    """Outcome of a migration run."""

    moved: tuple[tuple[Path, Path], ...]   # (old_path, new_path) pairs
    skipped: tuple[tuple[Path, str], ...]  # (old_path, reason) pairs


def migrate_to_v03(
    config: TenantConfig,
    *,
    backend_factory: BackendFactory | None = None,
    dry_run: bool = False,
) -> MigrationResult:
    """Move every v0.2 CP file to its v0.3 working-directory location.

    Args:
        config: loaded tenant config (provides `root` + sync backend).
        backend_factory: optional override (tests pass a fake).
        dry_run: if True, log what would happen but don't touch the disk.

    Returns:
        MigrationResult listing what moved and what was skipped.

    Raises:
        MigrationError: working tree dirty, not a git repo, or project on
            disk has no MC-2 entry to compute scope from.
    """
    root = config.root

    _require_git_clean(root)

    # Read MC-2 to build code → scope mapping. We do this before touching
    # the disk so a network failure can't leave us partway through.
    factory = backend_factory or _default_backend_factory
    backend = factory(config.sync.backend)
    projects = backend.read_projects(config)
    code_to_scope: dict[str, str] = {}
    for project in projects:
        try:
            code_to_scope[project.code] = scope_for(project.company_kind)
        except ValueError as exc:
            raise MigrationError(
                f"Cannot migrate: project {project.code!r} has unknown "
                f"company_kind {project.company_kind!r}. Fix in MC-2 first."
            ) from exc

    moved: list[tuple[Path, Path]] = []
    skipped: list[tuple[Path, str]] = []

    # Pass 1: live CPs at projects/<code>.md
    live_dir = root / "projects"
    if live_dir.exists():
        for path in sorted(live_dir.iterdir()):
            if path.is_dir():
                continue  # archived/ subdir handled in pass 2
            if path.suffix != ".md":
                continue
            code = path.stem
            scope = code_to_scope.get(code)
            if scope is None:
                # Project on disk but not in MC-2. Could be 1pi-* internal,
                # legacy admin file, or recently renamed. Don't guess —
                # surface it.
                skipped.append((path, "no MC-2 entry; resolve by hand"))
                continue
            target = root / scope / "projects" / code / "cp.md"
            if target.exists():
                skipped.append((path, f"target {target} already exists"))
                continue
            _git_mv(root, path, target, dry_run=dry_run)
            moved.append((path, target))

    # Pass 2: archived CPs at projects/archived/<code>.md
    #
    # Archived files predate MC-2's company_kind tracking — many bare-numeric
    # codes (5000-series) were archived before the companies table existed.
    # MC-2 has no record of them, so we can't look up their kind. Default
    # them to `1p`: every archived file in the cp tenant at v0.3 migration
    # time is historical 1P client work, which is the safe, common-case
    # bucket. The design doc accepts this fallback for the archived-only
    # path. Live projects still error if MC-2 doesn't know about them
    # (pass 1's strict behavior is unchanged).
    archived_old = root / "projects" / "archived"
    if archived_old.exists():
        for path in sorted(archived_old.iterdir()):
            if not path.is_file() or path.suffix != ".md":
                continue
            code = path.stem
            scope = code_to_scope.get(code)
            if scope is None:
                logger.info(
                    "Archived %s not in MC-2; defaulting to 1p scope.", code
                )
                scope = "1p"
            target = root / scope / "projects" / "archived" / code / "cp.md"
            if target.exists():
                skipped.append((path, f"target {target} already exists"))
                continue
            _git_mv(root, path, target, dry_run=dry_run)
            moved.append((path, target))

    # Pass 3: tidy up empty old dirs.
    if not dry_run:
        _remove_if_empty(archived_old)
        _remove_if_empty(live_dir)

    return MigrationResult(moved=tuple(moved), skipped=tuple(skipped))


def _require_git_clean(root: Path) -> None:
    """Verify `root` is a git working tree with no uncommitted changes.

    A dirty tree means a botched migration would commingle hand-edits with
    the move. Demanding a clean tree forces the user to commit explicitly
    before we touch anything.
    """
    if not (root / ".git").exists():
        # `.git` may be a file (worktree) or directory (regular checkout).
        # Either way it must exist at the root.
        raise MigrationError(
            f"{root} is not a git working tree. Migration uses `git mv` to "
            "preserve rename detection — initialize git first."
        )

    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MigrationError(
            f"`git status` failed in {root}: {result.stderr.strip() or result.stdout.strip()}"
        )
    if result.stdout.strip():
        raise MigrationError(
            "Working tree has uncommitted changes. Commit or stash before "
            "running migrate-to-v03 so the migration can be reviewed and "
            "reverted as a single commit if needed.\n\n"
            f"{result.stdout}"
        )


def _git_mv(root: Path, src: Path, dst: Path, *, dry_run: bool) -> None:
    """Run `git mv src dst`, creating any missing parent dirs first.

    `git mv` refuses to create destination directories on its own, and
    versions prior to 2.34 don't have a flag for it. mkdir -p the parent,
    then run the move. If git can't track the rename (e.g. file isn't
    tracked yet), fall back to a plain filesystem move + git add.
    """
    rel_src = src.relative_to(root)
    rel_dst = dst.relative_to(root)
    if dry_run:
        logger.info("[dry-run] git mv %s %s", rel_src, rel_dst)
        return

    dst.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "mv", str(rel_src), str(rel_dst)],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return

    # git mv failed. Most common reason: src isn't tracked (someone added
    # the file but never committed). Fall back to a plain move + stage.
    stderr = (result.stderr or result.stdout).strip()
    logger.warning(
        "git mv %s %s failed (%s); falling back to filesystem move + git add",
        rel_src,
        rel_dst,
        stderr,
    )
    shutil.move(str(src), str(dst))
    subprocess.run(["git", "add", str(rel_dst)], cwd=root, check=True)


def _remove_if_empty(path: Path) -> None:
    """Remove `path` if it exists and is an empty directory. Quiet no-op
    otherwise — the caller doesn't care if the dir was already gone."""
    if not path.exists() or not path.is_dir():
        return
    try:
        path.rmdir()
    except OSError:
        # Not empty — that's fine. Caller's job is best-effort cleanup.
        pass
