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
