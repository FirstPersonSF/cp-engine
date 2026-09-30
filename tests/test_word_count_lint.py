"""Whole-file word-count discipline lint (#204).

The rule was documented in `CLAUDE.md` as engine-enforced for months while
nothing measured it. These tests pin the thresholds, the exemptions, and the
warn-only contract so it can't silently regress to a no-op again.
"""

from __future__ import annotations

from pathlib import Path

from cp_engine.word_count_lint import (
    AUDIT_THRESHOLD_WORDS,
    ROTATE_THRESHOLD_WORDS,
    is_exempt,
    lint_word_count,
    word_count_warnings,
)


def _doc(words: int) -> str:
    return "word " * words


def test_under_budget_is_silent():
    assert lint_word_count(_doc(100), "p") == []
    assert lint_word_count("", "p") == []


def test_threshold_is_strictly_greater_than():
    # exactly at the line is fine; one word over trips it
    assert lint_word_count(_doc(AUDIT_THRESHOLD_WORDS), "p") == []
    assert len(lint_word_count(_doc(AUDIT_THRESHOLD_WORDS + 1), "p")) == 1


def test_audit_threshold_names_the_audit():
    (warning,) = lint_word_count(_doc(AUDIT_THRESHOLD_WORDS + 1), "proj")
    assert warning.startswith("proj: ")
    assert "duplication audit" in warning
    assert "2,500" in warning


def test_rotate_threshold_supersedes_audit():
    """Over 3,500 reports rotation only — not both warnings at once."""
    warnings = lint_word_count(_doc(ROTATE_THRESHOLD_WORDS + 1), "proj")
    assert len(warnings) == 1
    assert "archive rotation" in warnings[0]
    assert "duplication audit" not in warnings[0]


def test_markdown_emphasis_does_not_inflate_the_count():
    """Mirrors exec_summary_lint._word_count so both measure alike."""
    plain = "one two three"
    styled = "**one** __two__ `three`"
    assert lint_word_count(plain, "p") == lint_word_count(styled, "p")


def test_meetings_and_ledgers_are_exempt():
    # named in CLAUDE.md
    assert is_exempt(Path("1p/co/proj/meetings/2026-01-01.md"))
    # append-only meeting ledger — same shape, different name
    assert is_exempt(Path("1p/co/proj/spine/Retrospective/meeting-history.md"))
    # closed work isn't audited
    assert is_exempt(Path("1p/co/inactive/old/cp.md"))
    # a live CP is NOT exempt
    assert not is_exempt(Path("1p/co/proj/cp.md"))


def test_scan_skips_exempt_and_top_level_files(tmp_path: Path):
    over = _doc(AUDIT_THRESHOLD_WORDS + 50)

    (tmp_path / "cp.md").write_text(over)  # tenant top-level: skipped
    proj = tmp_path / "1p" / "co" / "live"
    proj.mkdir(parents=True)
    (proj / "cp.md").write_text(over)
    meetings = tmp_path / "1p" / "co" / "meetings"
    meetings.mkdir(parents=True)
    (meetings / "cp.md").write_text(over)  # exempt

    warnings = word_count_warnings(tmp_path)
    assert len(warnings) == 1
    assert warnings[0].startswith("1p/co/live: ")


def test_scan_orders_worst_first(tmp_path: Path):
    for name, n in (("small", 2_600), ("huge", 4_000), ("mid", 3_000)):
        d = tmp_path / "1p" / "co" / name
        d.mkdir(parents=True)
        (d / "cp.md").write_text(_doc(n))

    warnings = word_count_warnings(tmp_path)
    assert [w.split(":")[0].split("/")[-1] for w in warnings] == [
        "huge",
        "mid",
        "small",
    ]


def test_scan_is_pure_read(tmp_path: Path):
    proj = tmp_path / "1p" / "co" / "live"
    proj.mkdir(parents=True)
    target = proj / "cp.md"
    body = _doc(AUDIT_THRESHOLD_WORDS + 50)
    target.write_text(body)

    word_count_warnings(tmp_path)
    assert target.read_text() == body


# ──────────────────────────────────────────────────────────────────────
#  Contributor breakdown
#
#  Motivating failure: three CP files crossed the threshold in three days
#  (ibx-5153 08-26, slt-5196 08-27, mission-control 08-27). Three different
#  guesses were made about the cause before anyone measured; one was written
#  into a CP file as fact ("the engine-managed strips") and measurement
#  disproved it outright. The warning named a problem and left the diagnosis
#  to a human.
# ──────────────────────────────────────────────────────────────────────


def _cp(exec_body: str = "", strip_body: str = "", hand: str = "") -> str:
    return (
        "<!-- cp-engine:start project-facts -->\n"
        f"{strip_body}\n"
        "<!-- cp-engine:end project-facts -->\n"
        "<!-- cp-engine:start exec-summary -->\n"
        f"{exec_body}\n"
        "<!-- cp-engine:end exec-summary -->\n"
        f"{hand}\n"
    )


def test_contributors_split_the_three_buckets():
    from cp_engine.word_count_lint import contributors

    out = "\n".join(contributors(_cp(
        exec_body="**Status:** " + "e " * 100,
        strip_body="s " * 50,
        hand="h " * 30,
    )))
    assert "exec-summary" in out
    assert "engine strips" in out
    assert "hand-written" in out


