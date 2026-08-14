"""The /cp-wrap skill must stay true to the code it drives (#184).

A slash command is documentation the model EXECUTES. When it names a flag
that was renamed or a payload key that moved, the failure is silent — the
model improvises and produces a plausible, wrong report. These tests bind
the prose to the implementation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_SKILL = Path(__file__).resolve().parent.parent / "plugin" / "commands" / "cp-wrap.md"


@pytest.fixture(scope="module")
def skill() -> str:
    """Raw skill text."""
    return _SKILL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def flat(skill: str) -> str:
    """Whitespace-collapsed skill text.

    The markdown is hard-wrapped at ~76 chars, so any phrase long enough to
    be worth asserting on is split across lines. Asserting against the raw
    text would make these tests fail on reflow rather than on meaning.
    """
    return " ".join(skill.split())


def test_skill_exists_and_has_frontmatter(skill: str) -> None:
    assert skill.startswith("---\n")
    assert "allowed-tools:" in skill
    assert "description:" in skill


def test_every_cp_verb_it_names_is_real(skill: str) -> None:
    """A skill citing a verb that doesn't exist sends the model into a
    guess. Checked against the live click group, not a hardcoded list."""
    from cp_engine.cli import main

    named = {"wrap", "close", "commitments-sweep"}
    registered = set(main.commands)
    missing = named - registered
    assert not missing, f"/cp-wrap names non-existent verbs: {missing}"


def test_flags_it_tells_the_model_to_use_exist(skill: str) -> None:
    from cp_engine.cli import main

    params = {
        p.name for p in main.commands["wrap"].params
    }
    for flag in ("bundle", "facts_docx"):
        assert flag in params, f"/cp-wrap uses --{flag} but wrap has no such option"
    # ...and the skill actually mentions them.
    assert "--bundle" in skill
    assert "--facts-docx" in skill


def test_bundle_keys_it_documents_are_the_keys_the_bundle_emits(skill: str) -> None:
    """The field table in the skill is a contract with the payload builder.

    These are the keys the model is told to read; if one is renamed in the
    CLI without updating the skill, the model silently loses that fact.
    """
    for key in (
        "duration_weeks",
        "budget_per_hour",
        "target_profit_pct",
        "tail_share",
        "heaviest_days",
        "not_assessable_from_data",
        "open_commitments",
        "feedback_artifacts",
    ):
        assert key in skill, f"skill never mentions bundle key {key!r}"


def test_docx_api_it_describes_matches_the_module(skill: str) -> None:
    from cp_engine.wrap_docx import WrapSection

    fields = set(WrapSection.__dataclass_fields__)
    for kwarg in ("heading", "body", "table", "blanks"):
        assert kwarg in fields
        assert kwarg in skill, f"skill omits WrapSection.{kwarg}"


def test_the_nine_section_contract_is_numbered_and_complete(skill: str) -> None:
    """Same shape every time is the whole point — two wrap reports should be
    comparable. A drifting section list defeats that."""
    for n in range(1, 10):
        assert f"{n}. **" in skill, f"section {n} missing from the contract"
    assert "nine-section contract" in skill


def test_it_forbids_inventing_human_entry_fields(skill: str) -> None:
    """The load-bearing rule ported from social-builder. If this guidance
    ever softens, wrap reports start inventing margins and licensing
    clearances — the failure the visible-blank design exists to prevent."""
    assert "Never fill a `not_assessable_from_data` field" in skill
    assert "worse than a visible blank" in skill


def test_it_requires_allocated_hours_to_be_labelled(flat: str) -> None:
    """Presenting a planning row as a timesheet actual overstates precision
    on the one number a scope conversation turns on."""
    low = flat.lower()
    assert 'call allocated hours "allocated"' in low
    assert "not timesheets" in low


def test_it_carries_the_motivating_failure(skill: str) -> None:
    """The 'why' has to survive, or the next editor trims the contract back
    to something that feels lighter and loses the point."""
    assert "actual hours: not captured" in skill
    assert "sprint_allocations" in skill


def test_it_names_the_known_commitments_blindness(skill: str) -> None:
    """`cp commitments-sweep` returned zero for EVERY engagement until
    2026-08-14. A wrap report that trusts an empty result inherits it."""
    assert "commitments-sweep" in skill
    assert "blind" in skill.lower()


def test_it_distinguishes_itself_from_cp_close(skill: str) -> None:
    """Two close-out verbs that look alike will be used interchangeably
    unless the difference is stated up front."""
    assert "This is not `cp close`" in skill
    assert "FIRST" in skill  # ordering: wrap before close


def test_it_forbids_final_in_a_filename(flat: str) -> None:
    """Standing tenant rule — version numbers, never 'final'."""
    assert 'Never "final" in a filename' in flat


def test_dropbox_guidance_avoids_the_bare_dest_name_trap(skill: str) -> None:
    """A bare `dest_name` drops the file at the Dropbox project root — a
    rule broken twice by hand before the tool default was fixed."""
    assert "03 Assets/06 Spine" in skill
    assert "bare" in skill and "dest_name" in skill
