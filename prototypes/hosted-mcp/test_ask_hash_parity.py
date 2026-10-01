"""Step 4a on the hosted server: `create_commitment` hashes with the engine.

Before 4a this verb wrote `uuid4().hex[:8]` on purpose, so a commitment
logged mid-session could never hash-match the sprint bullet or the meeting
row for the same ask (216 unreproducible hashes on 2026-10-01). It now calls
`cp_engine.asks.ask_hash`. Control: on the pre-4a server the first test fails
(random hash) and the repeat test inserts a second open row.

    python -m pytest prototypes/hosted-mcp/test_ask_hash_parity.py -v
"""
from __future__ import annotations

import re

from test_promote_uphill import CHILD, CHILD_ID, server, wired  # noqa: F401 — fixtures


def test_hosted_and_engine_hashes_agree(server, wired, tmp_path):
    from cp_engine.ingest import _write_ask

    out = server.create_commitment(CHILD, "Send Janet the recut")
    (row,) = [r for r in wired.store["commitments"] if r["id"] == out["commitment_id"]]

    # The engine's sprint-file writer, on the same workstream's sprint file.
    sprint = tmp_path / f"{CHILD}.md"
    sprint.write_text("## Client communication\n### Open asks\n")
    _write_ask(CHILD, {"text": "Send Janet the recut", "who": "Drew", "date": "2026-10-01"}, sprint)
    bullet = re.search(r"cp:hash=([0-9a-f]{8})", sprint.read_text()).group(1)

    assert row["cp_hash"] == bullet


def test_recreating_an_open_ask_returns_it_instead_of_a_second_row(server, wired):
    first = server.create_commitment(CHILD, "Send Janet the recut")
    again = server.create_commitment(CHILD, "send janet the recut.")
    assert again.get("duplicate_of") == first["commitment_id"]
    rows = [r for r in wired.store["commitments"]
            if r.get("project_id") == CHILD_ID and "recut" in r["description"].lower()]
    assert len(rows) == 1


def test_recreating_a_closed_ask_is_a_new_life_under_a_salted_hash(server, wired):
    """A deliberate repeat of a done ask still lands — the reason the hash was
    random — but the closed row keeps the unsalted hash, so a re-ingest of the
    original meeting cannot resurrect it."""
    from cp_engine.asks import ask_hash

    first = server.create_commitment(CHILD, "Send the weekly status note")
    (row,) = [r for r in wired.store["commitments"] if r["id"] == first["commitment_id"]]
    row["status"] = "done"
    again = server.create_commitment(CHILD, "Send the weekly status note")
    assert "duplicate_of" not in again, again
    (new,) = [r for r in wired.store["commitments"] if r["id"] == again["commitment_id"]]
    assert new["status"] == "open"
    assert row["cp_hash"] == ask_hash(CHILD, "Send the weekly status note")
    assert new["cp_hash"] == ask_hash(CHILD, "Send the weekly status note", occurrence=1)
