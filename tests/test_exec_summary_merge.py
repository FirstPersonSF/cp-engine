"""Per-field merge into the exec-summary region (#251 option (c)).

WHY PER-FIELD. Measured over 60 days of the live tenant, of 45 exec-summary
rewrites: 26 touched exactly ONE field, 5 touched two, 10 rewrote all eight.
The dominant real use is "update Status, leave the rest alone", so a
whole-region verb would make the COMMON case the destructive one — a caller
sending only Status silently blanks the other seven fields.

`test_untouched_fields_survive` is the test that decision exists for.
"""

from __future__ import annotations

from datetime import date

import pytest

from cp_engine.exec_summary_merge import (
    ExecSummaryMergeError,
    merge_exec_summary_fields,
)
from cp_engine.render import EXEC_SUMMARY_END, EXEC_SUMMARY_START

TODAY = date(2026, 9, 14)


def _cp(region_body: str, *, stamp: str = "2026-07-14") -> str:
    return (
        "---\nProject: Test\n---\n\n"
        "## Facts\n| a | b |\n\n"
        f"## Exec Summary  ·  updated {stamp}\n\n"
        f"{EXEC_SUMMARY_START}\n{region_body}\n{EXEC_SUMMARY_END}\n\n"
        "## Current Work\n\nHand-written prose that must not move.\n"
    )


FULL = (
    "**Last session:** 2026-09-01\n"
    "**Objective:** Ship the safety site.\n"
    "**Status:** Audit phase.\n"
    "**Where it stands:**\n"
    "- 20% of sites reviewed.\n"
    "- Louise joins next week.\n"
    "**Next up:** Medium-effort integrations.\n"
    "**Blockers:** None.\n"
)


# ── the reason this module exists ─────────────────────────────────────


def test_untouched_fields_survive():
    """The 58% case: update one field, everything else must be byte-identical."""
    out, changed = merge_exec_summary_fields(
        _cp(FULL), {"Status": "Regional migrations next."}, today=TODAY
    )
    assert changed == ("Status",)
    assert "**Status:** Regional migrations next." in out
    # Every other field survives, values intact.
    assert "**Objective:** Ship the safety site." in out
    assert "**Next up:** Medium-effort integrations." in out
    assert "**Blockers:** None." in out
    assert "**Last session:** 2026-09-01" in out
    assert "- 20% of sites reviewed." in out
    assert "- Louise joins next week." in out


def test_content_outside_the_region_is_untouched():
    out, _ = merge_exec_summary_fields(_cp(FULL), {"Status": "x"}, today=TODAY)
    assert "## Facts\n| a | b |" in out
    assert "Hand-written prose that must not move." in out


# ── bulleted fields ───────────────────────────────────────────────────


def test_bulleted_field_replaces_its_bullets():
    out, changed = merge_exec_summary_fields(
        _cp(FULL), {"Where it stands": ["Migrations underway.", "Launch at risk."]},
        today=TODAY,
    )
    assert changed == ("Where it stands",)
    assert "- Migrations underway." in out
    assert "- Launch at risk." in out
    # The old bullets are gone, not appended to.
    assert "20% of sites reviewed" not in out
    assert "Louise joins next week" not in out
    # The field AFTER the bullets is not swallowed.
    assert "**Next up:** Medium-effort integrations." in out


def test_several_fields_at_once():
    out, changed = merge_exec_summary_fields(
        _cp(FULL),
        {"Status": "Launch at risk.", "Blockers": "Cross-region agreement."},
        today=TODAY,
    )
    assert set(changed) == {"Status", "Blockers"}
    assert "**Status:** Launch at risk." in out
    assert "**Blockers:** Cross-region agreement." in out
    assert "**Objective:** Ship the safety site." in out


# ── the freshness stamp ───────────────────────────────────────────────


def test_stamp_advances_on_a_real_change():
    out, _ = merge_exec_summary_fields(_cp(FULL), {"Status": "new"}, today=TODAY)
    assert "## Exec Summary  ·  updated 2026-09-14" in out


def test_no_op_merge_does_not_manufacture_freshness():
    """Rewriting a field with its own value must not advance the stamp.

    Otherwise a scheduled caller re-sending identical content would make a
    months-old summary read as current — the exact failure #249 measures.
    """
    out, changed = merge_exec_summary_fields(
        _cp(FULL), {"Status": "Audit phase."}, today=TODAY
    )
    assert changed == ()
    assert "updated 2026-07-14" in out
    assert "updated 2026-09-14" not in out


def test_empty_fields_mapping_is_a_no_op():
    src = _cp(FULL)
    out, changed = merge_exec_summary_fields(src, {}, today=TODAY)
    assert out == src and changed == ()


