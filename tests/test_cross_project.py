"""Tests for cp_engine.cross_project — the MC-2 routing-proposal store (#88).

Mock style mirrors tests/test_commitments.py's supabase chained-builder
MagicMocks.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cp_engine.cross_project import (
    build_routed_plan,
    decide,
    proposal_hash,
    routed_item_text,
    write_proposal,
)


def _client(*, owner_rows: list[dict], hash_hit: bool = False) -> MagicMock:
    client = MagicMock()
    # Owner lookup: .select(...).eq(...).execute().data
    client.table.return_value.select.return_value.eq.return_value.execute.return_value.data = owner_rows
    # Dedupe: .select("id").eq("cp_hash", h).limit(1).execute().data
    client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = (
        [{"id": "x"}] if hash_hit else []
    )
    return client


def _last_insert(client) -> dict:
    calls = client.table.return_value.insert.call_args_list
    assert calls, "no insert() was called"
    return calls[-1].args[0]


def test_proposal_hash_is_target_scoped() -> None:
    a = proposal_hash("sap-5174", "decisions", "Customer sessions on the 28th")
    b = proposal_hash("ibx-5192", "decisions", "Customer sessions on the 28th")
    assert a != b
    assert len(a) == 8


def test_write_proposal_project_row_shape() -> None:
    client = _client(owner_rows=[{"id": "p1", "number": 5174}])
    outcome = write_proposal(
        client,
        meeting_id="m-1",
        source_code="ibx-5192",
        target_code="sap-5174",
        verb="decisions",
        text="Customer sessions on the 28th",
        confidence="high",
        item={"text": "Customer sessions on the 28th", "date": "2026-07-15"},
    )
    assert outcome == "inserted"
    row = _last_insert(client)
    assert row["source_code"] == "ibx-5192"
    assert row["target_code"] == "sap-5174"
    assert row["target_project_id"] == "p1"
    assert "target_initiative_id" not in row
    assert row["status"] == "pending"
    assert row["verb"] == "decisions"
    assert row["cp_hash"] == proposal_hash(
        "sap-5174", "decisions", "Customer sessions on the 28th"
    )
    assert row["payload"]["date"] == "2026-07-15"


def test_write_proposal_initiative_target() -> None:
    client = _client(owner_rows=[{"id": "i1", "code": "storyos"}])
    outcome = write_proposal(
        client,
        meeting_id=None,
        source_code="slt-5175",
        target_code="storyos",
        verb="asks",
        text="Allocation call for StoryOS",
        confidence="medium",
    )
    assert outcome == "inserted"
    row = _last_insert(client)
    assert row["target_initiative_id"] == "i1"
    assert "target_project_id" not in row


def test_write_proposal_duplicate_and_unresolvable() -> None:
    dup = _client(owner_rows=[{"id": "p1", "number": 5174}], hash_hit=True)
    assert (
        write_proposal(
            dup, meeting_id=None, source_code="a-1111", target_code="sap-5174",
            verb="risks", text="x", confidence="high",
        )
        == "duplicate"
    )
    dup.table.return_value.insert.assert_not_called()

    unresolvable = _client(owner_rows=[])
    assert (
        write_proposal(
            unresolvable, meeting_id=None, source_code="a-1111",
            target_code="zzz-9999", verb="risks", text="x", confidence="high",
        )
        == "unresolvable"
    )
    unresolvable.table.return_value.insert.assert_not_called()


def test_write_proposal_rejects_unknown_verb() -> None:
    with pytest.raises(ValueError):
        write_proposal(
            MagicMock(), meeting_id=None, source_code="a-1111",
            target_code="sap-5174", verb="stakeholders", text="x",
            confidence="high",
        )


def test_decide_validates_status_and_stamps() -> None:
    client = MagicMock()
    decide(client, "prop-1", "accepted", routed_commit_sha="abc123")
    patch = client.table.return_value.update.call_args.args[0]
    assert patch["status"] == "accepted"
    assert patch["routed_commit_sha"] == "abc123"
    assert patch["decided_at"]

    with pytest.raises(ValueError):
        decide(client, "prop-1", "pending")


def test_routed_item_text_and_plan() -> None:
    proposal = {
        "meeting_id": "0123456789abcdef",
        "source_code": "ibx-5192",
        "target_code": "sap-5174",
        "verb": "decisions",
        "text": "Customer sessions on the 28th",
        "payload": {"text": "Customer sessions on the 28th", "date": "2026-07-15"},
    }
    text = routed_item_text(proposal)
    assert text == (
        "Customer sessions on the 28th "
        "[cross-routed from ibx-5192 · meeting 01234567]"
    )
    plan = build_routed_plan(proposal)
    items = plan["projects"]["sap-5174"]["decisions"]
    assert len(items) == 1
    assert items[0]["text"] == text
    assert items[0]["date"] == "2026-07-15"


def test_routed_item_text_without_meeting() -> None:
    proposal = {
        "meeting_id": None,
        "source_code": "ibx-5192",
        "target_code": "sap-5174",
        "verb": "asks",
        "text": "Send the workshop deck",
        "payload": {},
    }
    assert routed_item_text(proposal) == (
        "Send the workshop deck [cross-routed from ibx-5192]"
    )
