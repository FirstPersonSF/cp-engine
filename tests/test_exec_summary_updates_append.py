"""`Updates` appends where every other field replaces (#281).

WHY THE ASYMMETRY IS THE POINT. `merge_exec_summary_fields` replaces a field
wholesale, and that is right for the other five: a rewrite of `Next up` means
the old list is WRONG. `Updates` is the one field whose old entries are the
thing being kept — the project's narrative — so replace semantics would force
a caller to resend the entire history to add one line, and dropping one would
be silent.

Measured 2026-09-17: a hosted session could refresh all five other fields and
the Updates log did not move, because `capture_project_state` had no argument
for it. The verb that exists to keep the record current could not write to the
field the record lives in.
"""

from __future__ import annotations

from datetime import date

import pytest

from cp_engine.exec_summary_merge import (
    ExecSummaryMergeError,
    append_update_entry,
)

_REGION = """<!-- cp-engine:start exec-summary -->
## Exec Summary  ·  updated 2026-09-01

**Objective:** Ship the thing.
**Status:** In flight.

**Where it stands:**
- One bullet.

**Updates:**
- 2026-09-01 — First entry.
- 2026-08-15 — Older entry.
<!-- cp-engine:end exec-summary -->
"""


def _entries(text: str) -> list[str]:
    body = text[text.index("**Updates:**"):]
    out = []
    for line in body.splitlines()[1:]:
        if line.startswith("- "):
            out.append(line)
        elif line.strip() and not line.startswith("- "):
            break
    return out


def test_an_entry_lands_newest_first() -> None:
    """Reverse-chronological is the file's own convention — a reader opening
    the summary wants the latest state first."""
    out, changed = append_update_entry(_REGION, "Third thing.", today=date(2026, 9, 17))
    assert changed
    entries = _entries(out)
    assert entries[0] == "- 2026-09-17 — Third thing."


def test_the_whole_history_survives() -> None:
    """THE FAILURE THIS PREVENTS. A replace-shaped API drops what you did not
    resend, and the caller does not find out."""
    out, _ = append_update_entry(_REGION, "Third thing.", today=date(2026, 9, 17))
    entries = _entries(out)
    assert len(entries) == 3
    assert "- 2026-09-01 — First entry." in entries
    assert "- 2026-08-15 — Older entry." in entries


def test_the_date_is_stamped_not_accepted() -> None:
    """A caller cannot backdate the record — the date comes from `today`.

    An entry whose date the caller chose would let a scheduled job write
    yesterday's summary as today's, which is the same class of lie the
    `· updated` stamp guard exists to stop.
    """
    out, _ = append_update_entry(_REGION, "2026-01-01 — Backdated.", today=date(2026, 9, 17))
    assert _entries(out)[0].startswith("- 2026-09-17 — ")


def test_resending_an_identical_entry_is_a_noop() -> None:
    """Retry after a timeout must be safe, and a scheduled caller must not be
    able to manufacture freshness by re-posting."""
    once, changed_a = append_update_entry(_REGION, "Third.", today=date(2026, 9, 17))
    twice, changed_b = append_update_entry(once, "Third.", today=date(2026, 9, 17))
    assert changed_a is True
    assert changed_b is False
    assert twice == once


def test_an_empty_entry_is_refused() -> None:
    """An empty append would advance the freshness stamp while saying nothing
    — worse than not writing, because the staleness check reads that stamp."""
    for bad in ("", "   ", "-", "- "):
        with pytest.raises(ExecSummaryMergeError):
            append_update_entry(_REGION, bad, today=date(2026, 9, 17))


def test_a_leading_dash_is_not_doubled() -> None:
    """Callers will paste a bullet. `- - 2026-09-17 —` is not a bullet."""
    out, _ = append_update_entry(_REGION, "- Already bulleted.", today=date(2026, 9, 17))
    assert _entries(out)[0] == "- 2026-09-17 — Already bulleted."


