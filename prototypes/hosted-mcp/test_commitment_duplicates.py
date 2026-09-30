"""#311 on the hosted server: link a session commitment to its meeting, and
flag likely-duplicate pairs in `commitments_sweep`.

A commitment logged by hand mid-session and the row auto-ingest later writes
for the same meeting never share text, so nothing grouped them: the sweep
showed 8 open rows for 6 obligations with no signal which pairs matched.

    python -m pytest prototypes/hosted-mcp/test_commitment_duplicates.py -v
"""
from __future__ import annotations

from test_promote_uphill import CHILD, CHILD_ID, server, wired  # noqa: F401 — fixtures

MEETING = "44444444-4444-4444-4444-444444444444"


def test_create_commitment_stores_the_meeting_link(server, wired):
    wired.store["fathom_meetings"] = [{"id": MEETING}]
    out = server.create_commitment(CHILD, "Send Janet the recut", source_meeting_id=MEETING)
    assert out.get("commitment_id"), out
    assert out["source_meeting_id"] == MEETING
    (row,) = [r for r in wired.store["commitments"] if r["id"] == out["commitment_id"]]
    # The SAME column auto-ingest fills — so resolve_commitments_by_meeting
    # and the sweep's same-meeting signal see both writers' rows.
    assert row["source_meeting_id"] == MEETING
    assert row["source_kind"] == "session"


def test_create_commitment_without_a_meeting_is_unchanged(server, wired):
    out = server.create_commitment(CHILD, "Send Janet the recut")
    (row,) = [r for r in wired.store["commitments"] if r["id"] == out["commitment_id"]]
    assert row["source_meeting_id"] is None


def test_a_malformed_meeting_id_is_rejected_not_stored(server, wired):
    before = len(wired.store["commitments"])
    out = server.create_commitment(CHILD, "Send Janet the recut", source_meeting_id="82736412")
    assert "error" in out and "list_project_meetings" in out["error"]
    assert len(wired.store["commitments"]) == before


def test_an_unknown_meeting_is_rejected_not_stored(server, wired):
    """A wrong link is worse than none: it would group this row with the
    wrong meeting's ingest rows and let resolve_commitments_by_meeting close it."""
    wired.store["fathom_meetings"] = []
    before = len(wired.store["commitments"])
    out = server.create_commitment(CHILD, "Send Janet the recut", source_meeting_id=MEETING)
    assert "error" in out and "no meeting" in out["error"]
    assert len(wired.store["commitments"]) == before


def test_sweep_returns_likely_duplicate_pairs(server, wired):
    wired.store["projects"] = [{"id": CHILD_ID, "code": CHILD}]
    wired.store["commitments"] = [
        {"id": "s1", "project_id": CHILD_ID, "status": "open", "source_kind": "session",
         "description": "Written interpretation of Brad's feedback + clarifying questions "
                        "sent to Janet for her email to Jaime/Brad",
         "created_at": "2026-09-20T12:00:00+00:00", "source_meeting_id": MEETING},
        {"id": "m1", "project_id": CHILD_ID, "status": "open", "source_kind": "meeting_ingest",
         "description": "Email to Janet + Brad with team's interpretation of Brad's "
                        "feedback and clarifying questions",
         "created_at": "2026-09-21T12:00:00+00:00", "source_meeting_id": MEETING},
        {"id": "m2", "project_id": CHILD_ID, "status": "open", "source_kind": "meeting_ingest",
         "description": "Book the Fort Worth studio for the pre-light",
         "created_at": "2026-09-21T12:00:00+00:00", "source_meeting_id": MEETING},
    ]
    out = server.commitments_sweep()
    assert "error" not in out, out
    assert out["total"] == 3
    assert out["likely_duplicate_pairs"] == 1
    (pair,) = out["likely_duplicates"][CHILD]
    assert {pair["a"], pair["b"]} == {"s1", "m1"}
    assert pair["same_meeting"] is True
    assert all(r["source_meeting_id"] == MEETING for r in out["buckets"][CHILD])
