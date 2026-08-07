#!/usr/bin/env python3
"""Sweep Finder/iCloud-style " 2" duplicates from a repo clone (#133).

iCloud Desktop & Documents sync (since excluded) littered dev clones with
`foo 2.py` / `pkg-1.2 2.dist-info` twins — fatal to `uv`, which parses
every dist-info name as a version. This sweeps them SAFELY:

- A path is only removed when a canonical twin exists AND is verified
  identical (file: same size+MD5; directory: recursive content diff).
- Tracked files are NEVER touched (git ls-files check) — a " 2" name that
  is tracked or differs from its twin is reported, not removed.
- Dry run by default; pass --delete to actually remove.

Usage:
    python3 scripts/sweep_dupes.py <repo-root> [--delete]
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

# "name 2.ext", "name 2", "name 2.dist-info" — the Finder collision shapes.
_DUPE_RE = re.compile(r"^(?P<stem>.*) (?P<n>[2-9])(?P<ext>(\.[A-Za-z0-9_.-]+)?)$")


def canonical_twin(path: Path) -> Path | None:
    m = _DUPE_RE.match(path.name)
    if not m:
        return None
    twin = path.with_name(f"{m.group('stem')}{m.group('ext') or ''}")
    return twin if twin.exists() else None


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dirs_identical(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(str(a), str(b))
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return False
    return all(
        _dirs_identical(a / sub, b / sub) for sub in cmp.common_dirs
    )


def identical(dupe: Path, twin: Path) -> bool:
    if dupe.is_dir() != twin.is_dir():
        return False
    if dupe.is_dir():
        return _dirs_identical(dupe, twin)
    try:
        return (
            dupe.stat().st_size == twin.stat().st_size
            and _md5(dupe) == _md5(twin)
        )
    except OSError:
        return False


def tracked_paths(root: Path) -> set[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return set()
    return {p for p in out.decode("utf-8", "replace").split("\0") if p}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--delete", action="store_true", help="actually remove (default: dry run)")
    args = ap.parse_args()
    root = args.root.resolve()
    tracked = tracked_paths(root)

    candidates: list[Path] = []
    for p in sorted(root.rglob("* *")):
        if ".git" in p.parts:
            continue
        if _DUPE_RE.match(p.name):
            candidates.append(p)
    # Prune nested candidates — removing the outermost dir covers them.
    pruned: list[Path] = []
    for p in candidates:
        if not any(parent in pruned for parent in p.parents):
            pruned.append(p)

    removed = kept_tracked = kept_differs = kept_no_twin = 0
    for p in pruned:
        rel = p.relative_to(root).as_posix()
        if rel in tracked:
            print(f"KEEP (tracked):   {rel}")
            kept_tracked += 1
            continue
        twin = canonical_twin(p)
        if twin is None:
            print(f"KEEP (no twin):   {rel}")
            kept_no_twin += 1
            continue
        if not identical(p, twin):
            print(f"KEEP (differs):   {rel}")
            kept_differs += 1
            continue
        print(f"{'REMOVE' if args.delete else 'would remove'}:  {rel}")
        if args.delete:
            shutil.rmtree(p) if p.is_dir() else p.unlink()
        removed += 1

    print(
        f"\n{removed} verified duplicate(s) {'removed' if args.delete else 'removable'} · "
        f"kept: {kept_no_twin} no-twin, {kept_differs} differing, {kept_tracked} tracked"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
