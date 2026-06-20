#!/usr/bin/env python3
"""Build a READ-ONLY old->new engagement-id map for the canonical-id migration.

Tasks 1-3 of the "Canonical Project ID from full_job_name" feature flipped the
canonical engagement id from the legacy bare `<company>-<number>` form (e.g.
`ibx-5192`) to the full slug derived from `full_job_name` (e.g.
`ibx-5192-platform-sales-readiness-summit`) via
`cp_engine.sync_mc2._engagement_canonical_id`.

This script computes the rename map a later (gated) migration script will consume
to git-mv working dirs and rewrite references. It writes NOTHING — no database
mutations, no tree changes. It SELECTs active engagement rows read-only and
prints the `old -> new` pairs that actually changed.

`build_id_map(rows)` is pure and importable for unit tests. `main()` only runs
under `if __name__ == "__main__"` and hits the DB read-only; run it from the cp
tenant working dir (creds resolve the same way `cp sync` resolves them: env
first, then the mc-2 clone's backend/.env).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running as a plain script (`python scripts/full_job_name_id_map.py`)
# without an editable install: add the package's src/ to the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cp_engine.sync_mc2 import _engagement_canonical_id  # noqa: E402


def build_id_map(rows) -> dict[str, str]:
    """Map each engagement's legacy id to its new canonical id, when it changed.

    For each row:
      * ``new`` = ``_engagement_canonical_id(row)`` (the full-slug form).
      * ``old`` = the legacy ``<company-lower>-<number>`` form, built inline from
        ``row["companies"]["code"]`` + ``row["number"]``.

    A pair is included ONLY when ``old != new`` (unchanged rows are skipped).
    Rows where an old id can't be formed (missing company code or number) are
    skipped.
    """
    id_map: dict[str, str] = {}
    for row in rows:
        company = (row.get("companies") or {}).get("code")
        number = row.get("number")
        if not company or not number:
            continue
        old = f"{str(company).lower()}-{number}"
        new = _engagement_canonical_id(row)
        if old != new:
            id_map[old] = new
    return id_map


def main() -> None:
    """Load active engagements READ-ONLY and print the old->new id map."""
    from supabase import create_client

    from cp_engine.config import load
    from cp_engine.sync_mc2 import _load_supabase_creds

    cfg = load(Path.cwd())
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

    id_map = build_id_map(rows)
    for old, new in sorted(id_map.items()):
        print(f"{old} -> {new}")
    print(f"\n{len(id_map)} id(s) would change (out of {len(rows)} active engagement row(s)).")


if __name__ == "__main__":
    main()
