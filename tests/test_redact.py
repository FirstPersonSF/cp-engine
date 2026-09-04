"""Tests for cp_engine.redact — the anonymisation verification pass (spec §4).

Every case here is a leak that actually happened in the 2026-09-04 RFP
session, or the inverse of one. The theme: a find-and-replace on the
client-name field catches the first-order leak and none of the others.
"""

from __future__ import annotations

from cp_engine.redact import check_redaction, render_report

_CLEAN_DRAFT = """
# Partner RFP — 2027 brand video set

We are First Person, agency of record to a global enterprise software
company in the spend-management category. We'll name them under NDA once
we're in conversation.

## Deliverables
One :30, two :15s and two :06s, with 9x16 and square cuts.

## Our recent work
Campaigns for Salesloft, Infoblox and Teleflex.
"""


def test_a_clean_draft_passes() -> None:
    rep = check_redaction(
        _CLEAN_DRAFT,
        client_name="SAP Concur",
        client_aliases=("Concur",),
        competitors=("Ramp", "Navan", "Workday"),
        our_roster=("Salesloft", "Infoblox", "Teleflex", "SAP Concur"),
        descriptor="a global enterprise software company",
    )
    assert rep.clean, [f.detail for f in rep.findings]


# ──────────────────────────────────────────────────────────────────────
#  First-order: the client's own name
# ──────────────────────────────────────────────────────────────────────


def test_the_client_name_is_a_leak() -> None:
    rep = check_redaction(
        _CLEAN_DRAFT.replace("a global enterprise software\ncompany",
                             "SAP Concur"),
        client_name="SAP Concur",
    )
    assert not rep.clean
    assert any(f.kind == "client-name" for f in rep.findings)


def test_a_short_form_alias_is_caught() -> None:
    """'SAP Concur' replaced, 'Concur' left behind — the classic miss."""
    draft = _CLEAN_DRAFT + "\nConcur's finance buyers are the audience.\n"
    rep = check_redaction(draft, client_name="SAP Concur",
                          client_aliases=("Concur",))
    assert any(f.kind == "client-name" and "Concur" in f.detail
               for f in rep.findings)


def test_name_matching_is_word_bounded() -> None:
    """'SAP' must not fire on 'SAPling' or 'ASAP'."""
    draft = _CLEAN_DRAFT + "\nWe need this ASAP; a SAPling is not a tree.\n"
    rep = check_redaction(draft, client_name="SAP",
                          descriptor="a global enterprise software company")
    assert not any(f.kind == "client-name" for f in rep.findings)


def test_name_matching_is_case_insensitive() -> None:
    draft = _CLEAN_DRAFT + "\nsap concur wants a January launch.\n"
    rep = check_redaction(draft, client_name="SAP Concur")
    assert any(f.kind == "client-name" for f in rep.findings)


# ──────────────────────────────────────────────────────────────────────
#  Second-order: our OWN roster (the leak that cost a pass)
# ──────────────────────────────────────────────────────────────────────


def test_the_client_left_in_our_own_credentials_list_is_flagged() -> None:
    """The 5198 miss: removing the client from the brief but not from
    First Person's own five-name client roster in the boilerplate."""
    draft = _CLEAN_DRAFT.replace(
        "Campaigns for Salesloft, Infoblox and Teleflex.",
        "Campaigns for Salesloft, Infoblox, Teleflex and SAP Concur.",
    )
    rep = check_redaction(
        draft,
        client_name="SAP Concur",
        our_roster=("Salesloft", "Infoblox", "Teleflex", "SAP Concur"),
        descriptor="a global enterprise software company",
    )
    assert not rep.clean
    assert any(f.kind == "client-name" for f in rep.findings)


def test_other_roster_names_are_reported_as_risk_not_leak() -> None:
    """Our other clients appearing is normal — it is only a leak when the
    redacted client is among them. Report, do not block."""
    rep = check_redaction(
        _CLEAN_DRAFT,
        client_name="SAP Concur",
        our_roster=("Salesloft", "Infoblox", "Teleflex"),
        descriptor="a global enterprise software company",
    )
    assert rep.clean, "other clients in the roster must not block"
    assert any(f.kind == "our-roster" for f in rep.findings)


# ──────────────────────────────────────────────────────────────────────
#  Triangulation: competitors identify without naming
# ──────────────────────────────────────────────────────────────────────


