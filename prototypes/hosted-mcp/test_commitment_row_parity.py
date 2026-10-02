"""Hosted `create_commitment` inserts the ENGINE's commitments row (H14).

The hosted verb used to build its INSERT row by hand beside
`cp_engine.commitments.write_commitment`'s — two copies of the columns and
defaults of one table. Both now come from `commitment_row`; this pins that
the row hosted inserts is exactly the one the engine builds for the same
resolved inputs, so a column or default added to one writer reaches both.

    python -m pytest prototypes/hosted-mcp/test_commitment_row_parity.py -v
"""
from __future__ import annotations

from test_promote_uphill import CHILD, CHILD_ID, server, wired  # noqa: F401 — fixtures

from cp_engine.commitments import commitment_row

MEETING = "44444444-4444-4444-4444-444444444444"


def test_hosted_inserts_the_engine_row(server, wired):
    wired.store["fathom_meetings"] = [{"id": MEETING}]
    wired.store["entities"] = [{"name": "Drew Fiero", "email": "drew@firstperson.is"}]

    out = server.create_commitment(
        CHILD, "  Send Janet the recut ", owner_email="Drew@FirstPerson.is",
        due_date="2026-10-15", direction="us_to_them", source_meeting_id=MEETING,
    )
    assert out.get("commitment_id"), out
    (row,) = [dict(r) for r in wired.store["commitments"] if r["id"] == out["commitment_id"]]
    row.pop("id")

    assert row == commitment_row(
        project_id=CHILD_ID, description="Send Janet the recut",
        cp_hash=row["cp_hash"], source_kind="session", direction="us_to_them",
        owner_email="drew@firstperson.is", owner_name="Drew Fiero",
        due_date="2026-10-15", source_meeting_id=MEETING,
    )


def test_minimal_call_matches_the_engine_defaults(server, wired):
    out = server.create_commitment(CHILD, "Book the studio")
    (row,) = [dict(r) for r in wired.store["commitments"] if r["id"] == out["commitment_id"]]
    row.pop("id")
    assert row == commitment_row(
        project_id=CHILD_ID, description="Book the studio",
        cp_hash=row["cp_hash"], source_kind="session",
    )
