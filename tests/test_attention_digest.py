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


# ── compose_digest tests ─────────────────────────────────────

def _make_past_due(*, code="ggl-5168", text="X", who="Tony", asked=date(2026, 5, 16),
                   by=date(2026, 5, 20), days_past=7, hash="aaaaaaaa"):
    from cp_engine.attention_digest import PastDueAsk
    return PastDueAsk(code=code, text=text, who=who, asked=asked, by=by,
                      days_past=days_past, hash=hash)

def _make_escalated(*, code="ibx-5167", text="X", category="scope",
                    raised=date(2026, 5, 25), hash="bbbbbbbb"):
    from cp_engine.attention_digest import EscalatedRisk
    return EscalatedRisk(code=code, text=text, category=category, raised=raised, hash=hash)


def test_compose_digest_renders_past_due_section():
    from cp_engine.attention_digest import compose_digest
    digest = {
        "past_due": [_make_past_due(text="Email Joe", days_past=11, by=date(2026, 5, 16))],
        "escalated": [],
        "allocation": [],
    }
    md = compose_digest(digest, recipient_name="Drew", today=date(2026, 5, 27))
    assert "Drew" in md
    assert "1 ask past due" in md
    assert "Email Joe" in md
    assert "11d past" in md
    assert "5/16" in md  # M/D format


def test_compose_digest_renders_escalated_section():
    from cp_engine.attention_digest import compose_digest
    digest = {
        "past_due": [],
        "escalated": [_make_escalated(text="script churn", category="scope")],
        "allocation": [],
    }
    md = compose_digest(digest, recipient_name="Drew", today=date(2026, 5, 27))
    assert "1 risk escalated" in md
    assert "script churn" in md
    assert "ibx-5167" in md


def test_compose_digest_renders_both_sections_when_both_present():
    from cp_engine.attention_digest import compose_digest
    digest = {
        "past_due": [_make_past_due()],
        "escalated": [_make_escalated()],
        "allocation": [],
    }
    md = compose_digest(digest, recipient_name="Drew", today=date(2026, 5, 27))
    assert "past due" in md
    assert "escalated" in md
    # Past-due section comes BEFORE escalated.
    assert md.index("past due") < md.index("escalated")


def test_compose_digest_all_clear_when_everything_empty():
    from cp_engine.attention_digest import compose_digest
    digest = {"past_due": [], "escalated": [], "allocation": []}
    md = compose_digest(digest, recipient_name="Drew", today=date(2026, 5, 27))
    assert "all clear" in md.lower()
    assert "Drew" in md


def test_compose_digest_sorts_past_due_by_days_past_descending():
    """Most-overdue asks appear first."""
    from cp_engine.attention_digest import compose_digest
    digest = {
        "past_due": [
            _make_past_due(text="newer", days_past=3, hash="11111111"),
            _make_past_due(text="oldest", days_past=15, hash="22222222"),
            _make_past_due(text="middle", days_past=7, hash="33333333"),
        ],
        "escalated": [],
        "allocation": [],
    }
    md = compose_digest(digest, recipient_name="Drew", today=date(2026, 5, 27))
    # Order in output: oldest (15d), middle (7d), newer (3d)
    assert md.index("oldest") < md.index("middle") < md.index("newer")


def test_compose_digest_omits_allocation_section_when_empty():
    """Allocation section isn't rendered when stub returns empty (current default)."""
    from cp_engine.attention_digest import compose_digest
    digest = {
        "past_due": [_make_past_due()],
        "escalated": [],
        "allocation": [],
    }
    md = compose_digest(digest, recipient_name="Drew", today=date(2026, 5, 27))
    assert "allocation" not in md.lower()


# ── attention_digest orchestrator tests ──────────────────────

def test_attention_digest_walks_current_sprint_dir(tmp_path, monkeypatch):
    """Orchestrator finds the current ISO-week sprint dir and runs both classifiers."""
    from cp_engine.attention_digest import attention_digest

    # Build a fake tenant root with a sprint file under the current ISO week.
    from cp_engine.sprints import current_sprint_week_iso
    from datetime import datetime
    week_iso = current_sprint_week_iso(datetime(2026, 5, 27))
    sprint_dir = tmp_path / "sprints" / week_iso
    sprint_dir.mkdir(parents=True)
    (sprint_dir / "ggl-5168.md").write_text(
        "## Client communication\n### Open asks\n\n"
        "- [open · 2026-05-10 · Drew · by 2026-05-15] Overdue ask <!-- cp:hash=aaaaaaaa -->\n"
        "\n## Dependencies & risks\n\n"
        "- [escalated · scope · 2026-05-25] Recent escalation <!-- cp:hash=bbbbbbbb -->\n"
    )

    # Minimal TenantConfig stub with the right `.root`.
    class _Cfg:
        def __init__(self, root): self.root = root
    config = _Cfg(tmp_path)

    digest = attention_digest(config=config, today=date(2026, 5, 27))
    assert len(digest["past_due"]) == 1
    assert digest["past_due"][0].text == "Overdue ask"
    assert len(digest["escalated"]) == 1
    assert digest["escalated"][0].text == "Recent escalation"
    assert digest["allocation"] == []


