#!/usr/bin/env python3
"""Step 4c, one time: give every hand-written file in ``spine/`` a home, and
fix the MC-2 rows the generated view cannot place.

``spine/`` is now rendered from MC-2 on every sync; a file there that MC-2
does not render is quarantined and removed. Before the first such sync:

1. **Cards** (``spine/_authored/<slug>.md`` that parse as an element and have
   no MC-2 row — e.g. the slt-5196 Morgan Wright dossier) are imported into
   ``spine_substance`` + ``spine_steps`` (`cp_engine.spine_import`). Cards a
   render already quarantined are recovered from ``exceptions/region-edits/``.
2. **Documents** (anything else in ``spine/`` MC-2 does not render — e.g. the
   ibx-5192 ``_authored/Mehul Story Arc/`` story-arc drafts) are moved out of
   ``spine/`` to the same relative path under the workstream dir, minus the
   ``_authored/`` prefix (``git mv``), staying ordinary hand-owned files.
3. **Stale-code rows** — ``spine_substance`` rows whose ``project_code`` is
   not the workstream's directory code (the two ``GGL-london-safety-video-
   phase-i`` rows of archived ggl-5176) — are re-homed (the sync healer, run
   once for workstreams sync no longer visits), and
4. **inactive workstreams** (which sync does not render) are rendered once
   from MC-2, so those rows get files and a pre-#216 file (snt-5194's YAML
   indent) is re-rendered.

DRY RUN BY DEFAULT: reads production MC-2 (explicit columns only) and the
tree, prints the plan, writes nothing. ``--apply`` performs 1–4 and commits
the tenant (no push).

    python scripts/archive/step4c_import_spine_cards.py --tenant ~/Documents/Python/cp
    python scripts/archive/step4c_import_spine_cards.py --tenant ... --apply
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cp_engine import mc2_db  # noqa: E402
from cp_engine.config import load as load_config  # noqa: E402
from cp_engine.mc2_db import Tables  # noqa: E402
from cp_engine.spine_import import (  # noqa: E402
    Handwritten,
    classify,
    find_handwritten,
    import_card,
    quarantined_cards,
)
from cp_engine.spine_mirror import render_project_spine  # noqa: E402
from cp_engine.spine_substance_sync import _rehome_substance_codes  # noqa: E402
from cp_engine.sync import _inactive_bins, _read_mc_id  # noqa: E402


def workstreams(root: Path) -> list[dict]:
    """paths.json entries plus every inactive dir that carries an MC-id."""
    out = []
    index = json.loads((root / ".cp-engine" / "paths.json").read_text())["workstreams"]
    for code, w in sorted(index.items()):
        if w.get("mc2_id") and (root / w["path"]).is_dir():
            out.append({"code": code, "rel": w["path"], "mc2_id": w["mc2_id"],
                        "status": w.get("status"), "inactive": False})
    seen = {w["mc2_id"] for w in out}
    for bin_dir in _inactive_bins(root):
        for d in sorted(p for p in bin_dir.iterdir() if p.is_dir()):
            mc = _read_mc_id(d / "cp.md")
            if mc and mc not in seen:
                seen.add(mc)
                out.append({"code": d.name, "rel": str(d.relative_to(root)),
                            "mc2_id": mc, "status": "inactive", "inactive": True})
    return out


def doc_destination(root: Path, w: dict, hw: Handwritten) -> Path:
    """A document leaves spine/ for the same path under the workstream dir,
    minus a leading ``_authored/`` (that prefix only means "in the spine")."""
    parts = Path(hw.rel).parts
    if parts and parts[0] == "_authored":
        parts = parts[1:]
    return root / w["rel"] / Path(*parts)


def preview_render(client, root: Path, w: dict) -> list[str]:
    """Render ``w``'s spine into a scratch copy and summarise what the real
    render would change. Writes nothing in the tenant."""
    import filecmp
    import shutil

    src = root / w["rel"] / "spine"
    with tempfile.TemporaryDirectory() as td:
        troot = Path(td)
        (troot / ".cp-engine.toml").write_text("# preview\n")
        pdir = troot / "ws"
        if src.is_dir():
            shutil.copytree(src, pdir / "spine")
        else:
            pdir.mkdir()
        render_project_spine(client, project_id=w["mc2_id"], project_code=w["code"],
                             project_dir=pdir, writer="step4c preview")
        before = {p.relative_to(src).as_posix() for p in src.rglob("*.md")} if src.is_dir() else set()
        dst = pdir / "spine"
        after = {p.relative_to(dst).as_posix() for p in dst.rglob("*.md")} if dst.is_dir() else set()
        out = [f"+ {f}" for f in sorted(after - before)]
        out += [f"- {f}" for f in sorted(before - after)]
        out += [f"~ {f}" for f in sorted(before & after)
                if not filecmp.cmp(src / f, dst / f, shallow=False)]
        return out


def stale_code_rows(client, project_id: str, code: str) -> list[str]:
    rows = (client.table(Tables.SPINE_SUBSTANCE).select("id, project_code")
            .eq("project_id", project_id).execute().data) or []
    return [r["id"] for r in rows if r.get("project_code") != code]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tenant", type=Path, default=Path.cwd())
    ap.add_argument("--apply", action="store_true",
                    help="write MC-2, move files, render, commit (default: dry run)")
    args = ap.parse_args()
    root = args.tenant.resolve()
    client = mc2_db.get_client(load_config(root))

    cards: list[tuple[dict, Handwritten]] = []
    docs: list[tuple[dict, Handwritten]] = []
    stale: list[tuple[dict, list[str]]] = []
    for w in workstreams(root):
        pdir = root / w["rel"]
        for hw in find_handwritten(client, project_id=w["mc2_id"], project_dir=pdir):
            (cards if hw.kind == "card" else docs).append((w, hw))
        on_disk = {hw.rel for ws, hw in cards if ws is w}
        for q, rel, text in quarantined_cards(root, w["rel"]):
            if rel in on_disk:
                continue
            # Parse the recovered text from a scratch file — never the tree.
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td) / Path(rel).name
                tmp.write_text(text, encoding="utf-8")
                kind, item, _reason = classify(tmp, rel)
            if kind == "card":
                cards.append((w, Handwritten(path=pdir / "spine" / rel, rel=rel, kind=kind,
                                             item=item, from_quarantine=q,
                                             words=len(text.split()))))
        ids = stale_code_rows(client, w["mc2_id"], w["code"])
        if ids:
            stale.append((w, ids))

    print(f"tenant {root}  ({'APPLY' if args.apply else 'DRY RUN — nothing written'})\n")
    print(f"1. hand-written cards → MC-2 elements: {len(cards)}")
    for w, hw in cards:
        state = "ALREADY IN MC-2 (skip)" if hw.already_in_mc2 else (
            f"{len(hw.item.versions)} version(s), {len(hw.item.steps)} step(s)")
        src = f" [from quarantine {hw.from_quarantine.name}]" if hw.from_quarantine else ""
        print(f"   {w['code']}: {hw.item.est_item_id} — {hw.words} words, {state}{src}")
    print(f"\n2. documents moved out of spine/: {len(docs)}")
    for w, hw in docs:
        print(f"   {w['rel']}/spine/{hw.rel} → {doc_destination(root, w, hw).relative_to(root)}"
              f"  ({hw.words} words; {hw.reason})")
    print(f"\n3. stale-code rows to re-home: {sum(len(i) for _, i in stale)}")
    for w, ids in stale:
        for i in ids:
            print(f"   {w['code']}: {i}")
    inactive = [w for w in workstreams(root) if w["inactive"]]
    print(f"\n4. inactive workstreams rendered once: {len(inactive)}")
    for w in inactive:
        changes = preview_render(client, root, w)
        if changes:
            print(f"   {w['rel']}: " + "; ".join(changes))

    if not args.apply:
        print("\nDry run. Re-run with --apply to perform the above (commits, never pushes).")
        return 0

    for w, hw in cards:
        print(f"import {w['code']} {hw.item.est_item_id}: "
              f"{import_card(client, hw, project_id=w['mc2_id'], project_code=w['code'])}")
    for w, hw in docs:
        dst = doc_destination(root, w, hw)
        if dst.exists():
            print(f"SKIP move (destination exists): {dst}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "-C", str(root), "mv", str(hw.path), str(dst)], check=True)
    for w, _ids in stale:
        n = _rehome_substance_codes(client, project_id=w["mc2_id"], project_code=w["code"])
        print(f"re-homed {n} row(s) for {w['code']}")
    touched = {w["mc2_id"] for w, _ in cards} | {w["mc2_id"] for w, _ in stale}
    paths = sorted({w["rel"] for w, _ in cards + docs} | {w["rel"] for w, _ in stale})
    for w in workstreams(root):
        if w["inactive"] or w["mc2_id"] in touched:
            paths.append(w["rel"])
            warn: list[str] = []
            render_project_spine(client, project_id=w["mc2_id"], project_code=w["code"],
                                 project_dir=root / w["rel"],
                                 writer="step4c import", warnings_out=warn)
            for x in warn:
                print(f"WARN {w['code']}: {x}")
    # Stage only what this script touched (the tenant may hold unrelated
    # untracked work): the workstreams it rendered or moved files in, plus
    # any quarantine the renders wrote.
    paths = sorted(set(paths))
    if (root / "exceptions" / "region-edits").is_dir():
        paths.append("exceptions/region-edits")
    subprocess.run(["git", "-C", str(root), "add", "-A", "--", *paths], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m",
                    "[spine] step 4c: hand-written spine cards imported to MC-2; "
                    "documents moved out of spine/; inactive spines rendered",
                    # Pathspec: commit only what this script staged.
                    "--", *paths],
                   check=False)
    print("Committed (not pushed).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
