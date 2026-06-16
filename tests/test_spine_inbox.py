"""Tests for the spine ingestion inbox (Phase 3)."""

from pathlib import Path

import pytest

from cp_engine.spine_inbox import (
    InboxCard,
    card_to_row,
    proposed_card,
    row_to_card,
)


# ---- Task 3.1: inbox card model --------------------------------------------


def test_proposed_card_id_composition():
    card = proposed_card(
        project_id="u1",
        project_code="ibx-5153",
        source_ref="mtg-42",
        raw_distillation="raw first pass",
    )
    assert card.id == "ibx-5153/inbox/mtg-42"


def test_proposed_card_status_default_and_guesses_none():
    card = proposed_card(
        project_id="u1",
        project_code="ibx-5153",
        source_ref="mtg-42",
        raw_distillation="raw",
    )
    assert card.status == "proposed"
    assert card.guessed_est_item_id is None
    assert card.guessed_type is None
    assert card.framing is None


def test_proposed_card_carries_guesses():
    card = proposed_card(
        project_id="u1",
        project_code="ibx-5153",
        source_ref="mtg-42",
        raw_distillation="raw",
        guessed_est_item_id="d1",
        guessed_type="deliverable",
    )
    assert card.guessed_est_item_id == "d1"
    assert card.guessed_type == "deliverable"


def test_inbox_card_is_frozen():
    card = proposed_card(
        project_id="u1", project_code="p", source_ref="s",
        raw_distillation="r",
    )
    with pytest.raises(Exception):
        card.status = "promoted"  # type: ignore[misc]


def test_card_to_row_round_trip():
    card = proposed_card(
        project_id="u1",
        project_code="ibx-5153",
        source_ref="mtg-42",
        raw_distillation="raw first pass",
        guessed_est_item_id="d1",
        guessed_type="deliverable",
    )
    row = card_to_row(card)
    assert row["id"] == "ibx-5153/inbox/mtg-42"
    assert row["project_id"] == "u1"
    assert row["project_code"] == "ibx-5153"
    assert row["source_ref"] == "mtg-42"
    assert row["raw_distillation"] == "raw first pass"
    assert row["guessed_est_item_id"] == "d1"
    assert row["guessed_type"] == "deliverable"
    assert row["status"] == "proposed"
    assert row["framing"] is None
    back = row_to_card(row)
    assert back == card


def test_row_to_card_tolerates_extra_columns():
    row = {
        "id": "p/inbox/s",
        "project_id": "u1",
        "project_code": "p",
        "source_ref": "s",
        "raw_distillation": "r",
        "guessed_est_item_id": None,
        "guessed_type": None,
        "status": "framed",
        "framing": "a brief",
        "created_at": "2026-06-15T00:00:00Z",
        "updated_at": "2026-06-15T00:00:00Z",
    }
    card = row_to_card(row)
    assert card.status == "framed"
    assert card.framing == "a brief"
    assert card.project_code == "p"
