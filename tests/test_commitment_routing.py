# ruff: noqa: E501 — corpus lines are real commitment text, kept whole.
"""Meeting-ingest commitments filed on the wrong workstream (#358).

Every Fathom action item from a meeting was written to the TAGGED
workstream. The #114 screen only fired on an explicit code, a whole project
name, or a company with exactly ONE active workstream — so a Salesloft
login, a Teleflex page fix and a Thoropass email all stayed where the
meeting happened to be tagged. These tests use the real mis-filings from
MC-2 (2026-10-21) across three tagged workstreams — ibx-5153, cnc-5237,
sap-5174 — plus negatives that mention another company in passing and must
NOT be flagged (heuristic extractors overfit one vocabulary; three projects
is the floor).

The corpus below is a trimmed copy of each workstream's real vocabulary:
its commitment rows and cp.md / meeting-history lines.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from cp_engine.commitments_sweep import SweepRow


def _ws(code, name, company, company_code, parent=None, label="job"):
    return SimpleNamespace(
        code=code,
        name=name,
        company_name=company,
        company_code=company_code,
        parent_code=parent,
        label=label,
    )


ROSTER = [
    _ws(
        "1pi-9005-mission-control",
        "1PI 9005 Mission Control",
        "First Person",
        "1PI",
        label="initiative",
    ),
    _ws(
        "1pi-9006-first-person-operations",
        "1PI 9006 First Person Operations",
        "First Person",
        "1PI",
        label="initiative",
    ),
    _ws("cnc-9004-storyos", "CNC 9004 StoryOS", "Canonic", "CNC", label="program"),
    _ws(
        "cnc-5237-soc-2",
        "CNC 5237 SOC 2",
        "Canonic",
        "CNC",
        parent="cnc-9004-storyos",
        label="initiative",
    ),
    _ws(
        "ggl-5136-go-safety-website",
        "GGL 5136 go/safety website",
        "Google",
        "GGL",
        parent="ggl-5216-google",
    ),
    _ws("ggl-5168-activation", "GGL 5168 Activation", "Google", "GGL", parent="ggl-5216-google"),
    _ws("ggl-5216-google", "GGL 5216 Google", "Google", "GGL", label="account"),
    _ws(
        "ibx-5153-ai-campaign",
        "IBX 5153 AI Campaign",
        "Infoblox",
        "IBX",
        parent="ibx-5217-infoblox",
    ),
    _ws("ibx-5217-infoblox", "IBX 5217 Infoblox", "Infoblox", "IBX", label="account"),
    _ws(
        "sap-5174-vision-update-2026",
        "SAP 5174 Vision Update 2026",
        "SAP Concur",
        "SAP",
        parent="sap-5218-sap-concur",
    ),
    _ws(
        "sap-5198-2027-ad-videos",
        "SAP 5198 2027 Ad Videos",
        "SAP Concur",
        "SAP",
        parent="sap-5218-sap-concur",
    ),
    _ws("sap-5218-sap-concur", "SAP 5218 SAP Concur", "SAP Concur", "SAP", label="account"),
    _ws(
        "slt-5196-brand-campaign-26",
        "SLT 5196 Brand Campaign 26",
        "Salesloft",
        "SLT",
        parent="slt-5219-salesloft",
    ),
    _ws("slt-5219-salesloft", "SLT 5219 Salesloft", "Salesloft", "SLT", label="account"),
    _ws(
        "tel-5113-2025-collateral",
        "TEL 5113 2025 Collateral",
        "teleflex",
        "TEL",
        parent="tel-5220-teleflex",
    ),
    _ws(
        "tel-5199-5093-collateral",
        "TEL 5199 5093 Collateral",
        "teleflex",
        "TEL",
        parent="tel-5220-teleflex",
    ),
    _ws("tel-5220-teleflex", "TEL 5220 teleflex", "teleflex", "TEL", label="account"),
]

CORPUS = {
    "cnc-5237-soc-2": [
        "Drew to obtain a second/alternate pen-test quote for comparison before fully committing.",
        "Drew to review Thoropass agreement with partners before signing; follow-up hold with Alex",
        "Alex Turner to send ISO 27001 pricing/scope detail for FirstPerson",
        "Alex Turner to send DocuSign agreement for Thoropass SOC 2 engagement ($15.8K all-in)",
        "Alex Turner to send the updated DocuSign agreement plus the Thoropass pen-test methodology deck Andrés walked through.",
        "Drew to confirm revised Thoropass agreement ($19,128 total, biannual) and sign the DocuSign",
        "Alex (with Joe, Thoropass owner) to recommend black box or gray box pen test for the API/MCP server",
        "Email Thoropass: accept $16k; request engagement letter; schedule pen test",
        "Email Drew pen test deck + sample report",
        "Report 'admin' tooltip bug in Thoropass MCP UI",
        "Send DocuSign agreement to Drew re: SOC 2 + black-box pen test; include 7% cap",
        "Consult Joe re: pen test scope; email Drew recommendation + gray-box pricing",
        "Vanta (Xander Torreso) to answer the remaining cost/scope questions on the Type II audit",
        "Thoropass (Cassin Sutor) argued that a new integration is not the evidence the auditors want",
        "Email Drew Vanta control ID list (Security/Availability/Confidentiality)",
        "Andrés Maroto walked through the Thoropass pen test sample report",
    ],
    # ILLUSTRATIVE for the same-company path: the live record (2026-10-21)
    # files Healy/Navon/Hexagon under cnc-5237 itself, so live scoring
    # cannot separate those rows — this is what the record would need.
    "cnc-9004-storyos": [
        "Schedule follow-up with Healy at Navon about the StoryOS pilot",
        "Healy (Navon) wants the Hexagon playbook before the pilot call",
        "Draft the Hexagon playbook section for the Canonic sales deck",
        "Navon pilot: Healy to confirm the security questionnaire owner",
        "Create FirstPerson account in Canonic dev (dev.canonic-os.com)",
        "Send Canonic site markdown + PNGs to Drew; Drew to build test site",
        "StoryOS narrator latency fix shipped to dev",
        "Hexagon playbook v2 sent to Healy",
        "Canonic website copy: publish the basic site this week",
        "StoryOS onboarding flow review with Tony",
    ],
    "ibx-5153-ai-campaign": [
        "Ask Janet to confirm whether the next working conversation can include Brad Rinklin and Jaime Mehra",
        "Email Janet re: Brad + Jaime on future calls; request 1-2 key messages per audience",
        "Request Hector move new platform diagram to FirstPerson Figma",
        "Draft messaging framework; then develop AI campaign concepts (preemptive/resilient/intelligent)",
        "Email Janet SOW for $49,500 for AI campaign reset",
        "Email Janet re: $50k SOW + brief table; request project code",
        "Download Magnifique R3 clips; edit 30s 'sharks' spot",
        "Text Janet re: Brad's Infoblox feedback; schedule quick call",
        "Review Infoblox AI story doc; update 'Your AI can't' with AI proof points",
        "Janet Noe wants Jaime in the room for the next creative review",
        "Hector Mendoza Suarez sends the platform diagram source",
        "Brad Rinklin on the 60% accuracy proof point",
        "Revise the manifesto per Janet",
        "Revise the AI story doc with proof points",
        "Revise Brad's quote card",
        "Revise SOW language for the reset",
        "Revise the 30s cut per feedback",
    ],
    "sap-5174-vision-update-2026": [
        "Fiona to share the L1/L2 Autonomous Spend deck so FP can build a visual",
        "Brief Jenny Chen on project to date",
        "Internal review meeting with Michelle + Fiona on V/M/P shortlist and Jenny deck",
        "Full client-team V/M/P review meeting (1 hr)",
        "Feedback on the ~7 V/M/P candidates (from Michelle Craig + Fiona Ashley)",
        "Fiona (and Michelle) to review the vision/mission/purpose package",
        "Send Fiona the trimmed walk-through deck plus 10 vision/mission/purpose candidates",
        "Review call with Jenny Chen (new SVP Concur Product Marketing)",
        "Check in with the SAP team (Fred, Dave, Ryan) on the Jenny-oriented document",
        "Feedback from the SAP Concur team on the vision/mission/purpose shortlist deck",
        "Email vision/mission/purpose deck + yardstick to Fiona, Michelle, Ryan, Fred",
        "Cleaned, packaged six-vision placemat for Monday team review",
        "Update vision shortlist (money/spend); send to Craig/Fiona by Oct 9",
        "Send Jenny slides via Dropbox to Fiona; Fiona upload to SharePoint",
    ],
    "slt-5196-brand-campaign-26": [
        "Email Morgan: request SalesLoft interviewee background/pre-recordings",
        "Write Latoya's interview answers; brief her on set",
        "Formalize message mapping; research SalesLoft interviewees; compile Sean brief",
        "Email Leah: request any further brief feedback",
        "Storyboard the 60s hero for the Salesloft brand campaign",
        "Morgan to confirm the interviewee lineup for the shoot",
        "Latoya and Sean interviews scheduled for the Atlanta shoot",
        "Salesloft portal access for the brand team",
    ],
    "tel-5113-2025-collateral": [
        "The two new ads are being socialized with the Euralift commercialization team",
        "The new product is officially named Euralift Elevate; trademark research is underway",
        "Marcello: inform Jason of the Euralift Elevate name",
        "Durability ads are a key initiative to boost Euralift numbers",
        "Present US bold-messaging research to OUS faculty (Euralift-focused)",
        "Updated durability ads went back to the client after Jeff worked over the weekend",
        "Fix the Vimeo embed on the Euralift landing page",
    ],
    "tel-5199-5093-collateral": [
        "Pricing a one-year extension of the Teleflex DTC commercial (talent version)",
        "Talent usage renewal for the DTC spot beyond May 2027",
    ],
    "ggl-5136-go-safety-website": [
        "go/safety website: migrate the incident pages to the new template",
        "Kyle to build the event-safety Google Sites page",
    ],
}


@pytest.fixture(scope="module")
def index():
    from cp_engine.commitment_routing import RoutingIndex

    idx = RoutingIndex(ROSTER)
    for code, lines in CORPUS.items():
        for line in lines:
            idx.add_document(code, line)
    return idx


# ── CONTROL: the real mis-filings, three tagged workstreams ─────────────────


@pytest.mark.parametrize(
    "tagged, text, want",
    [
        # ibx-5153 holds a Teleflex page fix and a Salesloft login.
        (
            "ibx-5153-ai-campaign",
            "Fix Euralift page: repair Vimeo video; verify/restore missing content",
            "tel-5113-2025-collateral",
        ),
        (
            "ibx-5153",
            "Log into SalesLoft portal [owner unresolved: Speaker 1]",
            "slt-5196-brand-campaign-26",
        ),
        # sap-5174 holds a Thoropass email.
        (
            "sap-5174-vision-update-2026",
            "Email Andres (Thoropass) re: black-box + gray-box on-demand; request cost impact",
            "cnc-5237-soc-2",
        ),
        # cnc-5237 (SOC 2) holds StoryOS work — same company, other workstream.
        (
            "cnc-5237-soc-2",
            "Schedule mtg w/ Healy (Navon) [owner unresolved: Speaker 2]",
            "cnc-9004-storyos",
        ),
    ],
)
def test_real_misfilings_are_flagged(index, tagged, text, want) -> None:
    got = index.suggest(text, tagged)
    assert got is not None, f"no flag for {text!r} on {tagged}"
    assert got.target_code == want


# ── NEGATIVES: another company named in passing, item belongs here ─────────


@pytest.mark.parametrize(
    "tagged, text",
    [
        # Salesloft interviewee work on the Salesloft job.
        (
            "slt-5196-brand-campaign-26",
            "Email Morgan: request SalesLoft interviewee background; confirm interview lead",
        ),
        # Infoblox mentioned as a reference on a Salesloft item.
        (
            "slt-5196-brand-campaign-26",
            "Pull the Infoblox reel as a reference for Latoya's interview prep",
        ),
        # A Canonic demo FOR the SAP client — SAP's people, SAP's work.
        (
            "sap-5174-vision-update-2026",
            "Schedule Canonic demo w/ Craig & Fiona for week of Oct 19",
        ),
        (
            "sap-5174-vision-update-2026",
            "Build ideal-scenario timeline for Fiona & Michelle; include 3-mo Canonic trial",
        ),
        # Plain on-project items.
        ("ibx-5153-ai-campaign", "Email Janet SOW for $49,500 for AI campaign reset"),
        ("cnc-5237-soc-2", "Email Thoropass: accept $16k; request engagement letter"),
        # A generic word that names another workstream ("website") is not
        # evidence of another company.
        ("cnc-5237-soc-2", "Update the website security page with the SOC 2 badge"),
    ],
)
def test_passing_mentions_are_not_flagged(index, tagged, text) -> None:
    assert index.suggest(text, tagged) is None


def test_a_habit_word_is_not_a_name(index) -> None:
    """Live 2026-10-21: "Revise deck/messaging per AJ" on StoryOS scored as
    Infoblox's because ibx-5153 says "revise" a lot. Only a proper noun (a
    vendor, a product, a person) is strong enough to move a row."""
    assert (
        index.suggest(
            "Revise deck/messaging per AJ: add logo wall, emphasize speed",
            "cnc-9004-storyos",
        )
        is None
    )


def test_an_explicit_code_is_decisive(index) -> None:
    got = index.suggest("Send the ggl-5168 playbook to Kelly", "ibx-5153-ai-campaign")
    assert got is not None and got.target_code == "ggl-5168-activation"


def test_co_tagged_workstreams_are_never_foreign(index) -> None:
    got = index.suggest(
        "Fix Euralift page: repair Vimeo video",
        ["ibx-5153", "tel-5113-2025-collateral"],
    )
    assert got is None


def test_ambiguous_foreign_company_falls_back_to_its_account(index) -> None:
    # Teleflex is clearly named, but nothing picks one of its two jobs.
    got = index.suggest("Send Teleflex the revised invoice", "ibx-5153-ai-campaign")
    assert got is not None and got.target_code == "tel-5220-teleflex"


def test_backfill_discounts_the_rows_own_contribution() -> None:
    """A mis-filed row is in the corpus under the WRONG workstream; scoring
    it must not let its own words vote for where it already sits."""
    from cp_engine.commitment_routing import RoutingIndex

    idx = RoutingIndex(ROSTER)
    for code, lines in CORPUS.items():
        for line in lines:
            idx.add_document(code, line)
    row = "Email Andres (Thoropass) re: black-box + gray-box on-demand"
    idx.add_document("sap-5174-vision-update-2026", row)
    got = idx.suggest(row, "sap-5174-vision-update-2026", in_corpus=True)
    assert got is not None and got.target_code == "cnc-5237-soc-2"


# ── the stored flag: one marker, two spellings, self-flags ignored ──────────


def test_flag_target_reads_both_marker_spellings() -> None:
    from cp_engine.commitment_routing import flag_target, strip_flags

    assert flag_target("Do X [off-project? → storyos]") == "storyos"
    assert flag_target("Do X [cross-project? → ggl-5216-google]") == "ggl-5216-google"
    assert flag_target("Do X") is None
    assert strip_flags("Do X [off-project? → storyos] [owner unresolved: S1]") == (
        "Do X [owner unresolved: S1]"
    )


# ── commitments_sweep surfaces flagged rows with a route_commitment call ────

_TODAY = date(2026, 10, 21)


def _sr(cid, desc):
    return SweepRow(
        id=cid,
        description=desc,
        owner="Drew",
        source_kind="meeting_ingest",
        due_date=None,
        date_status="proposed",
        age_days=2,
        ttl=None,
    )


def test_sweep_suggests_route_commitment_for_flagged_rows() -> None:
    from cp_engine.commitments_sweep import suggested_routes

    groups = {
        "IBX-ai-campaign": [
            _sr("r1", "Log into SalesLoft portal [off-project? → slt-5196-brand-campaign-26]"),
            _sr("r2", "Email Janet SOW for $49,500"),
            # A stale self-flag (the short-form tag bug) proposes nothing.
            _sr("r3", "Collect Infoblox feedback [off-project? → ibx-5153-ai-campaign]"),
        ],
        "SAP-vision-update-2026": [
            _sr("r4", "Email Andres [cross-project? → cnc-5237-soc-2]"),
        ],
    }
    # The group key is projects.code; the self-flag check needs the slug.
    out = suggested_routes(
        groups,
        canonical={
            "IBX-ai-campaign": "ibx-5153-ai-campaign",
            "SAP-vision-update-2026": "sap-5174-vision-update-2026",
        },
    )
    assert out == [
        {
            "project_code": "IBX-ai-campaign",
            "key": "r1",
            "target_code": "slt-5196-brand-campaign-26",
        },
        {"project_code": "SAP-vision-update-2026", "key": "r4", "target_code": "cnc-5237-soc-2"},
    ]


def test_route_payload_binds_to_the_hosted_verb_signature() -> None:
    """The payload is sent as `route_commitment(**entry)` — its keys must be
    exactly that verb's parameters."""
    import ast
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent / "prototypes" / "hosted-mcp" / "server.py"
    ).read_text()
    fn = next(
        n
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "route_commitment"
    )
    params = {a.arg for a in fn.args.args}
    from cp_engine.commitments_sweep import suggested_routes

    (entry,) = suggested_routes({"p": [_sr("x", "Do Y [off-project? → storyos]")]})
    assert set(entry) == params


