"""Tests for webhook/relations_propose.propose_responds_to_edge (mig 117).

The frame-promote arm of the relationship-layer auto-proposal: a promoted
inbox card that minted a new authored element the distiller matched to an
existing item gets a `responds_to` PROPOSAL into spine_relations. Mock style
mirrors tests/test_cross_project.py's chained-builder MagicMocks.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

from relations_propose import propose_responds_to_edge  # noqa: E402


def _client(*, existing: list[dict] | None = None) -> MagicMock:
    client = MagicMock()
    # The dup check: .select(...).eq().eq().eq().eq().execute().data
    (
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data
    ) = (existing or [])
    return client


def _last_insert(client) -> dict:
    calls = client.table.return_value.insert.call_args_list
    assert calls, "no insert() was called"
    return calls[-1].args[0]


def test_proposes_responds_to_for_inbound_element() -> None:
    client = _client()
    out = propose_responds_to_edge(
        client,
        project_id="p1",
        project_code="ibx-5153",
        new_est_item_id="_authored/janet-feedback-07-16",
        guessed_est_item_id="_authored/three-campaign-directions",
        kind="email",
    )
    assert out["proposed"] == 1
    row = _last_insert(client)
    assert row["kind"] == "responds_to"
    assert row["from_item_id"] == "_authored/janet-feedback-07-16"
    assert row["to_item_id"] == "_authored/three-campaign-directions"
    assert row["status"] == "proposed"
    assert row["source"] == "auto_ingest"
    assert row["project_id"] == "p1"
    assert row["confidence"] == 0.7


def test_no_proposal_when_no_guessed_target() -> None:
    client = _client()
    out = propose_responds_to_edge(
        client,
        project_id="p1",
        project_code="ibx-5153",
        new_est_item_id="_authored/some-note",
        guessed_est_item_id=None,
        kind="email",
    )
    assert out["proposed"] == 0
    client.table.return_value.insert.assert_not_called()


def test_no_self_edge() -> None:
    client = _client()
    out = propose_responds_to_edge(
        client,
        project_id="p1",
        project_code="ibx-5153",
        new_est_item_id="_authored/x",
        guessed_est_item_id="_authored/x",
        kind="email",
    )
    assert out["proposed"] == 0
    client.table.return_value.insert.assert_not_called()


def test_deliverable_kind_does_not_propose() -> None:
    # A promoted deliverable/output is the thing REACTED TO, not a reactor.
    client = _client()
    out = propose_responds_to_edge(
        client,
        project_id="p1",
        project_code="ibx-5153",
        new_est_item_id="_authored/a-new-deck",
        guessed_est_item_id="_authored/some-brief",
        kind="deliverable",
    )
    assert out["proposed"] == 0
    client.table.return_value.insert.assert_not_called()


def test_idempotent_when_edge_exists() -> None:
    client = _client(existing=[{"id": "e1", "status": "dismissed"}])
    out = propose_responds_to_edge(
        client,
        project_id="p1",
        project_code="ibx-5153",
        new_est_item_id="_authored/janet-feedback",
        guessed_est_item_id="_authored/directions",
        kind="email",
    )
    assert out["proposed"] == 0
    # A dismissed guess must stay dead — no re-insert.
    client.table.return_value.insert.assert_not_called()


def test_error_is_swallowed() -> None:
    client = MagicMock()
    client.table.side_effect = RuntimeError("supabase down")
    out = propose_responds_to_edge(
        client,
        project_id="p1",
        project_code="ibx-5153",
        new_est_item_id="_authored/x",
        guessed_est_item_id="_authored/y",
        kind="email",
    )
    # Best-effort: never raises, records the reason.
    assert out["proposed"] == 0
    assert out["reason"].startswith("error:")
