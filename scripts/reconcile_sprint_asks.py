#!/usr/bin/env python3
"""Reconcile every sprint-file ask with MC-2 ``commitments`` (step 4a).

Asks live in MC-2 from step 4a on; the sprint file only renders them. Before
the engine stops carrying asks from the files, every ask the files hold must
either BE a commitment already or become one. This walks the tenant's sprint
files and decides, per ask, using the policy Drew decided on 2026-10-01:

- **matched** — the ask already is a commitment (the one hash recipe, the
  bullet's own hash, the row's text under the new recipe, equal normalized
  text, or equal content words — ``cp_engine.asks.match_ask``), matched
  against ALL statuses. MC-2's status wins: an ask open in the file but done
  in MC-2 simply stops rendering. No write.
- **import-open** — no match, first raised within the last 6 weeks: a new
  ``open`` commitment, ``source_kind='sprint_import'``.
- **import-expired** — no match, first raised more than 6 weeks ago, OR on a
  Closed/Archived workstream: a new ``expired`` commitment, so the record
  exists and nothing chases it. (A Holding workstream's recent asks import
  ``open`` — paused is not over.)
- **skip_likely_duplicate** — no match, but ≥0.6 word overlap with an
  existing commitment of the workstream: probably the same ask in other
  words. NOT imported; the CSV names the commitment and its text for a human.

Each ask is read at its OWNING week and de-duplicated across carry-forward
(``sprints._open_through``, the engine's own walk). Owner: the first name in
``who``, canonicalized against the entities roster (an email only when the
roster has one). Due date: ``· by YYYY-MM-DD``. Direction: inferred from the
wording only where unambiguous, else the column default (``internal``).
``created_at`` is the first-raised day.

Dry run by default: reads MC-2 with explicit columns and the tenant on disk,
writes a CSV of every action, prints the counts, writes nothing. ``--apply``
inserts the imports. Run the re-key script (``rekey_commitment_hashes.py``)
first; this one matches on the NEW recipe in memory either way.

Usage:
    python scripts/reconcile_sprint_asks.py [--tenant ~/Documents/Python/cp]
        [--out reconcile.csv] [--today YYYY-MM-DD] [--apply]
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cp_engine.asks import (  # noqa: E402
    COMMITMENT_COLUMNS,
    OPEN,
    build_import_row,
    canonical_code,
    likely_duplicate,
    match_ask,
    normalize_ask_text,
)
from cp_engine.codes import code_number  # noqa: E402

ACTIVE = {"Deal", "Open"}


def mc2_client(tenant: Path):
    from cp_engine import mc2_db
    from cp_engine.config import load

    return mc2_db.get_client(load(tenant))


def fetch_all(client, table: str, columns: str) -> list[dict]:
    rows, start = [], 0
    while True:
        page = (client.table(table).select(columns)
                .range(start, start + 999).execute().data or [])
        rows += page
        if len(page) < 1000:
            return rows
        start += 1000


def sprint_asks(tenant: Path) -> dict[str, list[dict]]:
    """``{stem: [ask…]}`` — every ask identity's NEWEST statement, with
    whether the engine's carry walk still holds it open and its first-raised
    date. Identity is ``aggregators._bullet_key`` (cp:hash, else text)."""
    from cp_engine.aggregators import _bullet_key
    from cp_engine.sprints import _ASKS, _open_through, _owning_files, _parse_client_section

    by_stem: dict[str, list[Path]] = collections.defaultdict(list)
    for f in (tenant / "sprints").glob("20*-W*/*.md"):
        if not f.stem.startswith("_"):
            by_stem[f.stem].append(f)
    out: dict[str, list[dict]] = {}
    for stem, files in by_stem.items():
        files.sort(key=lambda p: p.parent.name, reverse=True)
        open_through = {
            _bullet_key(a.text): (a, raised, week)
            for a, raised, week in _open_through(_owning_files(files[0]), _ASKS)
        }
        seen: dict[str, dict] = {}
        for p in files:
            for a in _parse_client_section(p.read_text(encoding="utf-8"))[1]:
                k = _bullet_key(a.text)
                if k in seen:
                    continue
                m = re.search(r"cp:hash=([0-9a-f]{8})", a.text)
                held = open_through.get(k)
                seen[k] = {
                    "stem": stem, "text": a.text, "status": a.status,
                    "who": a.who, "by": a.by, "hash": m.group(1) if m else None,
                    "owning_week": held[2] if held else p.parent.name,
                    "raised": (held[1].isoformat() if held and held[1] else a.asked_date),
                    "open": held is not None,
                }
        out[stem] = list(seen.values())
    return out


def reconcile(tenant: Path, client, today: date) -> tuple[list[dict], list[dict], dict]:
    paths = json.loads((tenant / ".cp-engine/paths.json").read_text())["workstreams"]
    projects = fetch_all(client, "projects", "id, number, full_job_name, mc_status, company_id")
    company_name = {c["id"]: c.get("name") for c in fetch_all(client, "companies", "id, name")}
    by_id = {p["id"]: p for p in projects}
    by_number = {p["number"]: p for p in projects if p.get("number") is not None}
    rows = fetch_all(client, "commitments", COMMITMENT_COLUMNS)
    rows_by_project: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        rows_by_project[r["project_id"]].append(r)

    actions: list[dict] = []
    imports: list[dict] = []
    matched_ids: set[str] = set()
    notes = collections.Counter()
    for stem, asks in sorted(sprint_asks(tenant).items()):
        entry = paths.get(stem) or {}
        proj = by_id.get(entry.get("mc2_id")) or by_number.get(code_number(stem))
        if proj is None:
            for a in asks:
                actions.append({"action": "unresolved-workstream", "stem": stem, **_cols(a)})
            continue
        code = canonical_code(proj.get("full_job_name")) or stem
        status = entry.get("status") or proj.get("mc_status")
        active = status in ACTIVE
        pool = rows_by_project[proj["id"]]
        existing = list(pool)  # real commitments only — the duplicate check's pool
        for a in asks:
            m = match_ask(a["text"], code=code, rows=pool, bullet_hash=a["hash"])
            base = {"stem": stem, "code": code, "active": active, "ws_status": status,
                    **_cols(a)}
            if m:
                matched_ids.add(m.row["id"])
                flip = ""
                if a["open"] and m.row.get("status") != OPEN:
                    flip = f"file open → mc2 {m.row.get('status')}"
                elif not a["open"] and m.row.get("status") == OPEN:
                    flip = "file closed → mc2 open"
                actions.append({**base, "action": "matched", "method": m.method,
                                "commitment_id": m.row["id"], "mc2_status": m.row.get("status"),
                                "status_flip": flip})
                continue
            if not a["open"]:
                actions.append({**base, "action": "closed-in-file-no-row"})
                continue
            dup = likely_duplicate(a["text"], existing)
            if dup is not None:
                actions.append({**base, "action": "skip_likely_duplicate",
                                "commitment_id": dup[0]["id"],
                                "mc2_status": dup[0].get("status"),
                                "overlap": f"{dup[1]:.2f}",
                                "matched_text": " ".join((dup[0].get("description") or "").split())[:300]})
                continue
            row = build_import_row(
                text=a["text"], code=code, project_id=proj["id"], who=a["who"],
                by=a["by"], asked_date=a["raised"], today=today, client=client,
                company_name=company_name.get(proj.get("company_id")),
                workstream_status=status,
            )
            if any(i["cp_hash"] == row["cp_hash"] for i in imports):
                actions.append({**base, "action": "duplicate-of-import", "new_hash": row["cp_hash"]})
                continue
            imports.append(row)
            pool.append({**row, "id": f"import-{len(imports)}"})  # later restatements match it
            actions.append({**base, "action": f"import-{row['status']}", "new_hash": row["cp_hash"],
                            "owner_name": row.get("owner_name") or "",
                            "owner_email": row.get("owner_email") or "",
                            "due_date": row.get("due_date") or "",
                            "direction": row.get("direction") or "(default internal)"})
    # Open commitments no sprint ask names: they start rendering in the files.
    for r in rows:
        if r.get("status") == OPEN and r["id"] not in matched_ids:
            proj = by_id.get(r["project_id"]) or {}
            actions.append({"action": "mc2-only-open (will render)",
                            "code": canonical_code(proj.get("full_job_name")),
                            "commitment_id": r["id"], "mc2_status": OPEN,
                            "text": normalize_ask_text(r.get("description"))[:300]})
    notes["commitments"] = len(rows)
    return actions, imports, notes


def _cols(a: dict) -> dict:
    return {"file_status": a["status"], "file_open": a["open"], "owning_week": a["owning_week"],
            "raised": a["raised"], "who": a["who"] or "", "by": a["by"] or "",
            "bullet_hash": a["hash"] or "", "text": " ".join(a["text"].split())[:300]}


def summarize(actions: list[dict], imports: list[dict]) -> str:
    c = collections.Counter(a["action"] for a in actions)
    lines = ["actions:"] + [f"  {k:30} {v}" for k, v in sorted(c.items())]
    m = [a for a in actions if a["action"] == "matched"]
    lines.append("matched by method: " + ", ".join(
        f"{k}={v}" for k, v in collections.Counter(a["method"] for a in m).most_common()))
    flips = collections.Counter(a["status_flip"] for a in m if a.get("status_flip"))
    lines.append("status flips (MC-2 wins): " + (", ".join(f"{k}: {v}" for k, v in flips.items()) or "none"))
    for status in ("open", "expired"):
        rows = [r for r in imports if r["status"] == status]
        acts = [a for a in actions if a["action"] == f"import-{status}"]
        if not rows:
            continue
        ws = collections.Counter(a.get("ws_status") or "?" for a in acts)
        lines.append(f"import-{status}: {len(rows)} rows (by workstream status: "
                     + ", ".join(f"{k}={v}" for k, v in ws.most_common()) + ")")
        lines.append(f"  missing due_date: {sum(1 for r in rows if not r.get('due_date'))}")
        lines.append(f"  owner email resolved: {sum(1 for r in rows if r.get('owner_email'))}"
                     f" · name only: {sum(1 for r in rows if r.get('owner_name') and not r.get('owner_email'))}"
                     f" · no owner: {sum(1 for r in rows if not r.get('owner_name'))}")
        d = collections.Counter(r.get("direction") or "default(internal)" for r in rows)
        lines.append("  direction: " + ", ".join(f"{k}={v}" for k, v in d.most_common()))
    return "\n".join(lines)


def apply(client, imports: list[dict]) -> int:
    for i in range(0, len(imports), 100):
        client.table("commitments").insert(imports[i:i + 100]).execute()
    return len(imports)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tenant", type=Path, default=Path.home() / "Documents/Python/cp")
    ap.add_argument("--out", type=Path, default=Path("reconcile_sprint_asks.csv"))
    ap.add_argument("--today", type=date.fromisoformat, default=None)
    ap.add_argument("--apply", action="store_true", help="insert the imports (default: dry run)")
    args = ap.parse_args(argv)

    from cp_engine.clock import tenant_today

    today = args.today or tenant_today()
    client = mc2_client(args.tenant)
    actions, imports, notes = reconcile(args.tenant, client, today)
    fields = ["action", "stem", "code", "active", "ws_status", "file_status", "file_open", "owning_week",
              "raised", "who", "by", "bullet_hash", "method", "commitment_id", "mc2_status",
              "status_flip", "new_hash", "owner_name", "owner_email", "due_date", "direction",
              "overlap", "matched_text", "text"]
    with args.out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(actions)
    print(f"today: {today} · commitments read: {notes['commitments']}")
    print(summarize(actions, imports))
    print(f"csv: {args.out}")
    if not args.apply:
        print("dry run — nothing written (pass --apply to insert)")
        return 0
    print(f"applied: {apply(client, imports)} commitments inserted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
