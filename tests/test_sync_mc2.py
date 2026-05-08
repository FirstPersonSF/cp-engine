"""Tests for `cp_engine.sync_mc2` row-transformation logic.

These don't hit Supabase. They test the pure transformation of a row dict
(as Supabase would return it via the embedded `companies(code)` join)
into a ProjectState.

Integration tests against the real DB live in a separate suite that's
out of scope for v0.1's automated CI.
"""

from __future__ import annotations

from datetime import datetime, timezone

from cp_engine.sync_mc2 import (
    _canonical_id,
    _parse_iso,
    _row_is_valid,
    _row_to_state,
)


# ──────────────────────────────────────────────────────────────────────
#  Canonical ID
# ──────────────────────────────────────────────────────────────────────


def test_canonical_id_with_company_prefix() -> None:
    assert _canonical_id({"number": 5188, "companies": {"code": "GGL"}}) == "ggl-5188"
    assert _canonical_id({"number": 5168, "companies": {"code": "ibx"}}) == "ibx-5168"
    assert _canonical_id({"number": 5176, "companies": {"code": "Snt"}}) == "snt-5176"


def test_canonical_id_lowercases_prefix() -> None:
    """Spec §3.3: codes are lowercase, hyphenated."""
    assert _canonical_id({"number": 5188, "companies": {"code": "HEX"}}) == "hex-5188"


def test_canonical_id_falls_back_to_number_when_no_company() -> None:
    """Legacy data (May 2026: ~30/57 rows) has NULL company_id."""
    assert _canonical_id({"number": 5026, "companies": None}) == "5026"
    assert _canonical_id({"number": 5026}) == "5026"  # key absent entirely


def test_canonical_id_falls_back_when_company_has_no_code() -> None:
    """Defensive: company joined but its code is empty/null."""
    assert _canonical_id({"number": 5099, "companies": {"code": ""}}) == "5099"
    assert _canonical_id({"number": 5099, "companies": {"code": None}}) == "5099"


def test_canonical_id_strips_whitespace_in_prefix() -> None:
    assert _canonical_id({"number": 1, "companies": {"code": "  GGL  "}}) == "ggl-1"


# ──────────────────────────────────────────────────────────────────────
#  Row → ProjectState
# ──────────────────────────────────────────────────────────────────────


def test_row_to_state_happy_path() -> None:
    row = {
        "number": 5168,
        "companies": {"code": "GGL"},
        "full_job_name": "GGL 5168 Playbooks",
        "name": "Playbooks (Activation)",
        "mc_status": "Open",
        "account_manager": "Drew Fiero",
        "is_internal": False,
        "updated_at": "2026-05-07T16:14:34.123456+00:00",
    }
    state = _row_to_state(row)

    assert state.code == "ggl-5168"
    assert state.name == "GGL 5168 Playbooks"  # prefers full_job_name
    assert state.status == "Open"
    assert state.owner == "Drew Fiero"
    assert state.is_internal is False
    assert state.last_touched == datetime(
        2026, 5, 7, 16, 14, 34, 123456, tzinfo=timezone.utc
    )
    assert state.deadline is None
    assert state.one_line_summary is None


def test_row_to_state_legacy_row_without_company() -> None:
    """A legacy row missing the company join still produces a valid state
    — code falls back to just the number string."""
    row = {
        "number": 5026,
        "companies": None,
        "full_job_name": None,
        "name": "SentinelOne 5107",
        "mc_status": "Deal",
        "account_manager": None,
        "is_internal": False,
        "updated_at": None,
    }
    state = _row_to_state(row)
    assert state.code == "5026"
    assert state.name == "SentinelOne 5107"
    assert state.owner is None


def test_row_to_state_falls_back_to_name_when_full_job_name_missing() -> None:
    row = {
        "number": 1111,
        "companies": {"code": "GGL"},
        "full_job_name": None,
        "name": "Just the project name",
        "mc_status": "Open",
        "account_manager": None,
        "is_internal": False,
        "updated_at": None,
    }
    state = _row_to_state(row)
    assert state.name == "Just the project name"


def test_row_to_state_settles_for_empty_name_when_both_null() -> None:
    row = {
        "number": 2222,
        "companies": {"code": "GGL"},
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
    row = {
        "number": 1,
        "companies": {"code": "X"},
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


def test_row_is_valid_rejects_missing_number() -> None:
    """A row with no number can't produce a canonical ID — skip."""
    assert not _row_is_valid({"number": None, "mc_status": "Open"})
    assert not _row_is_valid({"mc_status": "Open"})


def test_row_is_valid_rejects_old_vocab_status() -> None:
    """Defensive: if a row somehow still has the pre-migration vocab,
    skip it rather than render wrongly."""
    assert not _row_is_valid({"number": 1, "mc_status": "Active"})
    assert not _row_is_valid({"number": 1, "mc_status": "Complete"})


def test_row_is_valid_rejects_unknown_status() -> None:
    assert not _row_is_valid({"number": 1, "mc_status": "Floating"})


def test_row_is_valid_accepts_all_canonical_statuses() -> None:
    for status in ("Deal", "Open", "Holding", "Closed", "Archived"):
        assert _row_is_valid({"number": 1, "mc_status": status}), status


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
    dt = _parse_iso("2026-05-07T16:14:34")
    assert dt is not None
    assert dt.tzinfo is timezone.utc
