# tests/test_brief.py — pure composition helpers behind `cp brief`
from cp_engine.brief import (
    _trim_bullets,
    briefing_section,
    commitments_section,
    compose_brief,
    exec_summary_section,
    facts_section,
    last_session_section,
    newest_session_capture,
)


def test_trim_bullets_keeps_sub_lines_with_their_parent():
    body = (
        "- one\n"
        "  - one.a\n"
        "- two\n"
        "- three\n"
        "  continuation of three\n"
        "- four\n"
    )
    trimmed, dropped = _trim_bullets(body, 3)
    assert dropped == 1
    assert "continuation of three" in trimmed
    assert "one.a" in trimmed
    assert "- four" not in trimmed


def test_trim_bullets_under_budget_is_untouched():
    body = "- a\n- b"
    trimmed, dropped = _trim_bullets(body, 5)
    assert trimmed == body
    assert dropped == 0


def test_facts_section_absent_cp_md():
    assert "No working dir" in facts_section(None)


def test_exec_summary_section_placeholders_are_unauthored():
    region = (
        "<!-- cp-engine:start exec-summary -->\n"
        "## Exec Summary\n\n"
        "**Status:** _<current state>_\n"
        "<!-- cp-engine:end exec-summary -->\n"
    )
    assert "scaffold only" in exec_summary_section(region)


def test_briefing_section_prefers_body_over_note():
    assert briefing_section("The brief.", None) == "The brief."
    assert briefing_section(None, "No spine.") == "_No spine._"


def test_commitments_section_none_vs_empty():
    assert commitments_section(None, "store gone") == "_store gone_"
    assert commitments_section([], None) == "_No open commitments._"


def test_last_session_section_absent_everything():
    assert "No Last-session line" in last_session_section(None, None)


def test_newest_session_capture_lexicographic(tmp_path):
    sess = tmp_path / "sessions"
    sess.mkdir()
    (sess / "2026-07-20-0900-drew.md").write_text("x", encoding="utf-8")
    (sess / "2026-07-23-1400-tony.md").write_text("x", encoding="utf-8")
    assert newest_session_capture(tmp_path) == "2026-07-23-1400-tony.md"
    assert newest_session_capture(None) is None
    assert newest_session_capture(tmp_path / "nope") is None


def test_compose_brief_is_pure_and_ordered():
    out = compose_brief("x-1", None, None, None, None, None, None)
    idx = [out.index(h) for h in (
        "# Brief — x-1", "## Facts", "## Exec Summary (trimmed)",
        "## Inputs & Briefing", "## Open commitments", "## Last session",
    )]
    assert idx == sorted(idx)
    assert out == compose_brief("x-1", None, None, None, None, None, None)