def test_render_prints_the_route_line() -> None:
    from cp_engine.commitments_sweep import render_sweep

    text = render_sweep(
        {
            "IBX-ai-campaign": [
                _sr(
                    "11111111-aaaa",
                    "Log into SalesLoft portal [off-project? → slt-5196-brand-campaign-26]",
                ),
            ]
        },
        today=_TODAY,
    )
    assert (
        '    route_commitment(project_code="IBX-ai-campaign", key="11111111-aaaa", '
        'target_code="slt-5196-brand-campaign-26")'
    ) in text


# ── the #114 screen: the short-form tag no longer flags its own project ─────


def test_screen_does_not_flag_the_tagged_project_by_its_long_code() -> None:
    import sys
    from pathlib import Path

    wh = str(Path(__file__).resolve().parent.parent / "webhook")
    if wh not in sys.path:
        sys.path.insert(0, wh)
    import commitments_propose as cprop

    roster = [r for r in ROSTER if r.code != "ibx-5217-infoblox"]
    got = cprop.screen_off_project(
        "Collect Infoblox feedback on 1-pager by Sep 21 EOD; compile",
        ingest_codes=["ibx-5153"],
        roster=roster,
    )
    assert got is None


# ── ingest end to end: the scored flag lands on the row, hash stays clean ───


