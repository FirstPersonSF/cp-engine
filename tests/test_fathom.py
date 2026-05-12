"""Tests for cp_engine.fathom — v0.8.7 Fathom bridge.

Three parts:
- has_good_tags: confidence-gate logic.
- State file: load/save/already_processed/mark_processed round-trip.
- stage_transcript: writes to correct path with proper header.

The Supabase-querying functions (list_meetings, fetch_meeting) aren't
unit-tested with mocks here — they're thin wrappers around the supabase
client and get exercised end-to-end in the live-tenant verification step.
"""

from __future__ import annotations

import json
from pathlib import Path

from cp_engine.fathom import (
    FathomMeetingFull,
    FathomStateFile,
    NEEDS_REVIEW_DIR,
    INCOMING_DIR,
    already_processed,
    has_good_tags,
    load_state,
    mark_processed,
    save_state,
    stage_transcript,
)


# ──────────────────────────────────────────────────────────────────────
#  has_good_tags
# ──────────────────────────────────────────────────────────────────────


def test_has_good_tags_returns_false_for_empty() -> None:
    assert has_good_tags([]) is False


def test_has_good_tags_returns_false_for_untagged_only() -> None:
    assert has_good_tags(["untagged"]) is False
    assert has_good_tags(["Untagged"]) is False
    assert has_good_tags(["", "untagged"]) is False


def test_has_good_tags_returns_true_when_real_tag_present() -> None:
    assert has_good_tags(["ggl-5168"]) is True
    assert has_good_tags(["untagged", "ggl-5168"]) is True
    assert has_good_tags(["storyos", "untagged"]) is True


def test_has_good_tags_handles_none_safely() -> None:
    # Defensive: list-with-empty-string shouldn't tip the gate.
    assert has_good_tags([""]) is False


# ──────────────────────────────────────────────────────────────────────
#  State file
# ──────────────────────────────────────────────────────────────────────


def test_load_state_returns_empty_when_missing(tmp_path: Path) -> None:
    state = load_state(tmp_path)
    assert state.last_polled_at is None
    assert state.processed_ids == []


def test_load_state_returns_empty_when_corrupt(tmp_path: Path) -> None:
    (tmp_path / ".cp-engine").mkdir()
    (tmp_path / ".cp-engine" / "state.json").write_text("not-json{")
    state = load_state(tmp_path)
    assert state.last_polled_at is None
    assert state.processed_ids == []


def test_save_and_load_state_round_trip(tmp_path: Path) -> None:
    s1 = FathomStateFile(
        last_polled_at="2026-05-12T10:00:00+00:00",
        processed_ids=["id-1", "id-2"],
    )
    save_state(s1, tmp_path)
    s2 = load_state(tmp_path)
    assert s2.last_polled_at == s1.last_polled_at
    assert s2.processed_ids == s1.processed_ids


def test_save_state_preserves_non_fathom_keys(tmp_path: Path) -> None:
    """State file may grow other top-level keys in future releases; don't
    clobber them when writing the fathom subkey."""
    (tmp_path / ".cp-engine").mkdir()
    initial = {"other": {"some": "value"}}
    (tmp_path / ".cp-engine" / "state.json").write_text(json.dumps(initial))

    state = FathomStateFile(last_polled_at="2026-05-12T10:00:00Z", processed_ids=["x"])
    save_state(state, tmp_path)

    raw = json.loads((tmp_path / ".cp-engine" / "state.json").read_text())
    assert raw["other"] == {"some": "value"}
    assert raw["fathom"]["processed_ids"] == ["x"]


def test_already_processed_checks_id_membership() -> None:
    s = FathomStateFile(processed_ids=["a", "b"])
    assert already_processed(s, "a") is True
    assert already_processed(s, "c") is False


def test_mark_processed_adds_id_and_updates_timestamp() -> None:
    s = FathomStateFile(last_polled_at="2026-01-01", processed_ids=["a"])
    s2 = mark_processed(s, "b", polled_at="2026-05-12")
    assert s2.processed_ids == ["a", "b"]
    assert s2.last_polled_at == "2026-05-12"


def test_mark_processed_is_idempotent_on_known_id() -> None:
    s = FathomStateFile(processed_ids=["a"])
    s2 = mark_processed(s, "a", polled_at="2026-05-12")
    assert s2.processed_ids == ["a"]  # no duplicate
    assert s2.last_polled_at == "2026-05-12"


# ──────────────────────────────────────────────────────────────────────
#  stage_transcript
# ──────────────────────────────────────────────────────────────────────


def _make_meeting(transcript: str = "0:00 - Drew\n  Hello.", **kwargs) -> FathomMeetingFull:
    defaults = dict(
        id="abc-123",
        title="Test Meeting",
        meeting_date="2026-05-12T17:00:00+00:00",
        project_tags=["ggl-5168"],
        transcript=transcript,
        summary=None,
        duration_minutes=25,
    )
    defaults.update(kwargs)
    return FathomMeetingFull(**defaults)  # type: ignore[arg-type]


def test_stage_transcript_writes_to_incoming_by_default(tmp_path: Path) -> None:
    meeting = _make_meeting()
    path = stage_transcript(meeting, tenant_root=tmp_path)
    assert path == tmp_path / INCOMING_DIR / "abc-123.txt"
    assert path.is_file()


def test_stage_transcript_writes_to_needs_review_when_flagged(tmp_path: Path) -> None:
    meeting = _make_meeting(project_tags=["untagged"])
    path = stage_transcript(meeting, tenant_root=tmp_path, needs_review=True)
    assert path == tmp_path / NEEDS_REVIEW_DIR / "abc-123.txt"


def test_stage_transcript_includes_metadata_header(tmp_path: Path) -> None:
    meeting = _make_meeting(
        title="1P Weekly Scrum", project_tags=["ggl-5168", "ggl-5176"]
    )
    path = stage_transcript(meeting, tenant_root=tmp_path)
    body = path.read_text()
    assert "# Fathom meeting: 1P Weekly Scrum" in body
    assert "# id: abc-123" in body
    assert "# project_tags: ggl-5168, ggl-5176" in body
    assert "# duration_minutes: 25" in body
    # And the transcript itself follows the header (sentinel: the
    # transcript content we passed in).
    assert "0:00 - Drew" in body


def test_stage_transcript_handles_empty_transcript(tmp_path: Path) -> None:
    meeting = _make_meeting(transcript="")
    path = stage_transcript(meeting, tenant_root=tmp_path)
    body = path.read_text()
    # Header still present, plus a placeholder for the empty body.
    assert "# id: abc-123" in body
    assert "(no transcript)" in body
