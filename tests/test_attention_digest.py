"""Tests for src/cp_engine/attention_digest.py (Lever 2)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest


def test_find_past_due_asks_finds_by_date_overdue(tmp_path):
    """An ask with `by <date>` < today is past due."""
    from cp_engine.attention_digest import _find_past_due_asks
    sprint = tmp_path / "ggl-5168.md"
    sprint.write_text(
        "## Client communication\n\n### Open asks\n\n"
        "- [open · 2026-05-16 · Tony · by 2026-05-20] Email Joe re GRC <!-- cp:hash=aaaaaaaa -->\n"
    )
    found = _find_past_due_asks(
        sprint_files=[sprint], today=date(2026, 5, 27), no_by_threshold_days=7
    )
    assert len(found) == 1
    f = found[0]
    assert f.code == "ggl-5168"
    assert f.text == "Email Joe re GRC"
    assert f.who == "Tony"
    assert f.by == date(2026, 5, 20)
    assert f.days_past == 7  # today - by
    assert f.hash == "aaaaaaaa"


def test_find_past_due_asks_finds_stale_no_by_asks(tmp_path):
    """An ask with NO `by` date that's been open >= threshold days is stale."""
    from cp_engine.attention_digest import _find_past_due_asks
    sprint = tmp_path / "tel-5113.md"
    sprint.write_text(
        "### Open asks\n\n"
        "- [open · 2026-05-15 · Drew] No-by ask, 12 days old <!-- cp:hash=bbbbbbbb -->\n"
        "- [open · 2026-05-21 · Drew] No-by ask, 6 days old <!-- cp:hash=cccccccc -->\n"
    )
    found = _find_past_due_asks(
        sprint_files=[sprint], today=date(2026, 5, 27), no_by_threshold_days=7
    )
    texts = [f.text for f in found]
    assert "No-by ask, 12 days old" in texts
    assert "No-by ask, 6 days old" not in texts


def test_find_past_due_asks_ignores_closed_asks(tmp_path):
    """A `[closed · ...]` bullet must never appear in results, even if past due."""
    from cp_engine.attention_digest import _find_past_due_asks
    sprint = tmp_path / "ibx-5153.md"
    sprint.write_text(
        "### Open asks\n\n"
        "- [closed · 2026-05-10 · Drew · by 2026-05-15] Already done <!-- cp:hash=dddddddd -->\n"
        "- [open · 2026-05-10 · Drew · by 2026-05-15] Still pending <!-- cp:hash=eeeeeeee -->\n"
    )
    found = _find_past_due_asks(
        sprint_files=[sprint], today=date(2026, 5, 27), no_by_threshold_days=7
    )
    texts = [f.text for f in found]
    assert "Already done" not in texts
    assert "Still pending" in texts


def test_find_past_due_asks_handles_who_with_parens_and_commas(tmp_path):
    """`who` can contain parens and commas (real production shape)."""
    from cp_engine.attention_digest import _find_past_due_asks
    sprint = tmp_path / "ggl-5168.md"
    sprint.write_text(
        "### Open asks\n\n"
        "- [open · 2026-05-15 · Google EHS team (Sav, Jim Logan, et al.) · by 2026-05-20] Provide feedback <!-- cp:hash=ffffffff -->\n"
    )
    found = _find_past_due_asks(
        sprint_files=[sprint], today=date(2026, 5, 27), no_by_threshold_days=7
    )
    assert len(found) == 1
    assert found[0].who == "Google EHS team (Sav, Jim Logan, et al.)"
    assert found[0].text == "Provide feedback"


def test_find_past_due_asks_no_results_when_all_fresh(tmp_path):
    """Empty list when nothing's past due."""
    from cp_engine.attention_digest import _find_past_due_asks
    sprint = tmp_path / "p.md"
    sprint.write_text(
        "### Open asks\n\n"
        "- [open · 2026-05-26 · Drew · by 2026-05-30] Fresh <!-- cp:hash=11111111 -->\n"
        "- [open · 2026-05-26 · Tony] No-by, only 1 day old <!-- cp:hash=22222222 -->\n"
    )
    found = _find_past_due_asks(
        sprint_files=[sprint], today=date(2026, 5, 27), no_by_threshold_days=7
    )
    assert found == []


def test_find_past_due_asks_extracts_code_from_filename(tmp_path):
    """The `code` field is taken from the sprint file's stem (e.g., 'ggl-5168.md' → 'ggl-5168')."""
    from cp_engine.attention_digest import _find_past_due_asks
    sprint = tmp_path / "mission-control.md"  # initiative slug
    sprint.write_text(
        "### Open asks\n\n"
        "- [open · 2026-05-10 · Drew · by 2026-05-15] something <!-- cp:hash=12345678 -->\n"
    )
    found = _find_past_due_asks(
        sprint_files=[sprint], today=date(2026, 5, 27), no_by_threshold_days=7
    )
    assert len(found) == 1
    assert found[0].code == "mission-control"