def test_attention_digest_falls_back_to_previous_week_when_current_missing(tmp_path, caplog):
    """Monday-morning-on-fresh-week scenario: current ISO week dir absent → use previous week."""
    from cp_engine.attention_digest import attention_digest
    from cp_engine.sprints import current_sprint_week_iso
    from datetime import datetime, timedelta as td
    import logging

    today = date(2026, 5, 27)
    current_iso = current_sprint_week_iso(datetime(today.year, today.month, today.day))
    prev_iso = current_sprint_week_iso(datetime(today.year, today.month, today.day) - td(days=7))
    # Do NOT create the current-week dir; only create the previous-week dir.
    prev_dir = tmp_path / "sprints" / prev_iso
    prev_dir.mkdir(parents=True)
    (prev_dir / "ggl-5168.md").write_text(
        "### Open asks\n\n"
        "- [open · 2026-05-10 · Drew · by 2026-05-15] Past-due from last week <!-- cp:hash=aaaaaaaa -->\n"
    )

    class _Cfg:
        def __init__(self, root): self.root = root

    with caplog.at_level(logging.WARNING):
        digest = attention_digest(config=_Cfg(tmp_path), today=today)
    assert len(digest["past_due"]) == 1
    assert digest["past_due"][0].text == "Past-due from last week"
    # And the warning fired.
    assert any("falling back to previous week" in r.message for r in caplog.records)


def test_attention_digest_returns_empty_when_neither_week_exists(tmp_path):
    """Both current AND previous week absent → empty digest (not a crash)."""
    from cp_engine.attention_digest import attention_digest
    class _Cfg:
        def __init__(self, root): self.root = root
    digest = attention_digest(config=_Cfg(tmp_path), today=date(2026, 5, 27))
    assert digest == {"past_due": [], "escalated": [], "allocation": []}


def test_attention_digest_respects_thresholds(tmp_path):
    """Orchestrator passes through past_due_threshold_days and escalated_window_days."""
    from cp_engine.attention_digest import attention_digest
    from cp_engine.sprints import current_sprint_week_iso
    from datetime import datetime
    week_iso = current_sprint_week_iso(datetime(2026, 5, 27))
    sprint_dir = tmp_path / "sprints" / week_iso
    sprint_dir.mkdir(parents=True)
    (sprint_dir / "p.md").write_text(
        "### Open asks\n\n"
        "- [open · 2026-05-23 · Drew] No-by 4d old <!-- cp:hash=11111111 -->\n"  # under 7d threshold
        "\n## Dependencies & risks\n\n"
        "- [escalated · scope · 2026-05-20] 7d-old escalation <!-- cp:hash=22222222 -->\n"
    )
    class _Cfg:
        def __init__(self, root): self.root = root
    # With defaults (7d threshold, 7d window), the no-by is excluded; the escalation is included.
    digest = attention_digest(config=_Cfg(tmp_path), today=date(2026, 5, 27))
    assert len(digest["past_due"]) == 0
    assert len(digest["escalated"]) == 1

    # With tighter past-due threshold (3 days), the no-by ask IS included.
    digest = attention_digest(
        config=_Cfg(tmp_path), today=date(2026, 5, 27),
        past_due_threshold_days=3, escalated_window_days=7,
    )
    assert len(digest["past_due"]) == 1


# ── Task 2.4: --post-to-slack stub ───────────────────────────

def test_post_digest_to_recipients_stub_raises_not_implemented():
    """Task 2.4 stub: Slack posting raises NotImplementedError until Task 2.6 wires it."""
    from cp_engine.attention_digest import _post_digest_to_recipients

    with pytest.raises(NotImplementedError) as excinfo:
        _post_digest_to_recipients(config=object(), digest_markdown="hi")
    assert "Task 2.6" in str(excinfo.value)