def test_it_does_not_delete_old_entries_to_enforce_roll_off() -> None:
    """THE BOUNDARY. Roll-off moves entries to the sprint file — a write to a
    SECOND file, which needs a checkout the hosted path does not have.

    Doing half of it here (deleting, writing nowhere) would destroy the
    narrative this field exists to keep. Old entries stay; the CLI ritual
    rolls them off.
    """
    aged = _REGION.replace("- 2026-08-15 — Older entry.", "- 2025-01-01 — Ancient.")
    out, _ = append_update_entry(aged, "New.", today=date(2026, 9, 17), roll_off_after_days=1)
    assert "- 2025-01-01 — Ancient." in out


def test_the_stamp_advances_only_on_a_real_append() -> None:
    from cp_engine.render import exec_summary_is_authored  # noqa: F401

    out, _ = append_update_entry(_REGION, "New.", today=date(2026, 9, 17))
    assert "updated 2026-09-17" in out
    again, changed = append_update_entry(out, "New.", today=date(2026, 9, 18))
    assert changed is False
    assert "updated 2026-09-17" in again, "a no-op must not move the stamp"


# --- #294: roll-off report, whole-block dedupe, byte-identical round-trip ----

_REALISTIC = """# Project

<!-- cp-engine:start exec-summary -->
## Exec Summary  ·  updated 2026-09-01

**Status:** x.

**Updates:**
- 2026-09-01 — Entry one.
  - nested sub-bullet under one
- 2026-08-30 — Entry two
  continuation line of entry two
- 2026-08-29 (second) — Entry three.

  **Also:** an indented paragraph with field shape.

- 2026-08-28 — Entry four.
<!-- a comment inside updates -->
- (2026-07-20 Entry five, compacted form)
- (Older updates — 2026-07-01 (rolled))

- 2026-06-26 — Entry six after blank.

<!-- cp-engine:end exec-summary -->

## Current Work
"""


def _without_probe(out: str, probe_line: str, old_stamp: str = "2026-09-01") -> str:
    """Strip the inserted line and rewind the stamp, so what remains can be
    compared byte-for-byte with the input."""
    assert out.count(probe_line + "\n") == 1
    out = out.replace(probe_line + "\n", "", 1)
    return out.replace("updated 2026-09-17", f"updated {old_stamp}", 1)


def test_round_trip_is_byte_identical_except_for_the_inserted_line() -> None:
    """THE ROUND-TRIP CONTRACT (#294). A nested `  - sub` bullet came back at
    top level and the blank line before the end marker vanished — no entry
    was lost, but a verb that rewrites lines it did not touch is one the
    next reader cannot trust. The new line is spliced in; everything else
    is left exactly where it was."""
    out, changed = append_update_entry(_REALISTIC, "Probe.", today=date(2026, 9, 17))
    assert changed is True
    assert "**Updates:**\n- 2026-09-17 — Probe.\n- 2026-09-01 — Entry one.\n" in out
    assert _without_probe(out, "- 2026-09-17 — Probe.") == _REALISTIC


def test_a_nested_bullet_keeps_its_indent() -> None:
    out, _ = append_update_entry(_REALISTIC, "Probe.", today=date(2026, 9, 17))
    assert "\n  - nested sub-bullet under one\n" in out
    assert "\n- nested sub-bullet under one\n" not in out


def test_the_blank_before_the_end_marker_survives() -> None:
    out, _ = append_update_entry(_REALISTIC, "Probe.", today=date(2026, 9, 17))
    assert "- 2026-06-26 — Entry six after blank.\n\n<!-- cp-engine:end exec-summary -->" in out


@pytest.mark.parametrize(
    "resend",
    [
        "Entry one.",  # first line of an entry that has a nested bullet
        "Entry one.\n  - nested sub-bullet under one",  # the whole entry, re-posted
        "Entry two continuation line of entry two",
        "Entry three.",  # an entry with an indented `**Also:**` paragraph
        "Entry four.",  # after an HTML comment
        "Entry six after blank.",  # after a blank line — outside the old window
        "Entry  six   after blank.",  # internal whitespace runs
        "  Entry six after blank.\t",  # leading/trailing whitespace
    ],
)
def test_dedupe_covers_the_whole_block_and_ignores_whitespace(resend: str) -> None:
    """THE WINDOW (#294). Dedupe used to see only the leading contiguous
    bullet run: the first blank, comment, or continuation line ended it, so
    re-posting the last entry of a real file came back `changed=True`. And a
    double space defeated the exact-string compare. Every entry between
    `**Updates:**` and the next authored field is in scope, compared
    whitespace-insensitively."""
    out, changed = append_update_entry(_REALISTIC, resend, today=date(2026, 10, 1))
    assert changed is False
    assert out == _REALISTIC


