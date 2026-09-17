r"""Appending to the tenant's improvements log (#282).

WHY IT EXISTS. `improvements.md` is a tenant FILE, so a session working only
through `cp-hosted` could READ it and had no way to add to it. That is the
worst arrangement for this particular file: the sessions most likely to hit
friction with the hosted surface were exactly the ones that could not record
it, so the log under-reports where it should report most.

THE FORMAT IS THE CONTRACT. `sweep improvements` clusters entries into issues
by their `area` tag; an entry it cannot parse is an entry the harvest drops.
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
_LOG = """# Improvements log

Some header prose.

- 2026-09-16 · `sprints parser` — An earlier observation about the parser.
- 2026-09-16 · `architecture docs` — A second earlier observation, longer.
"""


def test_the_entry_matches_the_format_the_harvest_parses() -> None:
    out = format_entry("hosted-mcp", "A tenant file was readable but not writable.", today=_TODAY)
    assert out == (
        "- 2026-09-17 · `hosted-mcp` — A tenant file was readable but not writable."
    )


def test_it_appends_newest_LAST() -> None:
    """Opposite of the Exec Summary's Updates, deliberately.

    This file is read chronologically and harvested in clusters, and every
    existing entry is in that order — appending at the top would split the
    file into two orderings, which is worse than either.
    """
    out, changed = append_entry(_LOG, "hosted-mcp", "A new observation worth keeping.", today=_TODAY)
    assert changed
    lines = [l for l in out.splitlines() if l.startswith("- ")]
    assert lines[-1].startswith("- 2026-09-17 · `hosted-mcp`")
    assert len(lines) == 3


def test_nothing_existing_is_touched() -> None:
    """NEVER DELETE is the file's own rule — the harvest marks entries in
    place, so the marker IS the archive."""
    out, _ = append_entry(_LOG, "x-area", "Another observation entirely here.", today=_TODAY)
    for original in _LOG.splitlines():
        if original.startswith("- "):
            assert original in out


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


def test_the_module_never_offers_a_delete_or_edit_path() -> None:
    """THE BOUNDARY. The harvest marks entries IN PLACE (`[→ cp-engine #N]`,
    `[fixed: <date>]`, `[dropped: …]`) and never removes them.

    A function here that could rewrite an existing entry would be a function
    that could quietly rewrite the record of a decision — so there is none,
    and this asserts it rather than trusting it.
    """
    import cp_engine.improvements as mod

    public = {n for n in dir(mod) if not n.startswith("_")}
    for banned in ("delete_entry", "remove_entry", "edit_entry", "update_entry"):
        assert banned not in public
