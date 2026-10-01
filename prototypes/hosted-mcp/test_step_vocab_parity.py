"""Spine-step vocabulary and read shape are the engine's (arch plan step 1c, H12)."""

from __future__ import annotations

import sys
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


def test_step_vocabulary_is_the_engines(server):
    from cp_engine import spine_steps

    assert server.STEP_STATUSES is spine_steps.STEP_STATUSES
    assert server.STEP_NOTE_MAX is spine_steps.NOTE_MAX
    assert server._STEP_SELECT is spine_steps._STEP_SELECT


def test_propose_spine_step_rejects_what_the_engine_rejects(server):
    """Behaviour through the verb: an out-of-vocabulary status and an over-long
    note are refused with the engine's limits, before any client is built."""
    from cp_engine import spine_steps

    out = server.propose_spine_step("ggl-5188", "k", "title", status="blocked")
    assert out == {"error": f"status must be one of {list(spine_steps.STEP_STATUSES)}"}
    out = server.propose_spine_step("ggl-5188", "k", "title", note="x" * (spine_steps.NOTE_MAX + 1))
    assert out == {"error": f"note exceeds {spine_steps.NOTE_MAX} characters"}
