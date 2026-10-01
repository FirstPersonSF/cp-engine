#!/usr/bin/env python3
"""Re-key every MC-2 commitment to the one ask hash recipe (step 4a).

``cp_engine.asks.ask_hash(<co>-<number>, normalized description)`` is
now the only recipe. Rows written before it carry one of four others —
``record-ask`` over the SHORT code (the webhook), ``set-milestone`` /
``set-client-ask-task`` over the full code, a random uuid slice (the hosted
server), or none — so a hash lookup misses ~73% of real twins. This rewrites
``cp_hash`` on every row so the lookup and the matcher agree.

COLLISIONS. ``commitments.cp_hash`` has a GLOBAL unique index. Two rows of
one workstream whose descriptions normalize to the same text get the same
new hash; the winner keeps it (an OPEN row first, then the oldest) and each
other row gets the next salted occurrence (``ask_hash(..., occurrence=n)``),
the same rule the hosted ``create_commitment`` uses for a deliberate repeat.
Every collision is listed. Promoted rows (``source_kind='promoted'``) keep
their hash — their identity is the original row, not their text.

Dry run by default: reads MC-2 (explicit columns), writes a CSV, prints the
counts, changes nothing. ``--apply`` writes, in two phases (every changing
row to a temporary ``rk-<id>`` value first, then to its final hash), so a
row whose new hash equals another row's OLD hash cannot trip the unique
index mid-run.

Usage:
    python scripts/archive/rekey_commitment_hashes.py [--tenant ~/Documents/Python/cp]
        [--out rekey.csv] [--apply]
"""

from __future__ import annotations

import argparse
import collections
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from cp_engine.asks import ask_hash, canonical_code, hash_key  # noqa: E402

COLUMNS = ("id, description, status, source_kind, cp_hash, project_id, "
           "created_at")


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


def plan_rekey(projects: list[dict], rows: list[dict]) -> list[dict]:
    """One action per row: ``keep`` | ``rekey`` | ``rekey-salted`` |
    ``assign`` (row had no hash) | ``skip-promoted`` | ``skip-no-project``."""
    # The rename-stable key (`<co>-<number>`) the recipe hashes on.
    code_of = {
        p["id"]: hash_key(canonical_code(p.get("full_job_name"))) for p in projects
    }
    groups: dict[tuple, list[dict]] = collections.defaultdict(list)
    actions: list[dict] = []
    for r in rows:
        code = code_of.get(r.get("project_id"))
        base = {"id": r["id"], "code": code or "", "status": r.get("status"),
                "source_kind": r.get("source_kind"), "old_hash": r.get("cp_hash") or "",
                "description": (r.get("description") or "")[:300]}
        if r.get("source_kind") == "promoted":
            actions.append({**base, "new_hash": base["old_hash"], "action": "skip-promoted",
                            "collision_group": ""})
            continue
        if not code:
            actions.append({**base, "new_hash": base["old_hash"], "action": "skip-no-project",
                            "collision_group": ""})
            continue
        h = ask_hash(code, r.get("description") or "")
        groups[(code, h)].append({**base, "_created": r.get("created_at") or "",
                                  "_full": r.get("description") or ""})
    for (code, h), members in groups.items():
        members.sort(key=lambda m: (m["status"] != "open", m["_created"], m["id"]))
        group = h if len(members) > 1 else ""
        for n, m in enumerate(members):
            m.pop("_created")
            new = ask_hash(code, m.pop("_full"), occurrence=n)
            if n:
                action = "rekey-salted"
            elif not m["old_hash"]:
                action = "assign"
            elif m["old_hash"] == new:
                action = "keep"
            else:
                action = "rekey"
            actions.append({**m, "new_hash": new, "action": action,
                            "collision_group": group})
    return actions


def check_unique(actions: list[dict]) -> list[str]:
    """Final hashes that would still collide across the WHOLE table (two
    workstreams' 8-hex prefixes meeting) — the index would refuse them."""
    seen = collections.Counter(a["new_hash"] for a in actions if a["new_hash"])
    return sorted(h for h, n in seen.items() if n > 1)


def apply(client, actions: list[dict]) -> int:
    changing = [a for a in actions if a["action"] in ("rekey", "rekey-salted", "assign")]
    for a in changing:  # phase 1: park on a value no recipe produces
        client.table("commitments").update({"cp_hash": f"rk-{a['id']}"}).eq("id", a["id"]).execute()
    for a in changing:  # phase 2: the final hash
        client.table("commitments").update({"cp_hash": a["new_hash"]}).eq("id", a["id"]).execute()
    return len(changing)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tenant", type=Path, default=Path.home() / "Documents/Python/cp")
    ap.add_argument("--out", type=Path, default=Path("rekey_commitment_hashes.csv"))
    ap.add_argument("--apply", action="store_true", help="write the new hashes (default: dry run)")
    args = ap.parse_args(argv)

    client = mc2_client(args.tenant)
    projects = fetch_all(client, "projects", "id, full_job_name")
    rows = fetch_all(client, "commitments", COLUMNS)
    actions = plan_rekey(projects, rows)

    fields = ["action", "id", "code", "status", "source_kind", "old_hash", "new_hash",
              "collision_group", "description"]
    with args.out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(actions, key=lambda a: (a["action"], a["code"], a["new_hash"])))

    counts = collections.Counter(a["action"] for a in actions)
    groups = {a["collision_group"] for a in actions if a["collision_group"]}
    clashes = check_unique(actions)
    print(f"commitments: {len(rows)}")
    for k in ("keep", "rekey", "assign", "rekey-salted", "skip-promoted", "skip-no-project"):
        print(f"  {k:16} {counts.get(k, 0)}")
    print(f"collision groups (same workstream, same normalized text): {len(groups)} "
          f"covering {sum(1 for a in actions if a['collision_group'])} rows")
    print(f"cross-table hash clashes after re-key: {len(clashes)}"
          + (f" — {', '.join(clashes[:10])}" if clashes else ""))
    print(f"csv: {args.out}")
    if not args.apply:
        print("dry run — nothing written (pass --apply to write)")
        return 0
    if clashes:
        print("refusing to apply: resolve the clashes above first", file=sys.stderr)
        return 2
    print(f"applied: {apply(client, actions)} rows re-keyed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
