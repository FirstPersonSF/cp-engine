"""#355 item 2 (single-name aliases) and the comment's ``side_for`` bug.

ITEM 2, measured 2026-10-09: Google's Aoileann Banks is heard as "Elin".
The tenant ``[names] aliases`` comment says "Full names only — a bare first
name would also rewrite real people who share it", and the engine gave that
warning teeth: a bare-name key rewrote every occurrence, including inside a
full name ("Elin Banks" → "Aoileann Banks Banks", "Elin Smith" → "Aoileann
Banks Smith"), and a card's own one-word ``Aliases:`` were never read at all
(only multi-word card aliases became rewrites). A speaker label "Elin" also
left Aoileann "unheard", so a risk naming her was hedged although she spoke.

The rule now: a ONE-word alias is a bare first name. It rewrites only where
it stands alone (or is followed by the target's own surname), never where
another surname follows; it must be a capitalised proper noun (config
refuses anything else); and it is skipped on a workstream that knows a
different person by that first name. Cards' one-word aliases feed the same
pass, scoped to the workstream the cards apply to.

COMMENT BUG: ``side_for`` matched only the singular "interviewee" in a role,
so people under an ``**Interviewees**`` group header became ``Side: client``.

CONTROLS. Every test below FAILS on origin/main's ``src`` on its assertions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ELIN = {"Elin": "Aoileann Banks"}


# ── the rewrite itself ──────────────────────────────────────────────────


def test_bare_first_name_alias_rewrites_only_where_it_stands_alone():
    from cp_engine.attribution import apply_aliases

    assert apply_aliases("Elin approved it.", ELIN) == "Aoileann Banks approved it."
    assert apply_aliases("Thanks, Elin's notes.", ELIN) == "Thanks, Aoileann Banks's notes."
    # The target's own surname follows: one name, not "Aoileann Banks Banks".
    assert apply_aliases("Elin Banks approved.", ELIN) == "Aoileann Banks approved."
    # A different surname follows: a different person who shares the name.
    assert apply_aliases("Elin Smith approved.", ELIN) == "Elin Smith approved."
    # Never inside another word, never another case.
    assert apply_aliases("Eline, Elinor and elin stay.", ELIN) == "Eline, Elinor and elin stay."


def test_card_writer_uses_the_same_rule():
    from cp_engine import stakeholders as sh

    assert sh.apply_aliases("Elin Banks", ELIN) == "Aoileann Banks"
    assert sh.apply_aliases("Elin Smith", ELIN) == "Elin Smith"


def test_full_name_aliases_unchanged():
    from cp_engine.attribution import apply_aliases

    aliases = {"Jeff Allman": "Geoff Ahmann"}
    assert apply_aliases("Jeff Allman sent frames.", aliases) == "Geoff Ahmann sent frames."


# ── config ──────────────────────────────────────────────────────────────


def _config(tmp_path: Path, aliases: str):
    from cp_engine.config import load
    from tests.test_config import write_committed, write_local

    write_committed(tmp_path, projects=[], extra=f"[names]\naliases = {{ {aliases} }}")
    write_local(tmp_path, {})
    return load(tmp_path)


def test_config_accepts_a_capitalised_single_name(tmp_path):
    assert dict(_config(tmp_path, '"Elin" = "Aoileann Banks"').name_aliases) == ELIN


def test_config_refuses_a_single_word_that_is_not_a_proper_noun(tmp_path):
    from cp_engine.config import CommittedConfigInvalid

    with pytest.raises(CommittedConfigInvalid, match="single-word"):
        _config(tmp_path, '"will" = "Will Harper"')


# ── the plan pass: cards' one-word aliases, scoping, speaker labels ─────


def _card(dir_: Path, slug: str, name: str, aliases: str = "") -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    alias_line = f"- **Aliases:** {aliases}\n" if aliases else ""
    (dir_ / f"{slug}.md").write_text(
        f"---\nest_item_id: _authored/{slug}\nest_item_kind: context\n"
        "binding: unbound\nlayer: Stakeholders\nplacement: context\n---\n"
        f"## v1 — 2026-08-01 · live\nframing: {name} — Google EHS\n"
        f"sources:\n\n<!-- cp:stakeholder -->\n- **Name:** {name}\n{alias_line}"
        "<!-- /cp:stakeholder -->\n\nDossier.\n"
    )


def _tenant(tmp_path: Path) -> tuple[Path, Path]:
    job = tmp_path / "1p" / "google" / "ggl-5168"
    job.mkdir(parents=True)
    (tmp_path / ".cp-engine.toml").write_text("")
    return tmp_path, job


def _run(tmp_path, job, plan, *, transcript="", aliases=None):
    from cp_engine.attribution import check_plan_attribution, load_known_people

    known = load_known_people(tenant_root=tmp_path, project_dir=job, transcript=transcript)
    check_plan_attribution(plan, transcript=transcript, known_for=lambda _c: known,
                           aliases=aliases)
    return plan["projects"]["ggl-5168"]


def test_a_cards_single_name_alias_corrects_the_plan(tmp_path):
    root, job = _tenant(tmp_path)
    _card(root / "1p" / "google" / "_stakeholders", "aoileann-banks", "Aoileann Banks",
          aliases="Elin")
    block = _run(root, job, {"projects": {"ggl-5168": {
        "inbound": [{"text": "Elin sent the sheet design.", "who": "Elin", "date": "2026-10-09"}],
        "stakeholders": [{"name": "Elin", "role": "EHS"}],
    }}})
    assert block["inbound"][0]["text"] == "Aoileann Banks sent the sheet design."
    assert block["inbound"][0]["who"] == "Aoileann Banks"
    assert "stakeholders" not in block  # resolves to the card, not minted


def test_single_name_alias_skipped_where_another_person_has_that_first_name(tmp_path):
    root, job = _tenant(tmp_path)
    _card(job / "spine" / "_authored", "elin-smith", "Elin Smith")
    block = _run(root, job, {"projects": {"ggl-5168": {
        "inbound": [{"text": "Elin sent the budget.", "who": "Elin", "date": "2026-10-09"}],
    }}}, aliases=ELIN)
    assert block["inbound"][0]["text"] == "Elin sent the budget."
    assert block["inbound"][0]["who"] == "Elin"


def test_speaker_label_heard_through_the_alias(tmp_path):
    """Fathom labels Aoileann "Elin"; a risk naming her is not hedged — she
    was heard, under her mis-heard name."""
    root, job = _tenant(tmp_path)
    _card(root / "1p" / "google" / "_stakeholders", "aoileann-banks", "Aoileann Banks",
          aliases="Elin")
    transcript = "00:01:02 - Elin\nThe sheet is late.\n00:01:09 - Drew Fiero\nOK.\n"
    block = _run(root, job, {"projects": {"ggl-5168": {
        "risks": [{"text": "Aoileann Banks flagged the sheet as late."}],
    }}}, transcript=transcript)
    assert "[attribution unverified]" not in block["risks"][0]["text"]


# ── side_for (the comment's bug) ────────────────────────────────────────


@pytest.mark.parametrize("group", ["Interviewees", "Customer interviewees", "Participants",
                                   "Study participants"])
def test_interviewee_and_participant_groups_are_not_client(group):
    from cp_engine import stakeholders as sh

    m = sh.Mention(name="Adelle Bonavire", role="Director of Demand Gen", group=group)
    assert sh.side_for(m) == "external"


def test_plural_role_is_external_and_a_client_group_stays_client():
    from cp_engine import stakeholders as sh

    assert sh.side_for(sh.Mention(name="A B", role="one of the interviewees")) == "external"
    assert sh.side_for(sh.Mention(name="A B", role="Brand lead",
                                  group="Salesloft (client) participants")) == "client"


def test_cpmd_interviewees_group_imports_as_external():
    from cp_engine import stakeholders as sh

    body = ("## Stakeholders\n\n**Interviewees**\n- **Adelle Bonavire** — Director of "
            "Demand Gen, PTC\n\n## Next\n")
    (m,) = sh.parse_cpmd_section(body)
    assert sh.side_for(m) == "external"
