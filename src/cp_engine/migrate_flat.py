"""One-shot migration from pre-v0.7's `<scope>/projects/<dir>` layout to
v0.7's `<scope>/<dir>` layout.

What this does:
  - For each scope (`1p`, `firstpersonsf`, `canonic`):
    - Move every `<scope>/projects/<dir>/` to `<scope>/<dir>/` via `git mv`.
    - Move `<scope>/projects/archived/<dir>/` to `<scope>/archived/<dir>/`.
    - Remove the now-empty `<scope>/projects/` directory.
  - For each linked source repo's `.cp-link` file (paths read from
    discover_cp_working_dirs against the *post-migration* tree):
    - Rewrite the `.cp-link` to point at the new path.

Idempotent: running on an already-migrated tree is a no-op.

Refuses to run on a dirty working tree — same discipline as the old
`migrate-to-v03` command. Commit or stash before invoking.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from cp_engine.state import ARCHIVED_DIR_NAME

# v0.7 working-tree layout uses these three scopes; mirrors sync._SCOPE_DIRS.
_SCOPES: tuple[str, ...] = ("1p", "firstpersonsf", "canonic")


class MigrateFlatError(RuntimeError):
    """Raised on migration pre-flight failures or git errors."""


@dataclass(frozen=True)
class MigrateFlatResult:
    """Outcome of a migrate-projects-flat run."""

    moved_dirs: tuple[tuple[Path, Path], ...]  # (old_path, new_path) pairs, includes archive moves
    removed_projects_dirs: tuple[Path, ...]    # empty `<scope>/projects/` parents removed
    rewrote_cp_links: tuple[Path, ...]         # source-repo .cp-link files rewritten
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
        raise MigrateFlatError(
            f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}"
        )
    return result.stdout


def _require_clean_tree(tenant_root: Path) -> None:
    status = _git(["status", "--porcelain"], cwd=tenant_root).strip()
    if status:
        raise MigrateFlatError(
            f"Working tree at {tenant_root} is not clean. Commit or stash before "
            f"running migrate-projects-flat:\n{status}"
        )


def _git_mv(src: Path, dst: Path, *, cwd: Path) -> None:
    """Rename via `git mv`, ensuring the destination's parent exists.

    `git mv` doesn't auto-create parent dirs, so we mkdir first. Both
    paths are passed relative to `cwd` (the git repo root) for clearer
    diffs.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    src_rel = src.relative_to(cwd)
    dst_rel = dst.relative_to(cwd)
    _git(["mv", str(src_rel), str(dst_rel)], cwd=cwd)


def _rewrite_cp_links(tenant_root: Path) -> list[Path]:
    """Rewrite each linked source-repo's `.cp-link` to point at the new
    working dir. Returns the list of files rewritten.

    Soft-fails: if config can't be loaded (minimal tenant, missing
    `[sync]`, etc.), returns an empty list rather than raising. The
    directory migration is the load-bearing part; .cp-link rewriting is
    a convenience for the local machine.
    """
    from cp_engine.link_local import discover_cp_working_dirs

    working_dirs = discover_cp_working_dirs(tenant_root)
    new_paths_by_repo = {wd.repo_name: wd.path for wd in working_dirs}

    try:
        from cp_engine.config import load

        config = load(tenant_root)
    except Exception:
        return []

    rewrote: list[Path] = []
    for repo_name, source_path in (config.local_repos or {}).items():
        link_file = source_path / ".cp-link"
        if not link_file.exists():
            continue
        new_target = new_paths_by_repo.get(repo_name)
        if new_target is None:
            # Source repo configured but not discoverable in cp tree.
            # Skip rather than erase the link.
            continue
        new_text = str(new_target) + "\n"
        if link_file.read_text() == new_text:
            continue
        link_file.write_text(new_text)
        rewrote.append(link_file)
    return rewrote


def migrate_projects_flat(tenant_root: Path) -> MigrateFlatResult:
    """Migrate `<scope>/projects/...` → `<scope>/...` in place.

    Pre-conditions:
      - `tenant_root` is a git repo
      - working tree is clean
    Post-conditions:
      - all old `<scope>/projects/` paths are gone
      - all moves are staged and committed via the caller's normal git flow
        (this function only does `git mv`; the caller commits)
      - linked source repos' `.cp-link` files now point at the new paths
    """
    _require_clean_tree(tenant_root)

    moved: list[tuple[Path, Path]] = []
    removed_projects_dirs: list[Path] = []

    for scope in _SCOPES:
        old_projects_root = tenant_root / scope / "projects"
        if not old_projects_root.is_dir():
            continue

        # Move each child of <scope>/projects/, including the archived/ subdir.
        # Sort for deterministic ordering (matters for git's rename detection
        # and for predictable output).
        for child in sorted(old_projects_root.iterdir()):
            if not child.is_dir():
                # Files directly under <scope>/projects/ aren't expected,
                # but if any exist, move them to <scope>/ rather than
                # silently leaving them behind.
                new = tenant_root / scope / child.name
                _git_mv(child, new, cwd=tenant_root)
                moved.append((child, new))
                continue

            if child.name == ARCHIVED_DIR_NAME:
                # <scope>/projects/archived/ becomes <scope>/archived/.
                new_archive = tenant_root / scope / ARCHIVED_DIR_NAME
                if new_archive.exists():
                    raise MigrateFlatError(
                        f"Cannot migrate {child} to {new_archive}: destination already "
                        f"exists. Inspect manually and resolve before "
                        f"re-running."
                    )
                _git_mv(child, new_archive, cwd=tenant_root)
                moved.append((child, new_archive))
            else:
                # <scope>/projects/<dir>/ becomes <scope>/<dir>/.
                new_dir = tenant_root / scope / child.name
                if new_dir.exists():
                    raise MigrateFlatError(
                        f"Cannot migrate {child} to {new_dir}: destination already "
                        f"exists. Inspect manually and resolve before "
                        f"re-running."
                    )
                _git_mv(child, new_dir, cwd=tenant_root)
                moved.append((child, new_dir))

        # The empty `<scope>/projects/` dir (if it's actually empty now)
        # gets removed. git doesn't track empty dirs so this is a plain
        # rmdir; nothing to commit.
        if old_projects_root.exists() and not any(old_projects_root.iterdir()):
            old_projects_root.rmdir()
            removed_projects_dirs.append(old_projects_root)

    # Rewrite .cp-link files in linked source repos. discover_cp_working_dirs
    # walks the *current* tree, which is now post-migration, so the paths
    # it returns are the v0.7 paths we want to write.
    rewrote: list[Path] = []
    if moved:
        rewrote = _rewrite_cp_links(tenant_root)

    return MigrateFlatResult(
        moved_dirs=tuple(moved),
        removed_projects_dirs=tuple(removed_projects_dirs),
        rewrote_cp_links=tuple(rewrote),
        no_op=not (moved or removed_projects_dirs or rewrote),
    )
