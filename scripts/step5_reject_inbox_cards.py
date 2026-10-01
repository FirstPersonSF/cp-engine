"""Step 5a, one time: dismiss every spine_inbox card still ``proposed``.

The auto-ingest webhook wrote a *proposed* ``spine_inbox`` card per project
per tagged meeting (a model distillation for a human to frame and promote).
Almost none were framed: 128 sat ``proposed`` on 2026-09-30, against 32
promoted and 39 dismissed in 90 days. Step 5a turned the writer off; this
clears the backlog by setting ``status = 'dismissed'`` (the lifecycle's
"a human rejected it" state, mc-2 mig 064) on every ``proposed`` row. Nothing
is deleted, and no spine substance is touched.

DRY RUN BY DEFAULT: prints the count by project and a sample of 10, and
writes nothing. ``--apply`` performs the update, guarded on
``status = 'proposed'`` so a card framed or promoted since the read is left
alone.

Usage:
    python scripts/step5_reject_inbox_cards.py [--tenant ~/Documents/Python/cp]
    python scripts/step5_reject_inbox_cards.py --tenant ... --apply
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Explicit columns only — `raw_distillation` is a model-written text blob and
# is never needed to decide a dismissal.
COLUMNS = "id, project_code, source_ref, status, created_at"
TABLE = "spine_inbox"


def mc2_client(tenant: Path):
    from cp_engine import mc2_db
    from cp_engine.config import load

    return mc2_db.get_client(load(tenant))


def fetch_proposed(client) -> list[dict]:
    """Every ``proposed`` card, paged (PostgREST caps a page at 1000)."""
    rows, start = [], 0
    while True:
        page = (
            client.table(TABLE)
            .select(COLUMNS)
            .eq("status", "proposed")
            .order("created_at")
            .range(start, start + 999)
            .execute()
            .data
            or []
        )
        rows += page
        if len(page) < 1000:
            return rows
        start += 1000


def summarize(rows: list[dict]) -> str:
    by_project = collections.Counter(r.get("project_code") or "?" for r in rows)
    lines = [f"spine_inbox cards with status='proposed': {len(rows)}"]
    for code, n in sorted(by_project.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  {code:<48} {n:>4}")
    lines.append("sample (first 10 by created_at):")
    for r in rows[:10]:
        lines.append(
            f"  {str(r.get('created_at') or '')[:10]}  {r.get('project_code')}  "
            f"{r.get('id')}"
        )
    return "\n".join(lines)


def apply(client, rows: list[dict]) -> int:
    """Dismiss in batches of 100, guarded on ``status = 'proposed'`` so a card
    that changed state since the read is not overwritten."""
    ids = [r["id"] for r in rows]
    done = 0
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        res = (
            client.table(TABLE)
            .update({"status": "dismissed"})
            .in_("id", batch)
            .eq("status", "proposed")
            .execute()
        )
        done += len(res.data or [])
    return done


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tenant", type=Path, default=Path.home() / "Documents/Python/cp")
    ap.add_argument("--apply", action="store_true",
                    help="set status='dismissed' on every proposed card (default: dry run)")
    args = ap.parse_args(argv)

    client = mc2_client(args.tenant)
    rows = fetch_proposed(client)
    print(summarize(rows))
    if not rows:
        return 0
    if not args.apply:
        print("dry run — nothing written (pass --apply to dismiss them)")
        return 0
    n = apply(client, rows)
    print(f"applied: {n} of {len(rows)} card(s) dismissed")
    return 0 if n == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
