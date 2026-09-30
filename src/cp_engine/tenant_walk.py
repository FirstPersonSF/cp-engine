"""Tenant-wide file walks that stay inside THIS tenant (#325).

A cp tenant clone can carry other checkouts of itself: an agent session's
git worktree under `.claude/worktrees/<name>/`, or a `git worktree add`
someone pointed at a subdirectory. Each is a full copy of the tree — every
`cp.md`, every `sessions/` dir — so a bare `root.rglob("cp.md")` walks the
tenant twice. On 2026-09-25 one stray agent worktree doubled every warning
`cxp render` printed, and the same walk in sync would have refreshed the
worktree's Last session lines as though they were the tenant's own.

The rule here: a walk from the tenant root never descends into

- a dot-directory (`.git`, `.claude`, `.cp-engine`, …) — nothing a tenant
  walk looks for lives there, and the path resolvers in `state.py` already
  skip them; or
- any path `git worktree list` names under the root, other than the root
  itself — a worktree placed at a plain-named subdirectory is still a
  second tenant.

`os.walk` with pruning rather than `rglob` + filter, so a large worktree is
never even listed.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from pathlib import Path
from typing import Iterator


def linked_worktrees(root: Path) -> tuple[Path, ...]:
    """Resolved paths of every git worktree strictly inside `root`.

    Empty when `root` isn't a git checkout or git isn't available — a walk
    that can't ask git still skips dot-dirs, which covers the common case.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain"],
            capture_output=True, text=True, check=True, timeout=10,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ()
    root_r = root.resolve()
    found: list[Path] = []
    for line in out.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[len("worktree "):]).resolve()
        if path != root_r and root_r in path.parents:
            found.append(path)
    return tuple(found)


def walk_tenant(root: Path, name_glob: str, *, dirs: bool = False) -> Iterator[Path]:
    """Yield paths under `root` whose NAME matches `name_glob`, sorted.

    Files by default; `dirs=True` yields matching directories instead (the
    `sessions/` walk). Skips dot-directories and linked worktrees as the
    module docstring describes.
    """
    root = Path(root)
    skip = set(linked_worktrees(root))
    hits: list[Path] = []
    for current, subdirs, files in os.walk(root):
        cur = Path(current)
        kept = []
        for d in subdirs:
            if d.startswith("."):
                continue
            if skip and (cur / d).resolve() in skip:
                continue
            kept.append(d)
        subdirs[:] = kept
        names = kept if dirs else files
        hits.extend(cur / n for n in names if fnmatch.fnmatchcase(n, name_glob))
    return iter(sorted(hits))
