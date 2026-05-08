"""Tests for `cp_engine.sync_mc2` row-transformation logic.

These don't hit Supabase. They test the pure transformation of a row dict
(as Supabase would return it) into a ProjectState.

Integration tests against the real DB live in a separate suite that's
out of scope for v0.1's automated CI.
"""

from __future__ import annotations

from datetime import datetime, timezone

from cp_engine.sync_mc2 import _parse_iso, _row_is_valid, _row_to_state


# ──────────────────────────────────────────────────────────────────────
#  Row → ProjectState
# ──────────────────────────────────────────────────────────────────────


def test_row_to_state_happy_path() -> None:
    row = {
        "code": "ggl-5168",
        "full_job_name": "GGL 5168 Playbooks",
        "name": "Playbooks (Activation)",
        "mc_status": "Open",
        "account_manager": "Drew Fiero",
        "is_internal": False,
        "updated_at": "2026-05-07T16:14:34.123456+00:00",
    }
    state = _row_to_state(row)

    assert state.code == "ggl-5168"
    # Prefers full_job_name over name
    assert state.name == "GGL 5168 Playbooks"
    assert state.status == "Open"
    assert state.owner == "Drew Fiero"
    assert state.is_internal is False
    assert state.last_touched == datetime(
        2026, 5, 7, 16, 14, 34, 123456, tzinfo=timezone.utc
    )
    assert state.deadline is None
    assert state.one_line_summary is None


def test_row_to_state_falls_back_to_name_when_full_job_name_missing() -> None:
    row = {
        "code": "ggl-1111",
        "full_job_name": None,
        "name": "Just the project name",
        "mc_status": "Open",
        "account_manager": None,
        "is_internal": False,
        "updated_at": None,
    }
    state = _row_to_state(row)
    assert state.name == "Just the project name"
    assert state.owner is None
    assert state.last_touched is None


def test_row_to_state_settles_for_empty_name_when_both_null() -> None:
    row = {
        "code": "ggl-2222",
        "full_job_name": None,
        "name": None,
        "mc_status": "Open",
        "account_manager": None,
        "is_internal": False,
        "updated_at": None,
    }
    state = _row_to_state(row)
    assert state.name == ""


def test_row_to_state_internal_flag_coerces_to_bool() -> None:
    # Defensive: even though the column is NOT NULL boolean, this guards
    # against future-Supabase-returns-something-weird cases.
    row = {
        "code": "x",
        "full_job_name": "X",
        "name": "X",
        "mc_status": "Open",
        "account_manager": None,
        "is_internal": 1,  # truthy non-bool
        "updated_at": None,
    }
    assert _row_to_state(row).is_internal is True


# ──────────────────────────────────────────────────────────────────────
#  Validation guard
# ──────────────────────────────────────────────────────────────────────


def test_row_is_valid_rejects_missing_code() -> None:
    assert not _row_is_valid({"code": "", "mc_status": "Open"})
    assert not _row_is_valid({"code": None, "mc_status": "Open"})


def test_row_is_valid_rejects_old_vocab_status() -> None:
    """Defensive: if a row somehow still has the pre-migration vocab,
    skip it rather than render wrongly."""
    assert not _row_is_valid({"code": "x", "mc_status": "Active"})
    assert not _row_is_valid({"code": "x", "mc_status": "Complete"})


def test_row_is_valid_rejects_unknown_status() -> None:
    assert not _row_is_valid({"code": "x", "mc_status": "Floating"})


def test_row_is_valid_accepts_all_canonical_statuses() -> None:
    for status in ("Deal", "Open", "Holding", "Closed", "Archived"):
        assert _row_is_valid({"code": "x", "mc_status": status}), status


# ──────────────────────────────────────────────────────────────────────
#  Timestamp parsing
# ──────────────────────────────────────────────────────────────────────


def test_parse_iso_with_microseconds_and_offset() -> None:
    dt = _parse_iso("2026-05-07T16:14:34.123456+00:00")
    assert dt == datetime(2026, 5, 7, 16, 14, 34, 123456, tzinfo=timezone.utc)


def test_parse_iso_with_seconds_only() -> None:
    dt = _parse_iso("2026-05-07T16:14:34+00:00")
    assert dt is not None
    assert dt.tzinfo is timezone.utc


def test_parse_iso_returns_none_for_falsy() -> None:
    assert _parse_iso(None) is None
    assert _parse_iso("") is None


def test_parse_iso_assumes_utc_for_naive() -> None:
    """Defensive: if Supabase ever returns a naive timestamp, treat as UTC."""
    dt = _parse_iso("2026-05-07T16:14:34")
    assert dt is not None
    assert dt.tzinfo is timezone.utc
