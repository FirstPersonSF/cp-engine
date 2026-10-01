"""The daily sync's 14-day TTL on undated meeting-ingest commitments (#136).

`expire_stale_commitments` closes ONLY rows the dates loop used to: undated,
`source_kind='meeting_ingest'`, `date_status='proposed'`, 14+ days old.
"""

from __future__ import annotations

from datetime import date

from cp_engine.commitments_sweep import expire_stale_commitments
from tests._spine_fake import FakeClient

TODAY = date(2026, 10, 1)


def _row(i, *, created="2026-09-10", due=None, kind="meeting_ingest",
         date_status="proposed", status="open"):
    return {"id": f"c{i}", "description": f"ask {i}", "due_date": due,
            "source_kind": kind, "date_status": date_status, "status": status,
            "created_at": f"{created}T12:00:00+00:00"}


def _client(*rows):
    return FakeClient(commitments=[dict(r) for r in rows])


def _status(client):
    return {r["id"]: r["status"] for r in client.store["commitments"]}


def test_only_stale_undated_proposed_meeting_rows_expire():
    c = _client(
        _row(1),                                   # 21 days, undated → expires
        _row(2, created="2026-09-17"),             # exactly 14 days → expires
        _row(3, created="2026-09-18"),             # 13 days → stays
        _row(4, due="2026-10-15"),                 # dated → stays
        _row(5, kind="manual"),                    # a person wrote it → stays
        _row(6, kind="session"),                   # → stays
        _row(7, date_status="agreed"),             # agreed → stays
        _row(8, status="done"),                    # already closed → stays done
    )
    assert expire_stale_commitments(c, today=TODAY) == 2
    assert _status(c) == {"c1": "expired", "c2": "expired", "c3": "open",
                          "c4": "open", "c5": "open", "c6": "open",
                          "c7": "open", "c8": "done"}


def test_a_second_run_closes_nothing():
    c = _client(_row(1))
    assert expire_stale_commitments(c, today=TODAY) == 1
    assert expire_stale_commitments(c, today=TODAY) == 0


def test_a_read_failure_raises_rather_than_reporting_zero():
    c = _client(_row(1))
    c.fail["select"] = {"commitments"}
    try:
        expire_stale_commitments(c, today=TODAY)
    except RuntimeError:
        return
    raise AssertionError("a failed read must not look like 'nothing to expire'")
