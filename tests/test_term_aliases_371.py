"""#371 — `[terms] aliases`: mis-heard product and brand terms.

WHAT BROKE. On sap-5198 the 10-16 ingest wrote SAP's Joule as "Juul" and
"Jool" ("Schedule 30-min SMB-focused Juul Base demo…", "Juul/Jules
expert"), and those strings landed in commitments. `[names] aliases`
could not fix it: a one-word name key must be a first name and is left
alone when another capitalised word follows ("Juul Base").

THE RULE. `[terms] aliases = {"Juul" = "Joule"}` is a plain whole-word,
case-sensitive rewrite (hyphen counts as a boundary) run over every prose
field of the plan BEFORE the name pass, and over each proposed commitment's
description before it is screened and hashed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

JOULE = {"Juul": "Joule", "Jool": "Joule"}


# ── the rewrite ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("before,after", [
    ("Schedule 30-min SMB-focused Juul Base demo", "Schedule 30-min SMB-focused Joule Base demo"),
    ("Juul/Jules expert", "Joule/Jules expert"),
    ("Jool and Juul are the same thing.", "Joule and Joule are the same thing."),
    ("Juul's roadmap", "Joule's roadmap"),
    ("a Juul-based agent", "a Joule-based agent"),
    ("(Jool)", "(Joule)"),
])
def test_term_alias_rewrites_whole_words(before, after):
    from cp_engine.attribution import apply_terms

    assert apply_terms(before, JOULE) == after


@pytest.mark.parametrize("text", [
    "Juuls ship Monday.",     # key inside a longer word
    "Joolie said hi.",        # a name that starts with the key
    "MyJuul app",             # key at the end of a word
    "jool lowercase",         # case-sensitive
    "Juul_tag",               # underscore is a word char
])
def test_term_alias_leaves_substrings_alone(text):
    from cp_engine.attribution import apply_terms

    assert apply_terms(text, JOULE) == text


def test_longest_key_first_and_no_aliases_is_identity():
    from cp_engine.attribution import apply_terms

    terms = {"S4": "S/4", "S4 HANA": "S/4HANA"}
    assert apply_terms("Move to S4 HANA.", terms) == "Move to S/4HANA."
    assert apply_terms("Juul", {}) == "Juul"
    assert apply_terms("", JOULE) == ""


# ── the plan pass ───────────────────────────────────────────────────────


def _plan():
    return {
        "projects": {"sap-5198": {
            "asks": [{"text": "Schedule 30-min SMB-focused Juul Base demo",
                      "owner": "Jeff Allman", "date": "2026-10-16"}],
            "decisions": [{"text": "Lead the spot with Jool.", "date": "2026-10-16"}],
        }},
        "account_summary": {"text": "SAP wants a Juul/Jules expert on the call."},
        "account_decisions": [{"text": "Juul stays the hero product."}],
        "themes": [{"text": "Jool everywhere", "date": "2026-10-16"}],
    }


def _known():
    from cp_engine.attribution import KnownPeople

    return KnownPeople()


def test_plan_pass_rewrites_terms_and_people_still_alias():
    from cp_engine.attribution import check_plan_attribution

    plan = _plan()
    summary = check_plan_attribution(
        plan, transcript="", known_for=lambda _c: _known(),
        aliases={"Jeff Allman": "Geoff Ahmann"}, term_aliases=JOULE,
    )
    block = plan["projects"]["sap-5198"]
    assert block["asks"][0]["text"] == "Schedule 30-min SMB-focused Joule Base demo"
    assert block["asks"][0]["owner"] == "Geoff Ahmann"  # people aliasing intact
    assert block["decisions"][0]["text"].startswith("Lead the spot with Joule.")
    assert plan["account_summary"]["text"] == "SAP wants a Joule/Jules expert on the call."
    assert plan["account_decisions"][0]["text"] == "Joule stays the hero product."
    assert plan["themes"][0]["text"] == "Joule everywhere"
    assert block["asks"][0]["date"] == "2026-10-16"  # identifiers untouched
    assert summary["terms"] == 5 and summary["aliased"] >= 1


def test_plan_pass_without_terms_keeps_the_old_summary_shape():
    from cp_engine.attribution import check_plan_attribution

    plan = _plan()
    summary = check_plan_attribution(plan, transcript="", known_for=lambda _c: _known())
    assert "terms" not in summary
    assert "Juul" in plan["projects"]["sap-5198"]["asks"][0]["text"]


def test_apply_attribution_checks_reads_term_aliases_from_config(tmp_path):
    from types import SimpleNamespace

    from cp_engine.plan_from_transcript import apply_attribution_checks

    config = SimpleNamespace(root=None, team=(), name_aliases={}, term_aliases=JOULE)
    plan = _plan()
    apply_attribution_checks(plan, config=config, transcript="")
    assert plan["projects"]["sap-5198"]["asks"][0]["text"].endswith("Joule Base demo")


# ── config ──────────────────────────────────────────────────────────────


def _load(tmp_path, extra):
    from cp_engine.config import load
    from tests.test_config import write_committed, write_local

    write_committed(tmp_path, projects=[], extra=extra)
    write_local(tmp_path, {})
    return load(tmp_path)


def test_config_parses_terms_beside_names(tmp_path):
    cfg = _load(tmp_path, '[names]\naliases = { "Jeff Allman" = "Geoff Ahmann" }\n'
                          '[terms]\naliases = { "Juul" = "Joule", "Jool" = "Joule" }')
    assert dict(cfg.term_aliases) == JOULE
    assert dict(cfg.name_aliases) == {"Jeff Allman": "Geoff Ahmann"}


def test_config_without_terms_is_empty(tmp_path):
    assert dict(_load(tmp_path, "").term_aliases) == {}


def test_config_refuses_a_malformed_terms_table(tmp_path):
    from cp_engine import CommittedConfigInvalid

    with pytest.raises(CommittedConfigInvalid, match=r"\[terms\]\.aliases"):
        _load(tmp_path, '[terms]\naliases = { "Juul" = "" }')
    with pytest.raises(CommittedConfigInvalid, match=r"\[terms\]\.aliases"):
        _load(tmp_path, '[terms]\naliases = ["Juul"]')


# ── proposed commitments ────────────────────────────────────────────────


def test_proposed_commitment_description_is_rewritten_before_hashing(monkeypatch):
    import commitments_propose as cprop
    from tests.test_commitments_propose import _FakeClient

    from cp_engine.asks import ask_hash

    row = {"id": "m-1", "participants": [],
           "action_items": [{"description": "Schedule 30-min SMB-focused Juul Base demo"}]}
    monkeypatch.setattr(cprop.mc2_db, "get_client", lambda required=False: _FakeClient(row))
    monkeypatch.setattr(cprop, "resolve_commitment_owner",
                        lambda client, code: {"id": "p", "code": code, "kind": "project"})
    written: list[dict] = []
    monkeypatch.setattr(cprop, "write_commitment",
                        lambda client, **kw: written.append(kw) or "inserted")

    cprop.propose_commitments("m-1", ["sap-5198"], term_aliases=JOULE)
    assert written[0]["description"] == "Schedule 30-min SMB-focused Joule Base demo"
    # Same identity as the sprint-file ask the (rewritten) plan writes.
    assert written[0]["cp_hash"] == ask_hash("sap-5198", written[0]["description"])

    written.clear()
    cprop.propose_commitments("m-1", ["sap-5198"])  # no terms: unchanged
    assert "Juul" in written[0]["description"]
