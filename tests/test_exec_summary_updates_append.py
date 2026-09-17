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
