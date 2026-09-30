"""#312 — auto-ingest names people in risk/decision bullets from Fathom
speaker labels that are wrong.

Fixtures are shaped on the real instances: slt-5196 meeting `c3864ea3`
(09-28) carries two speaker labels, Drew Fiero and Marcello Grande, while
Leah and Morgan talk from Marcello's room; `Morgan McCauley` for the card's
Morgan Wright (09-22); `Jeff Allman` for Geoff Ahmann (08-24).
"""

from __future__ import annotations

from pathlib import Path

from tests.test_plan_from_transcript import _make_tenant_config, _stub_claude_response

TRANSCRIPT = """\
00:57:50 - Drew Fiero
  Morgan, did David get the questions?

00:57:55 - Marcello Grande
  Okay. Um. Shoot, I, I'm not at all prepared for this call.

00:58:54 - Marcello Grande
  Can you show it to me? Morgan, can you show it to me?

00:59:10 - Drew Fiero
  We'll draft scripts after the pre-interviews.
"""


def _card(dirpath: Path, slug: str, framing: str) -> None:
    d = dirpath / "spine" / "_authored"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.md").write_text(
        "---\n"
        f"est_item_id: _authored/{slug}\n"
        "layer: Stakeholders\n"
        "---\n"
        "## v1 — 2026-08-28 · live\n"
        f"framing: {framing}\n"
    )


def _tenant(tmp_path: Path) -> Path:
    project = tmp_path / "1p" / "salesloft" / "slt-5196-brand-campaign-26"
    project.mkdir(parents=True)
    (project / "cp.md").write_text("# slt-5196\n")
    _card(project, "morgan-wright", "Morgan Wright — Salesloft (customer advocacy)")
    _card(project, "leah-ward", "Leah Ward — Salesloft (brand lead, buyer)")
    _card(project, "participant-ryan-person", "Participant — Ryan Person (Sovos, Sr. Director)")
    _card(project, "participant-drew-moldenhauer", "Participant — Drew Moldenhauer (3M)")
    return project


PLAN = """
transcript:
  source: fathom
  path: /tmp/t.txt
projects:
  slt-5196-brand-campaign-26:
    risks:
      - text: "Morgan's preparedness gap is now observed — she was unprepared for the first pre-interview."
        severity: watching
        category: client
        date: "2026-09-28"
      - text: "Shoot prep compresses into one week."
        severity: watching
        category: schedule
        date: "2026-09-28"
    decisions:
      - text: "Drew Fiero will draft scripts after the pre-interviews."
        date: "2026-09-28"
    inbound:
      - text: "Morgan admitted she was 'not at all prepared' for the pre-interview."
        who: "Morgan McCauley"
        date: "2026-09-28"
      - text: "Ryan Pearson added Latoya to the roster."
        who: "Morgan Wright"
        date: "2026-09-28"
    stakeholders:
      - name: "Morgan McCauley"
        role: "Customer marketing"
      - name: "Pat Quinlan"
        role: "Sovos AE"
"""


def _generate(tmp_path, monkeypatch):
    from cp_engine.plan_from_transcript import generate_plan

    _tenant(tmp_path)
    _stub_claude_response(monkeypatch, PLAN)
    config = _make_tenant_config(tmp_path)
    transcript_path = tmp_path / "t.txt"
    transcript_path.write_text(TRANSCRIPT)
    return generate_plan(
        config=config,
        project_code="slt-5196-brand-campaign-26",
        transcript_path=transcript_path,
        api_key="stub",
    ).plan["projects"]["slt-5196-brand-campaign-26"]


def test_risk_naming_an_unheard_client_person_is_hedged(tmp_path, monkeypatch):
    block = _generate(tmp_path, monkeypatch)
    morgan_risk, schedule_risk = block["risks"]
    assert morgan_risk["text"].endswith("[attribution unverified]")
    # No person named: nothing to hedge.
    assert "[attribution unverified]" not in schedule_risk["text"]
    # Drew is a speaker AND internal: never hedged.
    assert "[attribution unverified]" not in block["decisions"][0]["text"]


def test_inbound_quote_from_an_unheard_person_is_hedged_plain_inbound_is_not(
    tmp_path, monkeypatch
):
    block = _generate(tmp_path, monkeypatch)
    quoted, plain = block["inbound"]
    assert quoted["text"].endswith("[attribution unverified]")
    # Inbound is held to the looser rule: no quote, no hedge.
    assert "[attribution unverified]" not in plain["text"]


def test_mistranscribed_surnames_resolve_to_the_card(tmp_path, monkeypatch):
    block = _generate(tmp_path, monkeypatch)
    assert block["inbound"][0]["who"] == "Morgan Wright"
    assert block["inbound"][1]["text"].startswith("Ryan Person added")
    # A speaker's own full name is known — never "corrected" to the other
    # Drew on the cards.
    assert "Drew Fiero" in block["decisions"][0]["text"]


def test_a_known_person_is_not_minted_as_a_new_stakeholder(tmp_path, monkeypatch):
    block = _generate(tmp_path, monkeypatch)
    names = [s["name"] for s in block["stakeholders"]]
    assert names == ["Pat Quinlan"]


# ── pure helpers ──────────────────────────────────────────────────────


def test_self_addressing_label_does_not_count_as_heard():
    from cp_engine.attribution import heard_first_names

    t = (
        "00:01:00 - Morgan Wright\n  Morgan, can you show it to me?\n\n"
        "00:01:05 - Leah Ward\n  Sure.\n"
    )
    heard = heard_first_names(t)
    assert "leah" in heard
    assert "morgan" not in heard


def test_ambiguous_first_name_is_never_resolved():
    from cp_engine.attribution import KnownPeople, resolve_surnames

    known = KnownPeople()
    known.add("Morgan Wright")
    known.add("Morgan Chen")
    assert resolve_surnames("Morgan McCauley called.", known) == "Morgan McCauley called."


def test_aliases_rewrite_on_word_boundaries():
    from cp_engine.attribution import apply_aliases

    aliases = {"Jeff Allman": "Geoff Ahmann", "Jeff": "Geoff"}
    assert apply_aliases("Jeff Allman sent frames; Jeff agreed.", aliases) == (
        "Geoff Ahmann sent frames; Geoff agreed."
    )
    assert apply_aliases("Jeffrey stays.", aliases) == "Jeffrey stays."


def test_names_aliases_config_parses(tmp_path):
    from cp_engine.config import load
    from tests.test_config import write_committed, write_local

    write_committed(
        tmp_path, projects=[],
        extra='[names]\naliases = { "Jeff Allman" = "Geoff Ahmann" }',
    )
    write_local(tmp_path, {})
    assert dict(load(tmp_path).name_aliases) == {"Jeff Allman": "Geoff Ahmann"}


def test_card_framings_yield_person_names_only():
    from cp_engine.attribution import name_from_framing

    assert name_from_framing("Participant — Ryan Person (Sovos, Sr.)") == "Ryan Person"
    assert name_from_framing("Mimi Shen -Sr. Director, Travel AI") == "Mimi Shen"
    assert name_from_framing("Triptych — sub-vendor profile and SOW gaps") is None
    assert name_from_framing("Google EHS cast — the wider account roster") is None
