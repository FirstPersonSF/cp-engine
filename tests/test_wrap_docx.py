"""Tests for the Word rendering of a wrap report (#184).

Drew: *"We need to make certain documents human readable, so having a wrap
report as an md doc isn't correct."*

These assert against the OOXML the file actually contains, not against the
in-memory objects — a .docx that python-docx is happy with but Word renders
empty would pass a mock-based test and fail the only use it has.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from cp_engine.wrap_docx import (
    BLANK,
    WrapSection,
    build_wrap_docx,
    effort_table,
    facts_table,
)

_PAYLOAD = {
    "code": "ibx-5192",
    "name": "Platform Sales Readiness Summit",
    "status": "Open",
    "account_manager": "Drew Fiero",
    "start_date": "2026-06-22",
    "duration_weeks": 7.4,
    "budget": 38000.0,
    "target_profit_pct": 70.0,
    "budget_per_hour": 116.39,
    "effort": {
        "verified": True,
        "total_hours": 326.5,
        "weeks": 9,
        "by_person": [
            {"name": "Geoff Ahmann", "hours": 136.0},
            {"name": "Drew Fiero", "hours": 85.5},
        ],
    },
    "meetings": {
        "count": 39, "total_hours": 30.6, "tail_share": 0.67, "tail_days": 14,
    },
}


def _text(path: Path) -> str:
    """The document body's raw XML — what Word will actually read."""
    with zipfile.ZipFile(path) as zf:
        return zf.read("word/document.xml").decode("utf-8")


def test_writes_a_real_openable_docx(tmp_path: Path) -> None:
    out = build_wrap_docx(
        title="Wrap Report",
        subtitle="ibx-5192 · 2026-08-14",
        sections=[WrapSection("Project facts", table=facts_table(_PAYLOAD))],
        out_path=tmp_path / "w.docx",
    )
    assert out.exists() and out.stat().st_size > 0
    # A .docx is a zip with a specific member — not merely a non-empty file.
    with zipfile.ZipFile(out) as zf:
        assert "word/document.xml" in zf.namelist()


def test_title_and_subtitle_reach_the_document(tmp_path: Path) -> None:
    out = build_wrap_docx(
        title="Wrap Report",
        subtitle="ibx-5192 · 2026-08-14",
        sections=[],
        out_path=tmp_path / "w.docx",
    )
    body = _text(out)
    assert "Wrap Report" in body
    assert "2026-08-14" in body


def test_facts_render_as_a_table_not_prose(tmp_path: Path) -> None:
    """The readability point: facts belong in a table a human can scan."""
    out = build_wrap_docx(
        title="t", subtitle="",
        sections=[WrapSection("Facts", table=facts_table(_PAYLOAD))],
        out_path=tmp_path / "w.docx",
    )
    body = _text(out)
    assert "<w:tbl>" in body, "facts must be a real Word table"
    assert "$38,000" in body
    assert "326.5" in body


def test_unread_hours_render_loudly_not_as_a_blank(tmp_path: Path) -> None:
    """A silent blank here is what produced a retro claiming 'hours not
    captured' while sprint_allocations held 326.5 of them."""
    payload = dict(_PAYLOAD, effort={"verified": False})
    rows = facts_table(payload)
    flat = " ".join(" ".join(r) for r in rows)
    assert "NOT READ" in flat
    assert "do not state a margin" in flat


def test_missing_money_says_not_recorded_never_zero(tmp_path: Path) -> None:
    """A plausible-looking $0 becomes a talking point. A blank does not."""
    rows = facts_table({"code": "x", "effort": {}, "meetings": {}})
    flat = " ".join(" ".join(r) for r in rows)
    assert "not recorded" in flat
    assert "$0" not in flat


def test_human_entry_fields_render_as_visible_blanks(tmp_path: Path) -> None:
    """The load-bearing idea ported from social-builder: an unfilled field
    must LOOK unfilled. Omitting it is how it never gets filled."""
    out = build_wrap_docx(
        title="t", subtitle="",
        sections=[WrapSection(
            "Not assessed",
            blanks=["Project rating (1-5)", "OK to post publicly"],
        )],
        out_path=tmp_path / "w.docx",
    )
    body = _text(out)
    assert "Project rating (1-5)" in body
    assert BLANK in body


def test_bullet_blocks_become_real_bullets(tmp_path: Path) -> None:
    out = build_wrap_docx(
        title="t", subtitle="",
        sections=[WrapSection("Learnings", body="Intro.\n\n- one\n- two")],
        out_path=tmp_path / "w.docx",
    )
    body = _text(out)
    # OOXML stores the style id, not python-docx's friendly name: the
    # "List Bullet" style lands as pStyle val="ListBullet".
    assert 'w:val="ListBullet"' in body, (
        "a '- ' block must render as real bullets, not a paragraph that "
        "merely starts with a hyphen"
    )
    assert "Intro." in body


def test_effort_table_is_empty_when_unverified() -> None:
    """Caller skips the section rather than rendering a table of zeros."""
    assert effort_table({"effort": {"verified": False, "by_person": []}}) == []
    assert effort_table({"effort": {"verified": True, "by_person": []}}) == []


def test_effort_table_lists_people_when_read() -> None:
    rows = effort_table(_PAYLOAD)
    assert rows[0] == ["Person", "Hours (allocated)"]
    assert ["Geoff Ahmann", "136.0"] in rows


def test_hours_are_labelled_allocated_everywhere_they_appear() -> None:
    """Honesty constraint: these are MC-2 planning rows, not timesheets.
    Presenting an allocation as an actual overstates precision on the one
    number a scope conversation turns on."""
    facts = " ".join(" ".join(r) for r in facts_table(_PAYLOAD))
    assert "allocated" in facts.lower()
    effort = " ".join(" ".join(r) for r in effort_table(_PAYLOAD))
    assert "allocated" in effort.lower()


def test_empty_sections_do_not_crash(tmp_path: Path) -> None:
    out = build_wrap_docx(
        title="t", subtitle="", sections=[WrapSection("Heading only")],
        out_path=tmp_path / "w.docx",
    )
    assert out.exists()


def test_creates_missing_parent_directories(tmp_path: Path) -> None:
    out = build_wrap_docx(
        title="t", subtitle="", sections=[],
        out_path=tmp_path / "deep" / "nested" / "w.docx",
    )
    assert out.exists()