def test_propose_commitments_flags_a_scored_misfiling(monkeypatch, index) -> None:
    import sys
    from pathlib import Path

    wh = str(Path(__file__).resolve().parent.parent / "webhook")
    if wh not in sys.path:
        sys.path.insert(0, wh)
    import commitments_propose as cprop

    from cp_engine.asks import ask_hash

    meeting = {
        "id": "m-1",
        "participants": [{"name": "Drew Fiero", "email": "drew@firstperson.is"}],
        "action_items": [
            {
                "description": "Fix Euralift page: repair Vimeo video",
                "assignee": {"name": "Drew Fiero"},
            },
            {
                "description": "Email Janet SOW for $49,500 for AI campaign reset",
                "assignee": {"name": "Drew Fiero"},
            },
        ],
    }

    class _Q:
        def __getattr__(self, _n):
            return lambda *a, **k: self

        def execute(self):
            return SimpleNamespace(data=meeting)

    monkeypatch.setattr(
        cprop.mc2_db, "get_client", lambda required=False: SimpleNamespace(table=lambda n: _Q())
    )
    monkeypatch.setattr(
        cprop,
        "resolve_commitment_owner",
        lambda client, code: {
            "id": "p",
            "code": code,
            "kind": "project",
            "canonical_code": "ibx-5153-ai-campaign",
        },
    )
    written: list[dict] = []
    monkeypatch.setattr(
        cprop, "write_commitment", lambda client, **kw: written.append(kw) or "inserted"
    )

    summary = cprop.propose_commitments("m-1", ["ibx-5153"], roster=ROSTER, routing_index=index)
    assert summary["scored_flagged"] == 1 and summary["off_project_flagged"] == 1
    fix, sow = written
    assert fix["description"] == (
        "Fix Euralift page: repair Vimeo video [off-project? → tel-5113-2025-collateral]"
    )
    assert fix["cp_hash"] == ask_hash(
        "ibx-5153-ai-campaign", "Fix Euralift page: repair Vimeo video"
    )
    assert sow["description"] == "Email Janet SOW for $49,500 for AI campaign reset"
