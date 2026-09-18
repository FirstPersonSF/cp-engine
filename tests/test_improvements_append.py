r"""Appending to the tenant's improvements log (#282, #293).

WHY IT EXISTS. `improvements.md` is a tenant FILE, so a session working only
through `cp-hosted` could READ it and had no way to add to it. That is the
worst arrangement for this particular file: the sessions most likely to hit
friction with the hosted surface were exactly the ones that could not record
it, so the log under-reports where it should report most.

THE FORMAT IS THE CONTRACT. `sweep improvements` clusters entries into issues
by their `area` tag; an entry it cannot parse is an entry the harvest drops.

THE FILE IS SECTIONED (#293). `## Open` then `## Resolved`. An append at EOF
lands under Resolved — which is what shipped, and what the first webhook
commit did. The section tests below use a fixture shaped like the real tenant
file: multi-line entries, a multi-backtick area, and a harvest marker.
"""

from __future__ import annotations

from datetime import date

import pytest

from cp_engine.improvements import (
    ImprovementsError,
    append_entry,
    format_entry,
)

_TODAY = date(2026, 9, 17)

# No section headings at all — the EOF fallback shape.
_LOG = """# Improvements log

Some header prose.

- 2026-09-16 · `sprints parser` — An earlier observation about the parser.
- 2026-09-16 · `architecture docs` — A second earlier observation, longer.
"""

# Shaped like the tenant file: header block, `## Open`, `## Resolved`.
_SECTIONED = """---
Project: First Person tenant
---

# Improvements log

**Protocol:** log at the moment of friction. Never delete entries.

---

## Open

- 2026-09-04 · `protocol` — **Check whether the tool already exists** before
  proposing to build it. I recommended building a sweep that was built the
  day before.

  A second paragraph of the same entry, indented.
- 2026-09-10 · `mcp`/`engine` — A multi-backtick area the entry regex never
  matched, so it was never deduped.
- 2026-09-12 · `engine` — An already-harvested observation. [→ cp-engine #280]

## Resolved

- 2026-07-21 · `mcp` — A resolved observation. [fixed: 2026-08-01]
- 2026-08-30 · `dashboard` — The last resolved observation in the file.
"""


def _entries(text: str) -> list[str]:
    return [l for l in text.splitlines() if l.startswith("- ")]


def _section(text: str, heading: str) -> list[str]:
    """Lines between `heading` and the next `## ` heading (or EOF)."""
    lines = text.splitlines()
    start = lines.index(heading)
    out: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        out.append(line)
    return out


def test_the_entry_matches_the_format_the_harvest_parses() -> None:
    out = format_entry("hosted-mcp", "A tenant file was readable but not writable.", today=_TODAY)
    assert out == (
        "- 2026-09-17 · `hosted-mcp` — A tenant file was readable but not writable."
    )


# --- placement (#293) -------------------------------------------------------


def test_it_lands_at_the_END_of_open_not_under_resolved() -> None:
    """THE BUG. `sweep improvements` reads section membership, and an append
    at EOF is an append under `## Resolved`. The entry must be the last
    bullet of `## Open`, and `## Resolved` must be exactly as it was.
    """
    out, changed = append_entry(
        _SECTIONED, "hosted-mcp", "A new observation worth keeping.", today=_TODAY
    )
    assert changed

    open_entries = [l for l in _section(out, "## Open") if l.startswith("- ")]
    assert open_entries[-1].startswith("- 2026-09-17 · `hosted-mcp`")
    assert len(open_entries) == 4

    assert _section(out, "## Resolved") == _section(_SECTIONED, "## Resolved")
    assert out.index("`hosted-mcp` — A new observation") < out.index("## Resolved")


def test_one_blank_line_separates_the_new_entry_from_the_next_heading() -> None:
    out, _ = append_entry(_SECTIONED, "hosted-mcp", "A new observation worth keeping.", today=_TODAY)
    lines = out.splitlines()
    i = lines.index("## Resolved")
    assert lines[i - 1] == ""
    assert lines[i - 2].startswith("- 2026-09-17 · `hosted-mcp`")


