#!/usr/bin/env python3
"""One-time tree migration: rename project identifiers in the cp tenant
tree from the legacy `<company>-<number>` id to the canonical
full_job_name slug (e.g. `ibx-5192` → `ibx-5192-platform-sales-readiness-summit`).

This is Task 6 of the "Canonical Project ID from full_job_name" feature.
Tasks 1–4 flipped the engine's canonical id; this script brings the
EXISTING on-disk tree into line by `git mv`-ing the two places the old id
still appears:

  1. **Sprint files** — `sprints/<week>/<old>.md` across ALL weeks → `<new>.md`.
  2. **Working dirs** — `1p/<company>/<old-dir>/` → `1p/<company>/<new>/`,
     but ONLY when the existing dir genuinely differs from the new slug.
     In practice working dirs were already named `dir_slug(<old>, <name>)`
     = `<old>-<name-slug>`, which usually ALREADY equals the new
     full_job_name slug — so most dirs are a no-op. We move only the rare
     dir whose name differs (e.g. a bare `ibx-5192` dir).

It does NOT rewrite handwritten references — those live in human
territory. Instead it GREPS and REPORTS every handwritten reference to a
moved sprint path or old dir name across the tenant's `.md` files so the
operator can hand-fix the links.

`--dry-run` is the DEFAULT: it prints the planned `git mv`s and the link
report, and changes nothing. `--apply` performs the moves. The run is
idempotent: after `--apply`, a re-run finds no old files and plans
nothing. It never runs `cp sync` and never writes to MC-2 (MC-2 is read
ONLY to build the old→new map).

Seams (pure / IO-only, unit-tested without a DB):
  * ``plan_moves(tenant_root, id_map) -> list[(src, dst)]`` — pure planner.
  * ``apply_moves(tenant_root, moves, *, git=True)`` — executes the renames.
  * ``find_handwritten_refs(tenant_root, id_map) -> list[Ref]`` — link report.

`build_id_map` (Task 4) supplies the old→new map at runtime; tests pass a
map in directly.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Allow running as a plain script without an editable install: put both
# the package's src/ and this scripts/ dir (for the sibling import) on
# the path regardless of cwd.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))
sys.path.insert(0, str(_HERE))

# Reuse Task 4's map builder + row loading.
from full_job_name_id_map import build_id_map  # noqa: E402

from cp_engine.state import dir_slug  # noqa: E402
from cp_engine.sync import plan_sprint_renames  # noqa: E402

logger = logging.getLogger("migrate_full_job_name_ids")


# ──────────────────────────────────────────────────────────────────────
#  Planning (pure)
# ──────────────────────────────────────────────────────────────────────


def plan_moves(tenant_root: Path, id_map: dict[str, str]) -> list[tuple[Path, Path]]:
    """Return the ordered list of `(src, dst)` renames the migration would
    perform, given an `old_id -> new_id` map. Pure: reads the filesystem
    but mutates nothing.

    Two move kinds:

    * **Sprint files** — every `sprints/<week>/<old>.md` (all weeks) maps
      to `sprints/<week>/<new>.md`.
    * **Working dirs** — for any `1p/<company>/<dir>/` whose name resolves
      to `<old>` (either the bare old id or `dir_slug(<old>)`) AND whose
      name differs from the new slug `dir_slug(<new>)`, plan a move to
      `1p/<company>/<new-slug>/`. Dirs already named the new slug are
      skipped (the common case).

    Pairs where `old == new` never reach here (build_id_map filters them),
    but we defend against it anyway.
    """
    moves: list[tuple[Path, Path]] = []

    for old, new in sorted(id_map.items()):
        if old == new:
            continue
        new_slug = dir_slug(new)

        # 1) Sprint files, all weeks. Reuse the shared planner in cp_engine.sync
        # so the sprint-file glob lives in exactly one place (also used by
        # sync's drift/reactivation rename).
        moves.extend(plan_sprint_renames(tenant_root, old, new))

        # 2) Working dir under 1p/<company>/. The old dir name was
        # dir_slug(old, name); since dir_slug now ignores name, that is
        # just dir_slug(old). Match either the bare old id or that slug.
        old_dir_names = {old, dir_slug(old)}
        one_p = tenant_root / "1p"
        if one_p.is_dir():
            for company_dir in sorted(one_p.iterdir()):
                if not company_dir.is_dir():
                    continue
                for cand_name in old_dir_names:
                    cand = company_dir / cand_name
                    if not cand.is_dir():
                        continue
                    if cand_name == new_slug:
                        continue  # already at the new slug — no-op
                    dst = company_dir / new_slug
                    move = (cand, dst)
                    if move not in moves:
                        moves.append(move)

    return moves


# ──────────────────────────────────────────────────────────────────────
#  Execution
# ──────────────────────────────────────────────────────────────────────


def _git(args: list[str], *, cwd: Path) -> None:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}"
        )


def apply_moves(
    tenant_root: Path, moves: list[tuple[Path, Path]], *, git: bool = True
) -> None:
    """Perform the planned renames. With `git=True` (default) each rename
    is a `git mv` so history follows the file; with `git=False` a plain
    filesystem rename (used only if git is unavailable). Logs every move.
    """
    for src, dst in moves:
        rel_src = src.relative_to(tenant_root).as_posix()
        rel_dst = dst.relative_to(tenant_root).as_posix()
        logger.info("git mv %s -> %s", rel_src, rel_dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if git:
            _git(["mv", rel_src, rel_dst], cwd=tenant_root)
        else:
            src.rename(dst)


# ──────────────────────────────────────────────────────────────────────
#  Handwritten-reference report (read-only)
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Ref:
    """A handwritten reference to a moved sprint path or old dir name."""

    path: Path  # the .md file containing the reference (relative to tenant)
    line_no: int
    line: str
    matched: str  # the substring that matched (old sprint path or old id)

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.path.as_posix()}:{self.line_no}: {self.line.strip()}  [{self.matched}]"


def find_handwritten_refs(tenant_root: Path, id_map: dict[str, str]) -> list[Ref]:
    """Scan every `.md` file in the tenant for handwritten references to a
    moved sprint path (`sprints/<week>/<old>.md`) or to an old dir name
    (`<company>/<old>/` or `1p/<company>/<old>`). Returns the matches;
    NEVER edits anything. The operator hand-fixes these links.

    A match is reported once per (file, line, matched-token). We scan
    `.md` files only — generated files and the moved sprint files
    themselves may legitimately contain the old token, but the point is
    to surface *handwritten* links for human review, so we report
    everything and let the operator judge.
    """
    if not id_map:
        return []

    # The concrete tokens to match per old id: a sprint-file path ending
    # in `<old>.md`, or a dir reference `.../<old>/`.
    tokens_by_old: dict[str, list[str]] = {}
    for old in id_map:
        tokens_by_old[old] = [
            f"/{old}.md",   # any sprint path ending in <old>.md
            f"/{old}/",     # any dir reference .../<old>/
        ]

    refs: list[Ref] = []
    for md in sorted(tenant_root.rglob("*.md")):
        try:
            text = md.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        rel = md.relative_to(tenant_root)
        for i, line in enumerate(text.splitlines(), start=1):
            for old, tokens in tokens_by_old.items():
                for tok in tokens:
                    if tok in line:
                        refs.append(
                            Ref(path=rel, line_no=i, line=line, matched=tok.strip("/"))
                        )
    return refs


# ──────────────────────────────────────────────────────────────────────
#  Map loading (DB read-only) + CLI
# ──────────────────────────────────────────────────────────────────────


def _load_id_map(tenant_root: Path) -> dict[str, str]:
    """Load active engagements from MC-2 READ-ONLY and build the old→new
    id map (reusing Task 4's build_id_map + its row query). Mirrors
    full_job_name_id_map.main()'s row loading exactly."""
    from supabase import create_client

    from cp_engine.config import load
    from cp_engine.sync_mc2 import _load_supabase_creds

    cfg = load(tenant_root)
    url, key = _load_supabase_creds(cfg)
    client = create_client(url, key)
    resp = (
        client.table("projects")
        .select("number, full_job_name, mc_status, is_internal, companies!inner(code)")
        .in_("mc_status", ["Deal", "Open"])
        .eq("is_internal", False)
        .execute()
    )
    rows = resp.data or []
    return build_id_map(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "One-time tree migration: rename sprint files and any "
            "genuinely-differing working dirs from the legacy "
            "<company>-<number> id to the full_job_name slug. "
            "Dry-run by default; pass --apply to perform the git mv's."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the git mv's. Without this flag the script is a dry-run.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    tenant_root = Path.cwd()
    id_map = _load_id_map(tenant_root)

    if not id_map:
        print("No id changes needed — every active engagement already uses its full_job_name id.")
        return 0

    print(f"Loaded {len(id_map)} id change(s):")
    for old, new in sorted(id_map.items()):
        print(f"  {old} -> {new}")
    print()

    moves = plan_moves(tenant_root, id_map)
    if not moves:
        print("No sprint files or working dirs to move (tree already migrated).")
    else:
        verb = "Moving" if args.apply else "Would move (dry-run)"
        print(f"{verb} {len(moves)} path(s):")
        for src, dst in moves:
            print(
                f"  git mv {src.relative_to(tenant_root).as_posix()} "
                f"-> {dst.relative_to(tenant_root).as_posix()}"
            )
        print()

    # Handwritten-reference report (always, regardless of apply).
    refs = find_handwritten_refs(tenant_root, id_map)
    if refs:
        print(
            f"{len(refs)} handwritten reference(s) to old ids found "
            "(NOT edited — hand-fix these links):"
        )
        for r in refs:
            print(f"  {r}")
        print()
    else:
        print("No handwritten references to old ids found.")
        print()

    if args.apply:
        if moves:
            apply_moves(tenant_root, moves, git=True)
            print(
                f"Applied {len(moves)} git mv(s). Review with `git status`, "
                "fix the handwritten links above, then commit."
            )
        else:
            print("Nothing to apply.")
    else:
        print("Dry-run only. Re-run with --apply to perform the moves.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