def test_a_competitor_set_is_a_leak_even_with_no_client_name() -> None:
    """Ramp + Navan + Workday + 'spend management' names the client."""
    draft = _CLEAN_DRAFT + (
        "\nThe brief positions against Ramp, Navan and Workday.\n"
    )
    rep = check_redaction(
        draft,
        client_name="SAP Concur",
        competitors=("Ramp", "Navan", "Workday"),
        descriptor="a global enterprise software company",
    )
    assert not rep.clean
    assert any(f.kind == "competitor-set" for f in rep.findings)


def test_one_named_competitor_is_a_risk_not_a_leak() -> None:
    draft = _CLEAN_DRAFT + "\nThey compete with Ramp on onboarding speed.\n"
    rep = check_redaction(
        draft,
        client_name="SAP Concur",
        competitors=("Ramp", "Navan", "Workday"),
        descriptor="a global enterprise software company",
    )
    assert rep.clean, "a single competitor should warn, not block"
    assert any(f.kind == "competitor" for f in rep.findings)


# ──────────────────────────────────────────────────────────────────────
#  The descriptor and the NDA line
# ──────────────────────────────────────────────────────────────────────


def test_a_vague_descriptor_is_flagged() -> None:
    rep = check_redaction(_CLEAN_DRAFT, client_name="SAP Concur",
                          descriptor="a company")
    assert not rep.clean
    assert any(f.kind == "descriptor" for f in rep.findings)


def test_a_specific_descriptor_passes() -> None:
    rep = check_redaction(_CLEAN_DRAFT, client_name="SAP Concur",
                          descriptor="a global enterprise software company")
    assert not any(f.kind == "descriptor" for f in rep.findings)


def test_a_missing_nda_line_is_flagged() -> None:
    """Without it an anonymous brief reads as a fishing expedition."""
    draft = _CLEAN_DRAFT.replace(
        "We'll name them under NDA once\nwe're in conversation.", ""
    )
    rep = check_redaction(draft, client_name="SAP Concur",
                          descriptor="a global enterprise software company")
    assert not rep.clean
    assert any(f.kind == "nda-line" for f in rep.findings)


def test_the_nda_line_is_matched_on_commitment_not_exact_wording() -> None:
    draft = _CLEAN_DRAFT.replace(
        "We'll name them under NDA once\nwe're in conversation.",
        "Happy to identify the client under an NDA at first call.",
    )
    rep = check_redaction(draft, client_name="SAP Concur",
                          descriptor="a global enterprise software company")
    assert not any(f.kind == "nda-line" for f in rep.findings)


# ──────────────────────────────────────────────────────────────────────
#  Contract
# ──────────────────────────────────────────────────────────────────────


def test_findings_carry_evidence_not_just_a_verdict() -> None:
    """A finding you cannot locate in the document is not actionable."""
    draft = _CLEAN_DRAFT + "\nSAP Concur signed in September.\n"
    rep = check_redaction(draft, client_name="SAP Concur")
    leak = next(f for f in rep.findings if f.kind == "client-name")
    assert "SAP Concur signed" in leak.evidence


def test_an_empty_draft_is_not_silently_clean() -> None:
    rep = check_redaction("", client_name="SAP Concur")
    assert not rep.clean


def test_report_is_json_serialisable() -> None:
    import json

    rep = check_redaction(_CLEAN_DRAFT, client_name="SAP Concur")
    assert json.loads(json.dumps(rep.to_dict()))["clean"] in (True, False)


def test_render_names_the_judgment_it_does_not_make() -> None:
    """The tool must never imply an anonymity it has not achieved."""
    out = render_report(check_redaction(_CLEAN_DRAFT, client_name="X"))
    assert "does NOT decide" in out
    assert "handful of companies" in out


def test_an_alias_inside_the_full_name_is_reported_once() -> None:
    """'Concur' begins INSIDE 'SAP Concur' — one leak, not two.

    Comparing start offsets alone let the alias re-report a position the
    full name had already claimed, doubling every finding in the list.
    """
    draft = "First Person is agency of record to SAP Concur for 2027.\n"
    rep = check_redaction(draft, client_name="SAP Concur",
                          client_aliases=("Concur",))
    name_findings = [f for f in rep.findings if f.kind == "client-name"]
    assert len(name_findings) == 1, [f.detail for f in name_findings]
    assert "SAP Concur" in name_findings[0].detail


def test_a_standalone_alias_is_still_caught_alongside_the_full_name() -> None:
    """Deduping must not suppress a genuinely separate occurrence."""
    draft = "We work with SAP Concur. Concur's buyers are finance leaders.\n"
    rep = check_redaction(draft, client_name="SAP Concur",
                          client_aliases=("Concur",))
    assert len([f for f in rep.findings if f.kind == "client-name"]) == 2
