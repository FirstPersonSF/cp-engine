"""Commitment due-date validation and the owner column are the engine's
(arch plan step 1c; inventory H14 validator, H23)."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "http://example.invalid")
    mp.setenv("SUPABASE_ANON_KEY", "x")
    import server as mod

    yield mod
    mp.undo()


@pytest.mark.parametrize("raw,expected", [
    (None, None), ("", None), ("   ", None), ("2026-10-01", "2026-10-01"),
    (" 2026-10-01 ", "2026-10-01"), ("Fri 10/2", None), ("2026-02-30", None),
    (date(2026, 10, 1), "2026-10-01"),
])
def test_valid_due_date_matches_the_engine(server, raw, expected):
    from cp_engine.commitments import _valid_due_date

    assert server.valid_due_date(raw) == expected
    if isinstance(raw, str) or raw is None:
        assert server.valid_due_date(raw) == _valid_due_date(raw)


def test_owner_columns_is_the_engines(server):
    from cp_engine import mc2_db

    assert server._owner_columns(None) == (mc2_db.owner_columns(None),) == ("project_id",)


def test_the_copies_call_the_engine(server, monkeypatch):
    """Identity through behaviour: patch the engine names the module bound."""
    monkeypatch.setattr(server, "_engine_valid_due_date", lambda raw: "SENTINEL")
    assert server.valid_due_date("2026-10-01") == "SENTINEL"