# ── caller errors surface, they do not silently write nothing ─────────


def test_unknown_field_raises():
    with pytest.raises(ExecSummaryMergeError, match="unknown exec-summary field"):
        merge_exec_summary_fields(_cp(FULL), {"Statuz": "typo"}, today=TODAY)


def test_missing_region_raises():
    no_region = "---\nProject: Test\n---\n\n## Current Work\n\nprose\n"
    with pytest.raises(ExecSummaryMergeError, match="no exec-summary region"):
        merge_exec_summary_fields(no_region, {"Status": "x"}, today=TODAY)


def test_a_typo_never_reports_success():
    """The point of raising: a typo'd label must not look like a write."""
    try:
        merge_exec_summary_fields(_cp(FULL), {"Next Up": "wrong case"}, today=TODAY)
    except ExecSummaryMergeError:
        return
    raise AssertionError("a bad label reported success")


# ── shape preservation ────────────────────────────────────────────────


def test_placeholder_field_can_be_filled():
    region = (
        "**Objective:** _<what this engagement is for>_\n"
        "**Status:** _<current state>_\n"
    )
    out, changed = merge_exec_summary_fields(
        _cp(region), {"Status": "Real content now."}, today=TODAY
    )
    assert changed == ("Status",)
    assert "**Status:** Real content now." in out
    # An untouched placeholder stays a placeholder — not invented content.
    assert "**Objective:** _<what this engagement is for>_" in out


def test_inline_to_bulleted_and_back_is_stable():
    out, _ = merge_exec_summary_fields(
        _cp(FULL), {"Blockers": ["One.", "Two."]}, today=TODAY
    )
    assert "**Blockers:**\n- One.\n- Two." in out
    back, _ = merge_exec_summary_fields(out, {"Blockers": "None."}, today=TODAY)
    assert "**Blockers:** None." in back
    assert "- One." not in back


def test_blank_list_clears_a_bulleted_field():
    out, changed = merge_exec_summary_fields(
        _cp(FULL), {"Where it stands": []}, today=TODAY
    )
    assert changed == ("Where it stands",)
    assert "20% of sites reviewed" not in out
    assert "**Next up:** Medium-effort integrations." in out


# ── a multi-line string is bullets, not an inline value ───────────────
#
# THE REPORT (2026-09-15). A hosted caller sent `Blockers` as ONE string with
# embedded newlines — five `- ` bullets in a single blob — because the verb's
# signature typed it `str`. Rendered inline, the label swallowed the first
# bullet and the field read:
#
#     **Blockers:** - **Scoper P1 baseline has not run.** …
#     - **Scoper P3+ is gated…**
#
# The other four survived only because they already carried their own
# prefixes. Nothing was lost, but the field read wrong in a file six consumers
# treat as project truth. Fixed at both ends: the verb now types the three
# bulleted fields `list[str]`, and a multi-line string is split rather than
# glued.


class TestMultilineStringBecomesBullets:
    def test_the_reported_shape(self):
        """The exact blob that produced the glued line."""
        blob = (
            "- **Scoper P1 baseline has not run.** Credentials expired.\n"
            "- **Scoper P3+ is gated on the estimator rebuild.**\n"
            "- **5151 regression, unfixed.**"
        )
        out, changed = merge_exec_summary_fields(
            _cp(FULL), {"Blockers": blob}, today=TODAY
        )
        assert changed == ("Blockers",)
        # The label must stand alone — this is the assertion that failed live.
        assert "**Blockers:**\n- **Scoper P1 baseline has not run.**" in out
        assert "**Blockers:** - " not in out

    def test_existing_bullet_markers_are_not_doubled(self):
        out, _ = merge_exec_summary_fields(
            _cp(FULL), {"Blockers": "- one\n- two"}, today=TODAY
        )
        assert "- - one" not in out
        assert "**Blockers:**\n- one\n- two" in out

    def test_lines_without_markers_still_become_bullets(self):
        out, _ = merge_exec_summary_fields(
            _cp(FULL), {"Blockers": "one\ntwo"}, today=TODAY
        )
        assert "**Blockers:**\n- one\n- two" in out

    def test_a_single_line_string_stays_inline(self):
        """Unchanged behaviour: Status and Objective are prose, not lists."""
        out, _ = merge_exec_summary_fields(
            _cp(FULL), {"Status": "One phrase."}, today=TODAY
        )
        assert "**Status:** One phrase." in out

    def test_a_list_is_still_the_preferred_shape(self):
        out, _ = merge_exec_summary_fields(
            _cp(FULL), {"Blockers": ["one", "two"]}, today=TODAY
        )
        assert "**Blockers:**\n- one\n- two" in out
