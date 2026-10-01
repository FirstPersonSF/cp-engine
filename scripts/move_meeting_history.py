#!/usr/bin/env python3
"""Step 4c, one time: move every meeting-history timeline out of ``spine/``.

``<workstream>/spine/Retrospective/meeting-history.md`` → ``<workstream>/
meeting-history.md`` (`cp_engine.retrospective.HISTORY_FILENAME`). The history
has no MC-2 copy (its table was dropped in mig 072) and ``spine/`` is now a
view rendered from MC-2, so it moves to the workstream root as an ordinary
hand-owned tenant file. The content is not touched — a pure ``git mv``, so
``git log --follow`` keeps its history.

Until a workstream is moved, every reader and writer resolves the legacy file
(`retrospective.history_path`), so running this late loses nothing; the
render also leaves the legacy file alone.

DRY RUN BY DEFAULT: lists each move and refuses any whose destination exists.
``--apply`` does the ``git mv``s, removes the emptied ``Retrospective/`` dirs
and commits the tenant (no push).

    python scripts/move_meeting_history.py --tenant ~/Documents/Python/cp
    python scripts/move_meeting_history.py --tenant ... --apply
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cp_engine.retrospective import HISTORY_FILENAME, LEGACY_RELPATH  # noqa: E402


def plan(root: Path) -> tuple[list[tuple[Path, Path]], list[str]]:
    """``([(legacy, destination)], [refusals])`` for every tracked legacy file."""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", f"*/{LEGACY_RELPATH.as_posix()}"],
        check=True, capture_output=True, text=True,
    ).stdout
    moves, refusals = [], []
    for rel in sorted(x for x in out.split("\0") if x):
        legacy = root / rel
        ws = legacy.parents[2]           # <ws>/spine/Retrospective/<file>
        dest = ws / HISTORY_FILENAME
        if dest.exists():
            refusals.append(f"{rel}: destination {dest.relative_to(root)} already exists")
            continue
        moves.append((legacy, dest))
    return moves, refusals


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tenant", type=Path, default=Path.cwd())
    ap.add_argument("--apply", action="store_true",
                    help="git mv + commit (default: dry run)")
    args = ap.parse_args()
    root = args.tenant.resolve()
    moves, refusals = plan(root)
    words = 0
    print(f"tenant {root}  ({'APPLY' if args.apply else 'DRY RUN — nothing moved'})\n")
    for legacy, dest in moves:
        n = len(legacy.read_text(encoding="utf-8", errors="replace").split())
        words += n
        print(f"  {legacy.relative_to(root)}  →  {dest.relative_to(root)}  ({n:,} words)")
    for r in refusals:
        print(f"  REFUSED {r}")
    print(f"\n{len(moves)} move(s), {words:,} words; {len(refusals)} refused.")
    if refusals:
        print("Resolve the refusals by hand (merge the two files), then re-run.")
    if not args.apply:
        print("Dry run. Re-run with --apply to move (commits, never pushes).")
        return 1 if refusals else 0
    if not moves:
        return 0
    for legacy, dest in moves:
        subprocess.run(["git", "-C", str(root), "mv", str(legacy), str(dest)], check=True)
        retro = legacy.parent
        if retro.is_dir() and not any(p for p in retro.iterdir() if p.name != ".DS_Store"):
            for p in retro.iterdir():
                p.unlink()
            retro.rmdir()
    subprocess.run(
        ["git", "-C", str(root), "commit", "-q", "-m",
         f"[spine] step 4c: move {len(moves)} meeting-history timelines out of "
         "spine/ to <workstream>/meeting-history.md",
         # Pathspec: commit only these moves, never anything else staged.
         "--", *[str(p.relative_to(root)) for m in moves for p in m]],
        check=True,
    )
    print("Committed (not pushed).")
    return 1 if refusals else 0


if __name__ == "__main__":
    sys.exit(main())