def test_a_section_with_no_blank_before_the_heading_gets_one() -> None:
    """Inserting is not allowed to glue the new bullet onto `## Resolved`."""
    tight = "## Open\n\n- 2026-09-01 · `a` — An observation that sits tight.\n## Resolved\n\n- 2026-07-01 · `b` — Resolved thing here.\n"
    out, _ = append_entry(tight, "x", "A new observation worth keeping.", today=_TODAY)
    lines = out.splitlines()
    i = lines.index("## Resolved")
    assert lines[i - 1] == ""
    assert lines[i - 2].startswith("- 2026-09-17 · `x`")


def test_an_empty_open_section_receives_the_entry() -> None:
    empty = "## Open\n\n## Resolved\n\n- 2026-07-01 · `b` — Resolved thing here.\n"
    out, changed = append_entry(empty, "x", "A new observation worth keeping.", today=_TODAY)
    assert changed
    assert [l for l in _section(out, "## Open") if l.startswith("- ")] == [
        "- 2026-09-17 · `x` — A new observation worth keeping."
    ]
    assert _section(out, "## Resolved") == _section(empty, "## Resolved")


def test_the_result_names_its_placement() -> None:
    sectioned = append_entry(_SECTIONED, "x", "A new observation worth keeping.", today=_TODAY)
    flat = append_entry(_LOG, "x", "A new observation worth keeping.", today=_TODAY)
    assert sectioned.placement == "open"
    assert flat.placement == "eof"
    # Still unpacks as the pair every existing caller wrote.
    text, changed = sectioned
    assert changed is True and isinstance(text, str)


def test_without_an_open_heading_it_appends_newest_LAST() -> None:
    """THE FALLBACK. No `## Open` → EOF, newest last — opposite of the Exec
    Summary's Updates, deliberately: this file is read chronologically.
    """
    out, changed = append_entry(_LOG, "hosted-mcp", "A new observation worth keeping.", today=_TODAY)
    assert changed
    lines = _entries(out)
    assert lines[-1].startswith("- 2026-09-17 · `hosted-mcp`")
    assert len(lines) == 3


# --- append-only -------------------------------------------------------------


def test_nothing_existing_is_touched() -> None:
    """NEVER DELETE is the file's own rule — the harvest marks entries in
    place, so the marker IS the archive."""
    out, _ = append_entry(_SECTIONED, "x-area", "Another observation entirely here.", today=_TODAY)
    for original in _SECTIONED.splitlines():
        if original.startswith("- "):
            assert original in out


def test_a_round_trip_only_ever_adds_exactly_one_line() -> None:
    """THE BOUNDARY, asserted on behaviour rather than on function names.

    A function here that could rewrite an existing entry would be a function
    that could quietly rewrite the record of a decision. So: N existing lines
    in, one new entry then a duplicate of it, and every original line is still
    present VERBATIM and IN ORDER, with the line count up by exactly one.
    """
    before = _SECTIONED.splitlines()

    once, a = append_entry(_SECTIONED, "hosted-mcp", "A new observation worth keeping.", today=_TODAY)
    twice, b = append_entry(once, "hosted-mcp", "A new observation worth keeping.", today=_TODAY)
    assert a is True and b is False

    after = twice.splitlines()
    assert len(after) == len(before) + 1

    # Every original line survives, verbatim, in order: removing the one
    # inserted line gives back the original sequence exactly.
    inserted = [i for i, l in enumerate(after) if l.startswith("- 2026-09-17 · `hosted-mcp`")]
    assert len(inserted) == 1
    survivors = after[: inserted[0]] + after[inserted[0] + 1 :]
    assert survivors == before


# --- dedupe -------------------------------------------------------------------


def test_a_duplicate_is_a_noop() -> None:
    """Retry after a timeout must not double-log."""
    once, a = append_entry(_LOG, "hosted-mcp", "Repeated observation for the retry.", today=_TODAY)
    twice, b = append_entry(once, "hosted-mcp", "Repeated observation for the retry.", today=_TODAY)
    assert a is True and b is False
    assert twice == once


