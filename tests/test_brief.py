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


# ──────────────────────────────────────────────────────────────────────
#  Canon gist — the substance, not just the title
#
#  Motivating failure (2026-08-28): an ECD deck re-architected the ibx-5153
#  campaign under a substituted vocabulary. Every canon TITLE still looked
#  satisfied, so nothing caught it; the divergence lived in the bodies.
#  Real bodies run 16→3,761 words and the ratified vocabulary sits at word
#  ~170 in the largest, so a short head-truncation would cut off the very
#  thing the check exists to surface.
# ──────────────────────────────────────────────────────────────────────


def test_gist_takes_the_ruling_line_from_a_decision_card():
    from cp_engine.brief import canon_gist

    body = (
        '**Ruling: the pillar spec ALIGNS to the FOUR moves — solid · '
        'intelligent · preemptive · future-proof — the client\'s own '
        'language.** **Date / who:** 2026-07-16.'
    )
    out = canon_gist(body)
    assert "solid · intelligent · preemptive · future-proof" in out
    # The trailing metadata is not the ruling.
    assert "Date / who" not in out


def test_gist_always_surfaces_an_unsettled_question():
    """A decision flagged open is what a later document closes by omission."""
    from cp_engine.brief import canon_gist

    body = (
        "**Ruling: the spec aligns to the four moves.** "
        "**Context:** " + "filler " * 200 +
        "**What is NOT settled:** whether the rewrite runs FOUR distinct legs "
        "or folds preemptive into acts — open question #1."
    )
    out = canon_gist(body)
    assert "NOT settled" in out
    assert "FOUR distinct legs" in out
    # It survives even though the clause sits far past the word budget.
    assert "filler filler" not in out.split("NOT settled")[1]


def test_gist_falls_back_to_prose_for_a_document_card():
    from cp_engine.brief import canon_gist

    body = "# Key stakeholders\n## Roster\nEmails harvested from the comments."
    out = canon_gist(body)
    assert out.startswith("Emails harvested")
    assert "#" not in out


def test_gist_drops_table_markup():
    from cp_engine.brief import canon_gist

    body = "# Roster\nThe contact list.\n| Name | Email |\n|---|---|\n| J | j@x |"
    assert canon_gist(body) == "The contact list."


def test_gist_is_empty_for_an_empty_body():
    """An empty body renders the title alone, never a dangling bullet."""
    from cp_engine.brief import canon_gist

    assert canon_gist(None) == ""
    assert canon_gist("   ") == ""


def test_canon_section_renders_title_and_gist():
    from cp_engine.brief import canon_section

    out = canon_section(
        [{"est_item_id": "_authored/x", "framing": "Pillar ruling",
          "body": "**Ruling: FOUR pillars — AI · CNS · Preemptive.**"}],
        None,
    )
    assert "**Pillar ruling**" in out
    assert "FOUR pillars — AI · CNS · Preemptive." in out


def test_canon_section_without_a_body_still_renders_the_title():
    """Pre-body callers and empty elements must not regress to a blank line."""
    from cp_engine.brief import canon_section

    out = canon_section(
        [{"est_item_id": "_authored/x", "framing": "Some canon"}], None
    )
    assert out.strip() == "- **Some canon** (`_authored/x`)"