def test_an_indented_field_shaped_paragraph_is_not_a_boundary() -> None:
    """cp-context-protocol has `  **Also:** …` inside an entry. Read as the
    next field, it hid six entries behind it from the dedupe."""
    out, changed = append_update_entry(
        _REALISTIC, "Entry four.", today=date(2026, 10, 1)
    )
    assert changed is False and out == _REALISTIC


def test_a_later_authored_field_ends_the_block() -> None:
    reordered = _REALISTIC.replace(
        "**Status:** x.\n\n**Updates:**",
        "**Updates:**",
    ).replace(
        "- 2026-06-26 — Entry six after blank.\n",
        "- 2026-06-26 — Entry six after blank.\n\n**Status:** x.\n- Not an update.\n",
    )
    _, changed = append_update_entry(reordered, "Not an update.", today=date(2026, 10, 1))
    assert changed is True


def test_roll_off_is_reported_by_count_and_dates_and_nothing_is_deleted() -> None:
    """THE REPORT (#294). `roll_off_after_days` was accepted and never read.
    It now shapes an advisory: how many entries are older than the
    threshold, and which. It is still never a licence to delete."""
    result = append_update_entry(
        _REALISTIC, "Probe.", today=date(2026, 9, 17), roll_off_after_days=28
    )
    assert result.roll_off.threshold == date(2026, 8, 20)
    # 07-20, 07-01 and 06-26 are over-age; 08-28..09-01 are within 28 days.
    assert result.roll_off.dates == (date(2026, 7, 20), date(2026, 7, 1), date(2026, 6, 26))
    assert result.roll_off.count == 3
    for kept in ("Entry five", "Entry six after blank.", "(rolled)"):
        assert kept in result.text


def test_roll_off_is_reported_on_the_noop_path_too() -> None:
    result = append_update_entry(
        _REALISTIC, "Entry one.", today=date(2026, 9, 17), roll_off_after_days=28
    )
    assert result.changed is False
    assert result.roll_off.count == 3


def test_roll_off_threshold_is_exclusive_and_undated_entries_are_skipped() -> None:
    text = _REALISTIC.replace(
        "- 2026-08-28 — Entry four.", "- No date on this one.\n- 2026-08-20 — Exactly 28 days."
    )
    result = append_update_entry(text, "Probe.", today=date(2026, 9, 17), roll_off_after_days=28)
    assert date(2026, 8, 20) not in result.roll_off.dates
    assert result.roll_off.count == 3


def test_the_result_still_unpacks_as_a_two_tuple() -> None:
    """The webhook caller destructures `text, changed = append_update_entry(...)`;
    the report rides as an attribute so that contract does not move."""
    result = append_update_entry(_REALISTIC, "Probe.", today=date(2026, 9, 17))
    text, changed = result
    assert text == result.text and changed == result.changed is True
    assert hasattr(result, "roll_off")


def test_the_scaffold_placeholder_bullet_is_dropped_on_first_real_entry() -> None:
    scaffold = _REGION.replace(
        "- 2026-09-01 — First entry.\n- 2026-08-15 — Older entry.\n",
        "- _<dated — first wrap up authors this>_\n",
    )
    out, changed = append_update_entry(scaffold, "First real.", today=date(2026, 9, 17))
    assert changed is True
    assert "_<dated" not in out
    assert "**Updates:**\n- 2026-09-17 — First real.\n<!-- cp-engine:end" in out


def test_a_region_without_an_updates_field_is_refused_not_faked() -> None:
    """The old path reported `changed=True` on a region with no `**Updates:**`
    line while writing nothing — a commit that says Updates moved when it
    did not."""
    no_field = _REGION.replace("**Updates:**\n", "").replace(
        "- 2026-09-01 — First entry.\n- 2026-08-15 — Older entry.\n", ""
    )
    with pytest.raises(ExecSummaryMergeError, match="Updates"):
        append_update_entry(no_field, "Probe.", today=date(2026, 9, 17))
