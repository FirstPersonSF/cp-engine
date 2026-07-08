"""Tests for cp_engine.commitments — the MC-2 commitments write path.

Mock style mirrors tests/test_ingest's supabase-py chained-builder mocks.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from cp_engine.commitments import (
    US_TO_THEM,
    _valid_due_date,
    resolve_commitment_owner,
    write_commitment,
)


def _client(*, rows: list[dict], hash_hit: bool = False) -> MagicMock:
    client = MagicMock()
    # Owner lookup: .select(...).eq(...).execute().data
    client.table.return_value.select.return_value.eq.return_value.execute.return_value.data = rows
    # Dedupe: .select("id").eq("cp_hash", h).limit(1).execute().data
    client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = (
        [{"id": "x"}] if hash_hit else []
    )
    return client


def _last_insert(client) -> dict:
    calls = client.table.return_value.insert.call_args_list
    assert calls, "no insert() was called"
    return calls[-1].args[0]


def test_resolve_owner_project() -> None:
    client = _client(rows=[{"id": "p1", "number": 5168}])
    owner = resolve_commitment_owner(client, "ggl-5168")
    assert owner == {"id": "p1", "code": "ggl-5168", "kind": "project"}


def test_resolve_owner_initiative() -> None:
    client = _client(rows=[{"id": "i1", "code": "mission-control"}])
    owner = resolve_commitment_owner(client, "mission-control")
    assert owner == {"id": "i1", "code": "mission-control", "kind": "initiative"}


def test_resolve_owner_missing_returns_none() -> None:
    client = _client(rows=[])
    assert resolve_commitment_owner(client, "ggl-9999") is None


def test_write_commitment_project_row_shape() -> None:
    client = _client(rows=[])
    outcome = write_commitment(
        client,
        owner={"id": "p1", "code": "ggl-5168", "kind": "project"},
        description="Deliver the thing",
        cp_hash="abcd1234",
        source_kind="meeting_ingest",
        direction=US_TO_THEM,
        owner_email="drew@firstperson.is",
        due_date="2026-07-20",
        source_meeting_id="meet-1",
    )
    assert outcome == "inserted"
    row = _last_insert(client)
    assert row["project_id"] == "p1"
    assert "initiative_id" not in row  # num_nonnulls==1 CHECK safety
    assert row["status"] == "open"
    assert row["date_status"] == "proposed"
    assert row["cp_hash"] == "abcd1234"
    assert row["due_date"] == "2026-07-20"


def test_write_commitment_initiative_owner_column() -> None:
    client = _client(rows=[])
    write_commitment(
        client,
        owner={"id": "i1", "code": "mission-control", "kind": "initiative"},
        description="Internal task",
        cp_hash="beef0001",
        source_kind="meeting_ingest",
    )
    row = _last_insert(client)
    assert row["initiative_id"] == "i1"
    assert "project_id" not in row


def test_write_commitment_duplicate_skips_insert() -> None:
    client = _client(rows=[], hash_hit=True)
    outcome = write_commitment(
        client,
        owner={"id": "p1", "code": "ggl-5168", "kind": "project"},
        description="Deliver the thing",
        cp_hash="abcd1234",
        source_kind="meeting_ingest",
    )
    assert outcome == "duplicate"
    assert not client.table.return_value.insert.called


def test_write_commitment_dedupe_failure_still_inserts() -> None:
    """Never silently drop a real commitment on a flaky dedupe lookup —
    the partial unique index on cp_hash is the backstop."""
    client = _client(rows=[])
    client.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.side_effect = (
        RuntimeError("boom")
    )
    outcome = write_commitment(
        client,
        owner={"id": "p1", "code": "ggl-5168", "kind": "project"},
        description="Deliver the thing",
        cp_hash="abcd1234",
        source_kind="meeting_ingest",
    )
    assert outcome == "inserted"
    assert client.table.return_value.insert.called


def test_valid_due_date() -> None:
    assert _valid_due_date("2026-07-20") == "2026-07-20"
    assert _valid_due_date(" 2026-07-20 ") == "2026-07-20"
    assert _valid_due_date("next Tuesday") is None
    assert _valid_due_date("") is None
    assert _valid_due_date(None) is None
