"""#387 — collect term and name aliases continuously, for review.

WHAT WAS MISSING. `[terms] aliases` (#371) and `[names] aliases` (#312) only
help once someone notices a mis-hearing and types it into config. SAP's
Joule went into commitments as "Juul" / "Jool" before anyone did.

THE LOOP. Ingest compares capitalised words and phrases against what the
tenant already knows (projects, companies, stakeholder cards, alias targets,
`[terms] known`) and queues near-misses in `alias-candidates.md`. Nothing
rewrites text or config until a person runs `cxp aliases --accept`; a
`--reject` keeps a pair from coming back. The weekly `/cp-prep` bundle lists
what is open.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

TODAY = date(2026, 10, 30)


@dataclass
class _Cfg:
    known_terms: tuple = ("Joule", "Concur")
    term_aliases: dict = None
    name_aliases: dict = None
    team: tuple = ("Drew Fiero",)


@dataclass
class _Card:
    name: str
    is_person: bool = True
    aliases: tuple = ()


@dataclass
class _Proj:
    name: str
    company_name: str


def _known(cards=(), roster=(), **cfg):
    from cp_engine.alias_candidates import build_known_terms

    return build_known_terms(config=_Cfg(**cfg), cards=cards, roster=roster)


def _find(text, **kw):
    from cp_engine.alias_candidates import find_candidates

    return {c.heard: (c.target, c.kind) for c in find_candidates(text, _known(**kw))}


AHMANN = [_Card("Geoff Ahmann")]


# ── similarity primitives ───────────────────────────────────────────────


@pytest.mark.parametrize("a,b,same", [
    ("Juul", "Joule", True), ("Jool", "Joule", True), ("Jeff", "Geoff", True),
    ("Tara", "Sara", False), ("Morgan", "Megan", False),
])
def test_phonetic_key(a, b, same):
    from cp_engine.alias_candidates import phonetic_key

    assert (phonetic_key(a) == phonetic_key(b)) is same


def test_edit_distance():
    from cp_engine.alias_candidates import edit_distance

    assert edit_distance("Juul", "Joule") == 2
    assert edit_distance("Tara", "Sara") == 1
    assert edit_distance("abcd", "abdc") == 1  # transposition
    assert edit_distance("same", "SAME") == 0


# ── positives ───────────────────────────────────────────────────────────


def test_juul_and_jool_are_flagged_as_joule():
    text = ("Let's schedule a 30-min SMB-focused Juul Base demo.\n"
            "We need a Juul/Jules expert. Jool is the assistant.")
    found = _find(text)
    assert found["Juul"] == ("Joule", "term")
    assert found["Jool"] == ("Joule", "term")


def test_jeff_allman_is_flagged_as_geoff_ahmann():
    found = _find("Jeff Allman will send the boards. Thanks, Jeff Allman.", cards=AHMANN)
    assert found == {"Jeff Allman": ("Geoff Ahmann", "name")}


def test_known_term_from_a_company_name_and_counts_and_context():
    from cp_engine.alias_candidates import find_candidates

    text = "The Infobox team called. Infobox wants the deck. Infoblox is fine."
    cands = find_candidates(text, _known(roster=[_Proj("Brand Refresh", "Infoblox")]))
    assert [(c.heard, c.target, c.count) for c in cands] == [("Infobox", "Infoblox", 2)]
    assert "Infobox team called" in cands[0].context


def test_a_term_alias_target_is_known_and_its_key_is_not_reproposed():
    found = _find("Juul and Jool and Jule.", known_terms=(),
                  term_aliases={"Juul": "Joule"})
    assert "Juul" not in found           # already an alias
    assert found["Jool"] == ("Joule", "term")


# ── negatives ───────────────────────────────────────────────────────────


def test_ordinary_capitalised_words_are_not_flagged():
    text = ("Monday we meet. Thanks everyone. Base camp is a base. Schedule it.\n"
            "Joules are units. Joule's roadmap. SAP and SMB. Concur works.\n"
            "Cool. Jewel is in the title. Okay. Agenda first.")
    found = _find(text)
    assert "Joules" not in found      # plural of a known term
    assert "Base" not in found        # also seen in lowercase
    assert "Concur" not in found      # exact known term
    assert "SAP" not in found and "SMB" not in found
    assert found == {}


def test_similar_real_names_both_known_are_not_flagged():
    cards = [_Card("Tara Jones"), _Card("Sara Jones")]
    text = "Tara and Sara agreed. Tara Jones owns it; Sara Jones signs."
    assert _find(text, cards=cards) == {}


def test_single_first_names_are_never_matched_against_people():
    # Only Sara is a known person; Tara is a different real person.
    assert _find("Tara said yes. Tara Smith will call.", cards=[_Card("Sara Smith")]) == {}


def test_a_common_first_name_is_not_a_mis_heard_product():
    assert _find("Joel will join the call.") == {}


def test_speaker_label_lines_are_ignored():
    text = "00:01:02 - Jeff Allman\nhello there\n00:01:09 - Drew Fiero\nok"
    assert _find(text, cards=AHMANN) == {}


def test_different_surname_is_not_a_mishearing():
    assert _find("Geoff White joined.", cards=AHMANN) == {}


# ── the review file ─────────────────────────────────────────────────────


def _cands(text, **kw):
    from cp_engine.alias_candidates import find_candidates

    return find_candidates(text, _known(**kw))


def test_record_creates_the_file_and_is_idempotent_per_meeting(tmp_path):
    from cp_engine import alias_candidates as ac

    cands = _cands("Juul Base demo. Jool too.")
    assert ac.record(tmp_path, cands, code="sap-5198", meeting_id="abcdef123456",
                     today=TODAY) == 2
    text = (tmp_path / ac.FILENAME).read_text()
    assert text.startswith("---\n")
    assert '- 2026-10-30 · term · "Juul" → Joule · ×1 · sap-5198 · meeting abcdef12 · ' in text
    # Re-ingest of the same meeting adds nothing.
    assert ac.record(tmp_path, cands, code="sap-5198", meeting_id="abcdef123456",
                     today=TODAY) == 0
    # Another meeting adds its own lines.
    assert ac.record(tmp_path, cands, code="sap-5198", meeting_id="99999999",
                     today=TODAY) == 2
    groups = ac.grouped(tmp_path)
    assert [(g.heard, g.count, len(g.meetings)) for g in groups] == [
        ("Jool", 2, 2), ("Juul", 2, 2)]
    lines = ac.render_review(groups)
    assert lines[0] == "## Alias candidates (2 open)"
    assert any('"Juul" → Joule' in ln and "2 meetings" in ln for ln in lines)


def _tenant(tmp_path, extra=""):
    from tests.test_config import write_committed, write_local

    tmp_path.mkdir(parents=True, exist_ok=True)
    write_committed(tmp_path, projects=[], extra=extra)
    write_local(tmp_path, {})
    return tmp_path


def test_accept_writes_terms_and_names_and_reject_never_returns(tmp_path):
    from cp_engine import alias_candidates as ac
    from cp_engine.config import load

    root = _tenant(tmp_path, '# keep me\n[terms]\nknown = ["Joule"]\n\n'
                             '[terms.aliases]\n"Juul" = "Joule"\n')
    before = (root / ".cp-engine.toml").read_text()
    ac.record(root, _cands("Jool and Jule.", known_terms=("Joule",)),
              code="sap-5198", meeting_id="m1", today=TODAY)
    ac.record(root, _cands("Jeff Allman called.", cards=AHMANN),
              code="ggl-5168", meeting_id="m2", today=TODAY)
    assert {g.heard for g in ac.grouped(root)} == {"Jool", "Jule", "Jeff Allman"}

    result = ac.review(root, accept=["Jool", "Jeff Allman"], reject=["Jule"], today=TODAY)
    assert result["accepted"] == [("Jool", "Joule", "term"),
                                  ("Jeff Allman", "Geoff Ahmann", "name")]
    assert result["rejected"] == [("Jule", "Joule", "term")]

    cfg_text = (root / ".cp-engine.toml").read_text()
    assert cfg_text.startswith(before.split("[terms]")[0])   # untouched above
    assert "# keep me" in cfg_text
    cfg = load(root)
    assert dict(cfg.term_aliases) == {"Juul": "Joule", "Jool": "Joule"}
    assert dict(cfg.name_aliases) == {"Jeff Allman": "Geoff Ahmann"}
    assert cfg.known_terms == ("Joule",)

    assert ac.grouped(root) == []
    text = (root / ac.FILENAME).read_text()
    accepted = text.split("## Accepted")[1].split("## Rejected")[0]
    assert '"Jool" → Joule' in accepted and '"Jeff Allman" → Geoff Ahmann' in accepted
    assert '"Jule" → Joule' in text.split("## Rejected")[1]

    # A rejected (or accepted) pair is not proposed again by a later ingest.
    assert ac.record(root, _cands("Jule and Jool.", known_terms=("Joule",)),
                     code="sap-5198", meeting_id="m3", today=TODAY) == 0


def test_accept_with_another_target_and_errors(tmp_path):
    from cp_engine import alias_candidates as ac
    from cp_engine.config import load

    root = _tenant(tmp_path)
    ac.record(root, _cands("Jool demo."), code="sap-5198", meeting_id="m1", today=TODAY)
    with pytest.raises(ac.ReviewError, match="no open candidate"):
        ac.review(root, accept=["Nope"], today=TODAY)
    with pytest.raises(ac.ReviewError, match="both"):
        ac.review(root, accept=["Jool"], reject=["Jool"], today=TODAY)
    ac.review(root, accept=["Jool=Joule AI"], today=TODAY)
    assert dict(load(root).term_aliases) == {"Jool": "Joule AI"}


def test_review_writes_nothing_when_a_choice_is_invalid(tmp_path):
    from cp_engine import alias_candidates as ac

    root = _tenant(tmp_path)
    ac.record(root, _cands("Jool demo."), code="sap-5198", meeting_id="m1", today=TODAY)
    cfg, queue = (root / ".cp-engine.toml").read_text(), (root / ac.FILENAME).read_text()
    with pytest.raises(ac.ReviewError):
        ac.review(root, accept=["Jool"], reject=["Missing"], today=TODAY)
    assert (root / ".cp-engine.toml").read_text() == cfg
    assert (root / ac.FILENAME).read_text() == queue


# ── config ──────────────────────────────────────────────────────────────


def test_config_terms_known_alone_and_malformed(tmp_path):
    from cp_engine import CommittedConfigInvalid
    from cp_engine.config import load

    root = _tenant(tmp_path / "a", '[terms]\nknown = ["Joule", "Concur", "Joule"]\n')
    cfg = load(root)
    assert cfg.known_terms == ("Joule", "Concur") and dict(cfg.term_aliases) == {}
    assert load(_tenant(tmp_path / "b")).known_terms == ()
    with pytest.raises(CommittedConfigInvalid, match=r"\[terms\]\.known"):
        load(_tenant(tmp_path / "c", '[terms]\nknown = "Joule"\n'))


# ── the CLI ─────────────────────────────────────────────────────────────


def test_cxp_aliases_lists_and_decides(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from cp_engine import alias_candidates as ac
    from cp_engine.cli import main

    root = _tenant(tmp_path)
    ac.record(root, _cands("Jool demo. Jule."), code="sap-5198", meeting_id="m1",
              today=TODAY)
    monkeypatch.chdir(root)
    runner = CliRunner()
    out = runner.invoke(main, ["aliases"])
    assert out.exit_code == 0, out.output
    assert '"Jool" → Joule' in out.output and "2 open" in out.output

    out = runner.invoke(main, ["aliases", "--accept", "Jool", "--reject", "Jule"])
    assert out.exit_code == 0, out.output
    assert 'accepted "Jool" → Joule ([terms.aliases])' in out.output
    assert runner.invoke(main, ["aliases"]).output.strip() == "No open alias candidates."

    out = runner.invoke(main, ["aliases", "--accept", "Gone"])
    assert out.exit_code == 1 and "no open candidate" in out.output


# ── the weekly surface ──────────────────────────────────────────────────


def test_planning_bundle_lists_open_candidates():
    from cp_engine.prep_planning import PlanningResult, render_planning_bundle

    base = dict(week_iso="2026-W45", week_dates="Nov 2 – Nov 8", project_count=0,
                estimated_minutes=60, tenant_hours_last_week={}, blocks_by_account={},
                milestone_counts={}, urgent_counts={})
    lines = ("## Alias candidates (1 open)", "", '1. **"Juul" → Joule** (term)', "")
    assert "## Alias candidates (1 open)" in render_planning_bundle(
        PlanningResult(**base, alias_candidates=lines))
    assert "Alias candidates" not in render_planning_bundle(PlanningResult(**base))


# ── the webhook ingest hook ─────────────────────────────────────────────


def test_webhook_queues_candidates_and_never_raises(tmp_path):
    import pipeline

    from cp_engine import alias_candidates as ac
    from cp_engine.config import load

    root = _tenant(tmp_path, '[terms]\nknown = ["Joule"]\n')
    config = load(root)
    transcript = root / "t.txt"
    transcript.write_text("00:00:01 - Drew Fiero\nLet's book the Juul Base demo.\n")
    n = pipeline._collect_alias_candidates(
        config=config, code="sap-5198", transcript_path=transcript,
        meeting={"id": "meet-1", "summary": "Jool demo agreed."}, meeting_id="meet-1",
        action_items=[{"description": "Schedule Juul demo"}], roster=[],
    )
    assert n == 2
    assert {g.heard for g in ac.grouped(root)} == {"Juul", "Jool"}

    missing = root / "nope.txt"
    assert pipeline._collect_alias_candidates(
        config=config, code="sap-5198", transcript_path=missing, meeting=None,
        meeting_id="m", action_items=None, roster=None) == "error"
