"""Tests for the Retrospective layer: build_entry + append_entry."""

from __future__ import annotations

from datetime import date

import pytest

from cp_engine.retrospective import append_entry, build_entry
from cp_engine.spine import parse_element

SUMMARY = (
    "The team reviewed the Q3 campaign. Janet pushed for a sharper two-track "
    "story. We agreed to ship the foundation doc by 6/17 and to fold Carol's "
    "framework into the synthesis.\n\nKey tension: timeline vs. depth."
)


# --- build_entry ---------------------------------------------------------


def test_build_entry_embeds_whole_summary_verbatim():
    out = build_entry(
        date="2026-06-11",
        title="AI campaign workshop",
        speakers=["Janet", "Drew"],
        summary=SUMMARY,
        decisions=[],
        action_items=[],
    )
    assert SUMMARY in out


def test_build_entry_header_with_speakers():
    out = build_entry(
        date="2026-06-11",
        title="AI campaign workshop",
        speakers=["Janet", "Drew"],
        summary="x",
        decisions=[],
        action_items=[],
    )
    assert out.splitlines()[0] == "### 2026-06-11 · AI campaign workshop (Janet, Drew)"


def test_build_entry_header_without_speakers_no_empty_parens():
    out = build_entry(
        date="2026-06-11",
        title="AI campaign workshop",
        speakers=[],
        summary="x",
        decisions=[],
        action_items=[],
    )
    assert out.splitlines()[0] == "### 2026-06-11 · AI campaign workshop"
    assert "()" not in out


def test_build_entry_decisions_and_action_items_present():
    out = build_entry(
        date="2026-06-11",
        title="W",
        speakers=["A"],
        summary="x",
        decisions=["ship by 6/17", "fold in Carol's framework"],
        action_items=["Drew drafts doc", "Janet reviews"],
    )
    assert "**Decisions:** ship by 6/17 · fold in Carol's framework" in out
    assert "**Action items:** Drew drafts doc · Janet reviews" in out


def test_build_entry_omits_decisions_and_action_items_when_empty():
    out = build_entry(
        date="2026-06-11",
        title="W",
        speakers=["A"],
        summary="x",
        decisions=[],
        action_items=[],
    )
    assert "**Decisions:**" not in out
    assert "**Action items:**" not in out


def test_build_entry_links_both():
    out = build_entry(
        date="2026-06-11",
        title="W",
        speakers=["A"],
        summary="x",
        decisions=[],
        action_items=[],
        recording_url="https://fathom.video/abc",
        transcript_link="meetings/abc.txt",
    )
    assert (
        "[Fathom recording](https://fathom.video/abc) · [transcript](meetings/abc.txt)"
        in out
    )


def test_build_entry_links_recording_only():
    out = build_entry(
        date="2026-06-11",
        title="W",
        speakers=["A"],
        summary="x",
        decisions=[],
        action_items=[],
        recording_url="https://fathom.video/abc",
    )
    assert "[Fathom recording](https://fathom.video/abc)" in out
    assert "[transcript]" not in out


def test_build_entry_no_links_line_when_neither():
    out = build_entry(
        date="2026-06-11",
        title="W",
        speakers=["A"],
        summary="x",
        decisions=[],
        action_items=[],
    )
    assert "[Fathom recording]" not in out
    assert "[transcript]" not in out


def test_build_entry_ends_with_meeting_marker():
    out = build_entry(
        date="2026-06-11",
        title="W",
        speakers=["A"],
        summary="x",
        decisions=[],
        action_items=[],
        meeting_id="m-42",
    )
    assert out.rstrip().endswith("<!-- cp:meeting=m-42 -->")


def test_build_entry_no_marker_without_meeting_id():
    out = build_entry(
        date="2026-06-11",
        title="W",
        speakers=["A"],
        summary="x",
        decisions=[],
        action_items=[],
    )
    assert "<!-- cp:meeting=" not in out


# --- append_entry --------------------------------------------------------


def _entry(meeting_id: str, title: str = "W") -> str:
    return build_entry(
        date="2026-06-11",
        title=title,
        speakers=["A"],
        summary="some summary",
        decisions=[],
        action_items=[],
        meeting_id=meeting_id,
    )


def test_append_entry_creates_parseable_element(tmp_path):
    hp = tmp_path / "spine" / "Retrospective" / "meeting-history.md"
    wrote = append_entry(
        hp,
        "m-1",
        _entry("m-1"),
        code="ibx-5153",
        project="ibx-5153",
        today=date(2026, 6, 11),
    )
    assert wrote is True
    assert hp.exists()
    el = parse_element(hp)
    assert el.layer == "Retrospective"
    assert el.type == "retrospective"
    assert el.id.endswith("/retrospective/meeting-history")
    assert el.project == "ibx-5153"


def test_append_entry_idempotent_on_meeting_id(tmp_path):
    hp = tmp_path / "spine" / "Retrospective" / "meeting-history.md"
    append_entry(
        hp, "m-1", _entry("m-1"), code="ibx-5153", project="ibx-5153",
        today=date(2026, 6, 11),
    )
    second = append_entry(
        hp, "m-1", _entry("m-1"), code="ibx-5153", project="ibx-5153",
        today=date(2026, 6, 12),
    )
    assert second is False
    text = hp.read_text()
    assert text.count("<!-- cp:meeting=m-1 -->") == 1


def test_append_entry_different_meeting_appends(tmp_path):
    hp = tmp_path / "spine" / "Retrospective" / "meeting-history.md"
    append_entry(
        hp, "m-1", _entry("m-1", "First"), code="ibx-5153", project="ibx-5153",
        today=date(2026, 6, 11),
    )
    wrote = append_entry(
        hp, "m-2", _entry("m-2", "Second"), code="ibx-5153", project="ibx-5153",
        today=date(2026, 6, 12),
    )
    assert wrote is True
    text = hp.read_text()
    assert "<!-- cp:meeting=m-1 -->" in text
    assert "<!-- cp:meeting=m-2 -->" in text
    # last_touched bumped to the newer call's today
    el = parse_element(hp)
    assert el.last_touched == "2026-06-12"
    # newest at top: Second's header precedes First's
    assert text.index("Second") < text.index("First")
