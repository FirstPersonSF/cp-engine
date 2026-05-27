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


def test_find_escalated_risks_finds_recent_escalation_format_a(tmp_path):
    """Engine-written shape: `- [escalated · category · date] text <!-- cp:hash=... -->`."""
    from cp_engine.attention_digest import _find_escalated_risks
    sprint = tmp_path / "ibx-5167.md"
    sprint.write_text(
        "## Dependencies & risks\n\n"
        "- [escalated · capacity · 2026-05-25] Tony bandwidth conflict <!-- cp:hash=88b22d40 -->\n"
    )
    found = _find_escalated_risks(sprint_files=[sprint], today=date(2026, 5, 27), window_days=7)
    assert len(found) == 1
    r = found[0]
    assert r.code == "ibx-5167"
    assert r.text == "Tony bandwidth conflict"
    assert r.category == "capacity"
    assert r.raised == date(2026, 5, 25)
    assert r.hash == "88b22d40"


def test_find_escalated_risks_finds_recent_escalation_format_b(tmp_path):
    """Human-written shape: `- [risk · escalated · category · date] text <!-- cp:hash=... -->`."""
    from cp_engine.attention_digest import _find_escalated_risks
    sprint = tmp_path / "ibx-5167.md"
    sprint.write_text(
        "## Dependencies & risks\n\n"
        "- [risk · escalated · scope · 2026-05-25] Script churn blocking <!-- cp:hash=c12a6d46 -->\n"
    )
    found = _find_escalated_risks(sprint_files=[sprint], today=date(2026, 5, 27), window_days=7)
    assert len(found) == 1
    assert found[0].text == "Script churn blocking"
    assert found[0].category == "scope"


def test_find_escalated_risks_ignores_non_escalated_severities(tmp_path):
    """severity=watching is NOT escalated, even if recent."""
    from cp_engine.attention_digest import _find_escalated_risks
    sprint = tmp_path / "p.md"
    sprint.write_text(
        "## Dependencies & risks\n\n"
        "- [watching · scope · 2026-05-26] Minor concern <!-- cp:hash=22222222 -->\n"
        "- [risk · watching · schedule · 2026-05-26] Another minor <!-- cp:hash=33333333 -->\n"
    )
    found = _find_escalated_risks(sprint_files=[sprint], today=date(2026, 5, 27), window_days=7)
    assert found == []


def test_find_escalated_risks_ignores_old_escalations(tmp_path):
    """Escalations older than window_days are excluded."""
    from cp_engine.attention_digest import _find_escalated_risks
    sprint = tmp_path / "p.md"
    sprint.write_text(
        "## Dependencies & risks\n\n"
        "- [escalated · vendor · 2026-04-15] Old escalation <!-- cp:hash=11111111 -->\n"  # >40 days ago
        "- [escalated · scope · 2026-05-26] Recent escalation <!-- cp:hash=22222222 -->\n"
    )
    found = _find_escalated_risks(sprint_files=[sprint], today=date(2026, 5, 27), window_days=7)
    texts = [r.text for r in found]
    assert "Old escalation" not in texts
    assert "Recent escalation" in texts


def test_find_escalated_risks_window_includes_today(tmp_path):
    """A risk raised today must appear."""
    from cp_engine.attention_digest import _find_escalated_risks
    sprint = tmp_path / "p.md"
    sprint.write_text(
        "## Dependencies & risks\n\n"
        "- [escalated · scope · 2026-05-27] Fresh today <!-- cp:hash=12345678 -->\n"
    )
    found = _find_escalated_risks(sprint_files=[sprint], today=date(2026, 5, 27), window_days=7)
    assert len(found) == 1
    assert found[0].text == "Fresh today"


def test_find_escalated_risks_window_boundary_exact(tmp_path):
    """Boundary: raised == today - window_days inclusive."""
    from cp_engine.attention_digest import _find_escalated_risks
    sprint = tmp_path / "p.md"
    sprint.write_text(
        "## Dependencies & risks\n\n"
        "- [escalated · scope · 2026-05-20] Exactly 7d ago <!-- cp:hash=44444444 -->\n"  # today=5/27, 5/20 is 7d ago
        "- [escalated · scope · 2026-05-19] 8d ago, outside window <!-- cp:hash=55555555 -->\n"
    )
    found = _find_escalated_risks(sprint_files=[sprint], today=date(2026, 5, 27), window_days=7)
    texts = [r.text for r in found]
    assert "Exactly 7d ago" in texts
    assert "8d ago, outside window" not in texts
