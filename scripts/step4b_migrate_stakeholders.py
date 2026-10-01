#!/usr/bin/env python3
"""Step 4b, one time: every person in the retired markdown stores gets a
spine Stakeholders card in MC-2 (``cp_engine.stakeholder_import``).

Reads the tree (cp.md ``## Stakeholders`` sections; every week's sprint
``### Stakeholders`` subsection) and production MC-2 (explicit columns only):
live Stakeholders cards, ``projects.company_id``, company names, and the
staff/freelancer entities that make someone internal. Prints the plan — per
person: create / update (fill the card's details block, record spellings) /
match (nothing to add), and promote (project → account scope) — plus every
canonicalization, skip and first-name merge.

DRY RUN BY DEFAULT: writes nothing anywhere (``--out`` writes the plan JSON
to a path you name, outside the tenant). ``--apply`` writes MC-2 cards; it
does not touch the tree — the next ``cxp sync`` renders the cards and retires
the sections (its import then matches every person to the card this run made).

    python scripts/step4b_migrate_stakeholders.py --tenant ~/Documents/Python/cp
    python scripts/step4b_migrate_stakeholders.py --tenant ... --all --out plan.json
    python scripts/step4b_migrate_stakeholders.py --tenant ... --apply
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cp_engine import mc2_db  # noqa: E402
from cp_engine import stakeholders as sh  # noqa: E402
from cp_engine.config import load as load_config  # noqa: E402
from cp_engine.mc2_db import Tables  # noqa: E402
from cp_engine.stakeholder_import import (  # noqa: E402
    CANDIDATE_PAIRS,
    CANONICAL_EVIDENCE,
    apply_plan,
    collect_mentions,
    plan_migration,
    summary,
    to_json,
)
from cp_engine.state import PathEntry, load_paths_index  # noqa: E402
from cp_engine.sync import _inactive_bins, _read_mc_id  # noqa: E402


def with_inactive_dirs(root: Path, index: dict) -> dict:
    """paths.json plus every inactive working dir that carries an MC-id
    stamp (closed workstreams drop out of the index — tel-5144's Teleflex
    contacts live there)."""
    out = dict(index)
    seen = {e.mc2_id for e in index.values() if e.mc2_id}
    for bin_dir in _inactive_bins(root):
        for d in sorted(x for x in bin_dir.iterdir() if x.is_dir()):
            mc = _read_mc_id(d / "cp.md")
            if mc and mc not in seen and d.name not in out:
                seen.add(mc)
                out[d.name] = PathEntry(
                    code=d.name, path=str(d.relative_to(root)), parent=None,
                    has_agreement=False, label=None, mc2_id=mc,
                    company=d.name.split("-", 1)[0].upper(), status="inactive",
                )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tenant", type=Path, default=Path.cwd())
    ap.add_argument("--all", action="store_true",
                    help="include closed/holding workstreams (default: active only)")
    ap.add_argument("--out", type=Path, help="write the plan as JSON here")
    ap.add_argument("--apply", action="store_true", help="write MC-2 cards (default: dry run)")
    args = ap.parse_args()
    root = args.tenant.resolve()
    config = load_config(root)
    client = mc2_db.get_client(config)
    index = load_paths_index(root)
    if args.all:
        index = with_inactive_dirs(root, index)

    cards = sh.fetch_cards(client)
    companies = sh.project_companies(client)
    names = {r["id"]: r.get("name") or "" for r in
             (client.table(Tables.COMPANIES).select("id, name").execute().data or [])}
    roster = sh.Roster.build(config.team, sh.internal_names_from_mc2(client))
    mentions, unresolved = collect_mentions(root, index, include_inactive=args.all)
    plan = plan_migration(mentions, cards=cards, index=index, project_company=companies,
                          roster=roster, tenant_aliases=dict(config.name_aliases or {}),
                          company_names=names)
    plan.unresolved_codes = unresolved
    s = summary(plan)

    mode = "APPLY" if args.apply else "DRY RUN — nothing written"
    print(f"tenant {root}  ({mode}; {'all' if args.all else 'active'} workstreams)\n")
    print(f"markdown entries: {len(mentions)}   live cards in MC-2: {len(cards)}")
    print(f"people (external, after canonicalization + merges): {s['people']}")
    print(f"  create {s['create']} · update {s['update']} · match {s['match']} · "
          f"promote {s['promote']}")
    print(f"  internal people skipped (unique): {s['internal_unique']}   "
          f"not-a-person entries (unique): {s['not_a_person_unique']}\n")
    print("By home workstream:")
    for code, counts in s["by_workstream"].items():
        print(f"  {code}: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print("\nPeople:")
    for p in plan.people:
        card = f"{p.card.project_code}/{p.card.est_item_id}" if p.card else "—"
        extra = []
        if p.variants:
            extra.append("aliases " + ", ".join(sorted(p.variants)))
        if p.merged_from:
            extra.append("merged " + ", ".join(p.merged_from))
        if p.promote:
            extra.append("PROMOTE → account")
        extra += p.flags
        print(f"  [{p.action:6}] {p.name}  home={p.home_code} ({p.scope})  card={card}"
              f"  seen in {', '.join(p.codes)}" + (f"  · {'; '.join(extra)}" if extra else ""))
    print("\nCanonicalizations applied:")
    for (frm, to), n in sorted(Counter((f, t) for _c, f, t, _s in plan.canonicalized).items()):
        why = f"   [{CANONICAL_EVIDENCE[to]}]" if to in CANONICAL_EVIDENCE else ""
        print(f"  {frm!r} → {to!r} ×{n}{why}")
    print("\nCandidate same-person pairs left separate (confirm to merge):")
    for co, a, b in CANDIDATE_PAIRS:
        print(f"  {co}: {a} ~ {b}")
    print("\nSkipped:")
    for (reason, name), n in sorted(Counter((x.reason, x.name) for x in plan.skipped).items()):
        print(f"  {reason}: {name} ×{n}")
    if unresolved:
        print(f"\nSprint stems with no indexed workstream (not migrated): {len(unresolved)}")
        print("  " + ", ".join(sorted(unresolved)))
    if args.out:
        args.out.write_text(to_json(plan), encoding="utf-8")
        print(f"\nplan written to {args.out}")

    log = apply_plan(client, plan, project_company=companies, company_names=names,
                     today=date.today(), dry_run=not args.apply)
    print("\nWrites " + ("performed:" if args.apply else "that --apply would perform:"))
    for line in log:
        print("  " + line)
    if not args.apply:
        print("\nDry run. Re-run with --apply to write the cards (never touches the tree).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