def test_a_duplicate_across_midnight_is_still_a_noop() -> None:
    """THE RETRY CASE THAT ACTUALLY HAPPENS. A timeout near midnight retries
    on a new date; the observation is the same observation, not a new one.

    Dedupe therefore matches on CONTENT, not on the whole dated line — the
    same bug found and fixed in the Updates append the same day.
    """
    once, _ = append_entry(_LOG, "hosted-mcp", "An observation spanning midnight.", today=_TODAY)
    twice, changed = append_entry(once, "hosted-mcp", "An observation spanning midnight.", today=date(2026, 9, 18))
    assert changed is False
    assert twice == once


def test_dedupe_normalises_the_EXISTING_side_too() -> None:
    """#293. The old code normalised the new entry's whitespace and compared
    the file's line exactly, so a hand-wrapped existing entry never matched."""
    wrapped = "## Open\n\n- 2026-09-01 · `engine` —   An   observation   that was\n  wrapped by hand   in the editor.\n\n## Resolved\n"
    out, changed = append_entry(
        wrapped, "engine", "An observation that was wrapped by hand in the editor.", today=_TODAY
    )
    assert changed is False
    assert out == wrapped


def test_a_multi_backtick_area_cannot_escape_dedupe() -> None:
    """#293. `` `mcp`/`engine` `` never matched `_ENTRY_RE`, so those entries
    were skipped by the dedupe entirely. The regex is for parsing, not for
    deciding which lines count: the same observation logged under `mcp` —
    one of that tag's spans — is a duplicate of it.
    """
    body = "A multi-backtick area the entry regex never matched, so it was never deduped."
    for area in ("mcp", "engine"):
        out, changed = append_entry(_SECTIONED, area, body, today=_TODAY)
        assert changed is False, area
        assert out == _SECTIONED
    # A different area with the same words is NOT the same entry.
    _, changed = append_entry(_SECTIONED, "dashboard", body, today=_TODAY)
    assert changed is True


def test_a_harvest_marked_entry_is_still_a_duplicate() -> None:
    """#293. `[→ cp-engine #N]` appended by the harvest must not turn a
    logged observation into a re-loggable one."""
    out, changed = append_entry(
        _SECTIONED, "engine", "An already-harvested observation.", today=_TODAY
    )
    assert changed is False
    assert out == _SECTIONED


def test_a_multi_line_existing_entry_is_compared_whole() -> None:
    """Tenant entries wrap across indented lines. Comparing only the first
    line would let the same observation in single-line form slip past."""
    out, changed = append_entry(
        _SECTIONED,
        "protocol",
        "**Check whether the tool already exists** before proposing to build it. "
        "I recommended building a sweep that was built the day before. "
        "A second paragraph of the same entry, indented.",
        today=_TODAY,
    )
    assert changed is False
    assert out == _SECTIONED


# --- format ---------------------------------------------------------------------


def test_a_thin_observation_is_refused() -> None:
    """A one-word entry is noise the harvest cannot cluster or act on."""
    for bad in ("", "slow", "it broke"):
        with pytest.raises(ImprovementsError):
            format_entry("area", bad, today=_TODAY)


def test_an_area_is_required() -> None:
    """`sweep improvements` clusters on the area tag — an entry without one
    cannot be grouped with its siblings."""
    with pytest.raises(ImprovementsError):
        format_entry("", "A perfectly reasonable observation about something.", today=_TODAY)


def test_a_backtick_in_the_area_cannot_break_the_format() -> None:
    """The area is rendered inside backticks; one inside it would terminate
    the span and produce a line the entry regex no longer matches."""
    with pytest.raises(ImprovementsError):
        format_entry("we`ird", "A perfectly reasonable observation about something.", today=_TODAY)


def test_whitespace_in_the_observation_is_normalised() -> None:
    """An entry is one line. A pasted multi-line observation that kept its
    newlines would break the one-bullet-per-entry shape the harvest reads."""
    out = format_entry("area", "A multi\n  line   observation\nthat was pasted in.", today=_TODAY)
    assert "\n" not in out
    assert "  " not in out.split(" — ", 1)[1]
