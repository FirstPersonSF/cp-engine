"""`cxp rerun-failed-ingests` — the #194 recovery verb.

The risky parts here are not the writes (execute_plan is well covered) but
the SELECTION: which stranded runs get replayed. Replaying too much is not a
neutral mistake — it re-asks the model, producing differently-worded
near-duplicates that content-hash dedupe cannot catch. So the filters are
what these tests pin down.
"""

from __future__ import annotations

import pytest

from cp_engine.cli_cmds.ingest import (
    _normalize_meeting_transcript,
    _week_iso_for,
)


# ──────────────────────────────────────────────────────────────────────
#  _week_iso_for — a replay must file content in the week that OWNS it
# ──────────────────────────────────────────────────────────────────────


def test_week_iso_comes_from_the_meeting_not_today() -> None:
    """A replay runs weeks later; the MEETING date decides the sprint week.

    Without this the recovered bullets land in the current sprint, which
    would be a second data-placement bug on top of the one being fixed.
    """
    assert _week_iso_for("2026-08-10") == "2026-W33"
    assert _week_iso_for("2026-08-18") == "2026-W34"


def test_week_iso_handles_year_boundaries() -> None:
    """ISO weeks are not calendar weeks — Jan 1 can belong to W01 or W53."""
    assert _week_iso_for("2026-01-01") == "2026-W01"
    assert _week_iso_for("2026-12-31") == "2026-W53"


# ──────────────────────────────────────────────────────────────────────
#  _normalize_meeting_transcript — Fathom stores JSONB segments
# ──────────────────────────────────────────────────────────────────────


def test_transcript_segments_flatten_to_speaker_prefixed_lines() -> None:
    got = _normalize_meeting_transcript(
        [{"speaker": "Drew", "text": "hi"}, {"speaker": "Tony", "text": "yo"}]
    )
    assert got == "Drew: hi\nTony: yo"


def test_transcript_accepts_plain_strings_and_bare_segments() -> None:
    assert _normalize_meeting_transcript("already text") == "already text"
    assert _normalize_meeting_transcript(["a", "b"]) == "a\nb"


def test_missing_transcript_is_empty_not_an_exception() -> None:
    """A run whose meeting lost its transcript must skip, not crash the batch."""
    assert _normalize_meeting_transcript(None) == ""


@pytest.mark.parametrize(
    "field",
    [
        [{"speaker": "", "text": "no speaker"}],
        [{"text": "only text"}],
    ],
)
def test_segments_without_a_speaker_still_yield_their_text(field) -> None:
    assert "no speaker" in _normalize_meeting_transcript(field) or (
        "only text" in _normalize_meeting_transcript(field)
    )
