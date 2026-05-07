"""Tests for status vocabulary.

Smoke-test the canonical vocab + helper. The CI drift check (against
mc-2's status.ts and status.py) lives in a separate workflow that's
out of scope for v0.1.0.
"""

from __future__ import annotations

from cp_engine.status import (
    ACTIVE_STATUSES,
    MC_STATUS_ACTIVE,
    MC_STATUSES,
    is_active_status,
)


def test_vocabulary_exact() -> None:
    """The five canonical statuses, in order."""
    assert MC_STATUSES == ("Deal", "Open", "Holding", "Closed", "Archived")


def test_active_subset() -> None:
    """Active subset is exactly Deal ∪ Open."""
    assert ACTIVE_STATUSES == ("Deal", "Open")


def test_per_status_flag_complete() -> None:
    """Every status has an explicit active/inactive flag — no gaps."""
    assert set(MC_STATUS_ACTIVE.keys()) == set(MC_STATUSES)


def test_is_active_status_truth_table() -> None:
    assert is_active_status("Deal") is True
    assert is_active_status("Open") is True
    assert is_active_status("Holding") is False
    assert is_active_status("Closed") is False
    assert is_active_status("Archived") is False


def test_is_active_status_handles_unknown() -> None:
    assert is_active_status(None) is False
    assert is_active_status("") is False
    assert is_active_status("Active") is False  # the old vocab — must not pass
    assert is_active_status("Complete") is False  # the old vocab — must not pass


def test_is_active_status_is_case_sensitive() -> None:
    """Statuses are TitleCase; lowercase or other casing must not pass."""
    assert is_active_status("open") is False
    assert is_active_status("OPEN") is False
    assert is_active_status("deal") is False