def test_the_exec_summary_strip_is_not_counted_as_an_engine_strip():
    """exec-summary is model-authored; lumping it with sync's strips would
    point the reader at the wrong owner."""
    from cp_engine.word_count_lint import contributors

    lines = contributors(_cp(exec_body="**Status:** " + "w " * 200))
    exec_line = next(ln for ln in lines if "exec-summary " in ln)
    strip_line = next(ln for ln in lines if "engine strips" in ln)
    # The Exec Summary owns its ~201 words (the label counts too); the
    # engine-strip bucket must not also claim them.
    assert "201" in exec_line
    assert "0 (0%)" in strip_line


def test_the_worst_hand_written_section_is_broken_down_by_entry():
    """Since #308 the threshold measures hand-written text, so the breakdown
    points into hand-written sections — that is where a trim can happen."""
    from cp_engine.word_count_lint import contributors

    done = (
        "## Done\n"
        "- 2026-08-20 — " + "big " * 300 + "\n"
        "- 2026-08-13 — " + "small " * 20 + "\n"
        "## Project Notes\n" + "note " * 40 + "\n"
    )
    out = "\n".join(contributors(_cp(hand=done)))
    assert "hand-written sections: Done" in out
    assert "Done by entry" in out
    assert "2026-08-20" in out


def test_an_entry_is_measured_with_its_sub_bullets():
    """The 08-20 Update was 395 words alone and 912 with its sub-detail —
    and 912 is the number that made the file overrun."""
    from cp_engine.word_count_lint import _split_entries, _word_count

    body = (
        "- 2026-08-20 — headline\n"
        "  - " + "detail " * 100 + "\n"
        "- 2026-08-13 — other\n"
    )
    entries = _split_entries(body)
    assert len(entries) == 2
    assert _word_count(entries[0]) > 100


def test_entry_label_survives_a_parenthesised_rollup():
    """Rolled-up entries are written "(2026-08-25 — …)" in real CPs."""
    from cp_engine.word_count_lint import _entry_label

    assert _entry_label("- (2026-08-25 — the spine pass)") == "2026-08-25"
    assert _entry_label("- 2026-08-28 — x") == "2026-08-28"


def test_a_malformed_file_still_gets_its_threshold_warning():
    """Diagnosis is a bonus; the warning is not. An unparseable region must
    never suppress the line that says the file is over budget."""
    from cp_engine.word_count_lint import lint_word_count

    text = "<!-- cp-engine:start exec-summary -->\n" + "w " * 3000
    out = lint_word_count(text, "proj")
    assert out
    assert "⚠ word-count" in out[0]


def test_under_budget_files_stay_silent():
    from cp_engine.word_count_lint import lint_word_count

    assert lint_word_count("w " * 100, "proj") == []


# ──────────────────────────────────────────────────────────────────────
#  #308 — only hand-authored text is measured
#
#  slt-5196 (09-29): 5,398 words, of which inbound 1,527 · decisions 1,030 ·
#  open-asks 949 · current-sprint 295 · exec-summary 1,013 · hand ≈350. The
#  "roll resolved threads into the archive file" warning fired on a file where
#  deleting every hand-written word would not have cleared it.
# ──────────────────────────────────────────────────────────────────────


def _strip(name: str, words: int) -> str:
    return (f"<!-- cp-engine:start {name} -->\n" + "s " * words
            + f"\n<!-- cp-engine:end {name} -->\n")


def test_huge_strips_with_little_hand_text_do_not_warn():
    text = (_strip("inbound-strip", 1_527) + _strip("recent-decisions-strip", 1_030)
            + _strip("open-asks-strip", 949) + _strip("current-sprint", 295)
            + _strip("exec-summary", 1_013) + "## Current Work\n" + "h " * 350)
    assert lint_word_count(text, "slt-5196") == []


def test_big_hand_written_text_still_warns_and_says_what_it_counted():
    text = _strip("inbound-strip", 2_000) + "## Current Work\n" + "h " * 3_600
    (warning,) = lint_word_count(text, "p")
    head = warning.splitlines()[0]
    assert "archive rotation" in head
    # The measured number is the hand-written one; the whole file is context.
    assert "3,603 words hand-written" in head
    # 2,000 strip words + 8 marker tokens + 3,603 hand words.
    assert "5,611 with engine regions" in head


def test_the_exec_summary_region_is_not_counted():
    """It sits inside a cp-engine marker, and the marker is the rule. Its
    fields carry their own budgets in `exec_summary_lint`."""
    text = _strip("exec-summary", AUDIT_THRESHOLD_WORDS + 500) + "h " * 10
    assert lint_word_count(text, "p") == []


def test_every_named_region_is_excluded_not_just_known_ones():
    """Region names are derived per node (#303); the exclusion follows the
    marker grammar, not a list someone has to keep current."""
    from cp_engine.word_count_lint import hand_authored_words

    text = _strip("some-future-strip", 5_000) + "h " * 7
    assert hand_authored_words(text) == 7


def test_scan_orders_by_hand_written_words(tmp_path: Path):
    """Worst-first must mean worst by the measured number, or a strip-heavy
    file under budget would be sorted above one that is actually over."""
    for name, strips, hand in (("stripheavy", 9_000, 2_600), ("handheavy", 0, 3_000)):
        d = tmp_path / "1p" / "co" / name
        d.mkdir(parents=True)
        (d / "cp.md").write_text(_strip("inbound-strip", strips) + "h " * hand)
    warnings = word_count_warnings(tmp_path)
    assert [w.split(":")[0].split("/")[-1] for w in warnings] == [
        "handheavy",
        "stripheavy",
    ]
