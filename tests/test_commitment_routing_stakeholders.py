# ruff: noqa: E501 — corpus lines are commitment-shaped text, kept whole.
"""The tagged workstream's own named stakeholders weigh against a reroute (#414).

THE REPORT (2026-11-03). An ibx-5153 commitment, "Finalize and email
Janet/Art/Leah/Jaime recap re: decisions, guardrails, Nov 12/16", was
flagged `[off-project? → slt-5196]`. Janet and Jaime are Infoblox's
stakeholders; Art and Leah have Salesloft cards. The #358 scoring counted
Art and Leah as Salesloft markers and gave Janet and Jaime no more weight
than any recurring word in Infoblox's record — which, with Janet's lines
also turning up in the tenant-wide sprint-planning record, was little.
Accepting the suggested route would have sent an Infoblox recap to another
client.

The fix: a name on one of the TAGGED company's stakeholder cards (a) counts
for the tagged company and never for another one, and (b) when the item
names one, another company must win on more than names — its own name, two
separate markers, or more of its own stakeholders than the item names of
the tagged company's.

Heuristics overfit one vocabulary, so the roster spans three client
companies with real-shaped people: Infoblox (Janet Noe, Jaime Mehra, Brad
Rinklin), Salesloft (Art Kilinski, Leah Ward, Morgan Wright, Latoya) and
SAP Concur (Fiona Ashley, Michelle Craig, Jenny Chen) — plus a SOC 2
vendor and a Teleflex product, so the non-name true positives stay covered.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


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
    _ws("1pi-9006-first-person-operations", "1PI 9006 First Person Operations", "First Person", "1PI", label="initiative"),
    _ws("cnc-5237-soc-2", "CNC 5237 SOC 2", "Canonic", "CNC", label="initiative"),
    _ws("ibx-5153-ai-campaign", "IBX 5153 AI Campaign", "Infoblox", "IBX", parent="ibx-5217-infoblox"),
    _ws("ibx-5217-infoblox", "IBX 5217 Infoblox", "Infoblox", "IBX", label="account"),
    _ws("sap-5174-vision-update-2026", "SAP 5174 Vision Update 2026", "SAP Concur", "SAP", parent="sap-5218-sap-concur"),
    _ws("sap-5218-sap-concur", "SAP 5218 SAP Concur", "SAP Concur", "SAP", label="account"),
    _ws("slt-5196-brand-campaign-26", "SLT 5196 Brand Campaign 26", "Salesloft", "SLT", parent="slt-5219-salesloft"),
    _ws("slt-5219-salesloft", "SLT 5219 Salesloft", "Salesloft", "SLT", label="account"),
    _ws("tel-5113-2025-collateral", "TEL 5113 2025 Collateral", "teleflex", "TEL", parent="tel-5220-teleflex"),
    _ws("tel-5220-teleflex", "TEL 5220 teleflex", "teleflex", "TEL", label="account"),
]

# Each workstream's record. Infoblox's people also recur in the tenant-wide
# operations record (sprint planning names every client), so they are weak
# markers there — the live shape that let two Salesloft names win.
CORPUS = {
    "ibx-5153-ai-campaign": [
        "Email Janet SOW for $49,500 for AI campaign reset",
        "Janet Noe wants Jaime in the room for the next creative review",
        "Revise the manifesto per Janet",
        "Revise the AI story doc with proof points from Brad Rinklin",
        "Draft messaging framework; then develop AI campaign concepts",
        "Download Magnifique R3 clips; edit 30s sharks spot",
    ],
    "1pi-9006-first-person-operations": [
        "Sprint planning: Janet still owes sign-off on the Infoblox reset scope",
        "Sprint planning: Jaime asked to see the pillars simplified to three",
        "Sprint planning: Janet and Jaime review the concepts on Thursday",
        "Sprint planning: the SAP review moved to the 19th",
        "Ops: renew the Dropbox team plan before the invoice date",
    ],
    "slt-5196-brand-campaign-26": [
        "Email Morgan: request SalesLoft interviewee background/pre-recordings",
        "Write Latoya's interview answers; brief her on set",
        "Email Leah: request any further brief feedback",
        "Leah approved the master ad template across sizes",
        "Send Leah the photo selects for the per-person picks",
        "Leah to confirm the landing page copy with Art",
        "Art to approve the QR codes on the event signage",
        "Art Kilinski shared the brand guidelines refresh",
        "Follow up with Art regarding final approval on the QR codes",
        "Propose the image generation service to Art and Leah once photo selections are made",
        "Morgan to confirm the interviewee lineup for the shoot",
    ],
    "sap-5174-vision-update-2026": [
        "Fiona to share the L1/L2 Autonomous Spend deck so FP can build a visual",
        "Internal review meeting with Michelle + Fiona on V/M/P shortlist and Jenny deck",
        "Feedback on the V/M/P candidates from Michelle Craig + Fiona Ashley",
        "Send Fiona the trimmed walk-through deck plus 10 vision/mission/purpose candidates",
        "Review call with Jenny Chen (new SVP Concur Product Marketing)",
        "Update vision shortlist (money/spend); send to Craig/Fiona by Oct 9",
        "Michelle to circulate the shortlist to the Concur leadership team",
    ],
    "cnc-5237-soc-2": [
        "Alex Turner to send DocuSign agreement for Thoropass SOC 2 engagement",
        "Email Thoropass: accept $16k; request engagement letter; schedule pen test",
        "Andres Maroto walked through the Thoropass pen test sample report",
        "Report admin tooltip bug in Thoropass MCP UI",
    ],
    "tel-5113-2025-collateral": [
        "The new product is officially named Euralift Elevate; trademark research is underway",
        "Durability ads are a key initiative to boost Euralift numbers",
        "Fix the Vimeo embed on the Euralift landing page",
        "Present bold-messaging research to OUS faculty (Euralift-focused)",
        "Euralift Elevate launch ads go to the commercialization team Friday",
    ],
}

# Stakeholder cards, as the generated view renders them: (company dir,
# workstream dir or None for account scope, name, side, aliases).
CARDS = [
    ("infoblox", None, "Janet Noe", "client", "Janet"),
    ("infoblox", None, "Jaime Mehra", "client", ""),
    ("infoblox", None, "Brad Rinklin", "client", ""),
    ("salesloft", None, "Art Kilinski", "client", ""),
    ("salesloft", None, "Leah Ward", "client", ""),
    ("salesloft", "slt-5196-brand-campaign-26", "Morgan Wright", "client", ""),
    ("salesloft", "slt-5196-brand-campaign-26", "Latoya Fontaine", "client", ""),
    ("sap", None, "Fiona Ashley", "client", ""),
    ("sap", None, "Michelle Craig", "client", ""),
    ("sap", "sap-5174-vision-update-2026", "Jenny Chen", "client", ""),
    # Internal people are on every workstream; they name none of them.
    ("infoblox", None, "Drew Fiero", "internal", ""),
]

_DIRS = {
    "ibx-5153-ai-campaign": "1p/infoblox/ibx-5153-ai-campaign",
    "ibx-5217-infoblox": "1p/infoblox",
    "slt-5196-brand-campaign-26": "1p/salesloft/slt-5196-brand-campaign-26",
    "slt-5219-salesloft": "1p/salesloft",
    "sap-5174-vision-update-2026": "1p/sap/sap-5174-vision-update-2026",
    "sap-5218-sap-concur": "1p/sap",
    "tel-5113-2025-collateral": "1p/teleflex/tel-5113-2025-collateral",
    "tel-5220-teleflex": "1p/teleflex",
    "cnc-5237-soc-2": "canonic/cnc-5237-soc-2",
    "1pi-9006-first-person-operations": "1p/first-person/1pi-9006-first-person-operations",
}


def _card_file(name: str, side: str, aliases: str) -> str:
    slug = name.lower().replace(" ", "-")
    return (
        "---\n"
        f"est_item_id: _authored/{slug}\n"
        "layer: Stakeholders\n"
        "---\n"
        "## v1 — 2026-10-01 · live\n"
        f"framing: {name} — stakeholder\n"
        "\n"
        "<!-- cp:stakeholder -->\n"
        f"- **Name:** {name}\n"
        f"- **Side:** {side}\n"
        + (f"- **Aliases:** {aliases}\n" if aliases else "")
        + "<!-- /cp:stakeholder -->\n"
        f"\n# {name}\n"
    )


def _tenant(root: Path) -> Path:
    import json

    entries = {}
    for code, rel in _DIRS.items():
        d = root / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "cp.md").write_text(f"# {code}\n", encoding="utf-8")
        entries[code] = {"path": rel}
    (root / ".cp-engine").mkdir(exist_ok=True)
    (root / ".cp-engine" / "paths.json").write_text(
        json.dumps({"version": 1, "workstreams": entries}), encoding="utf-8"
    )
    company_dir = {"infoblox": "1p/infoblox", "salesloft": "1p/salesloft", "sap": "1p/sap"}
    for co, ws, name, side, aliases in CARDS:
        base = root / company_dir[co]
        folder = base / "_stakeholders" if ws is None else base / ws / "spine" / "_authored"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{name.lower().replace(' ', '-')}.md").write_text(
            _card_file(name, side, aliases), encoding="utf-8"
        )
    return root


@pytest.fixture(scope="module")
def index(tmp_path_factory):
    from cp_engine.commitment_routing import build_routing_index

    root = _tenant(tmp_path_factory.mktemp("tenant"))
    idx = build_routing_index(ROSTER, tenant_root=root)
    for code, lines in CORPUS.items():
        for line in lines:
            idx.add_document(code, line)
    return idx


# ── THE REPORT: the tagged company's own names keep the row ────────────────


def test_the_ibx_5153_recap_stays_on_infoblox(index) -> None:
    """The live row. Janet and Jaime are Infoblox's; two Salesloft names do
    not outvote them."""
    text = (
        "Finalize and email Janet/Art/Leah/Jaime recap re: decisions, "
        "guardrails, Nov 12/16; request per-person photo picks"
    )
    assert index.suggest(text, "ibx-5153-ai-campaign") is None
    assert index.suggest(text, "ibx-5153") is None


def test_the_stakeholder_cards_are_read_from_the_tree(index) -> None:
    """The cards on disk reach the index — internal people excluded."""
    from cp_engine.commitment_routing import RoutingIndex

    assert isinstance(index, RoutingIndex)
    assert {"janet", "jaime"} <= index.people_of("ibx-5153-ai-campaign")
    assert {"art", "leah", "morgan"} <= index.people_of("slt-5196-brand-campaign-26")
    assert "drew" not in index.people_of("ibx-5153-ai-campaign")


@pytest.mark.parametrize(
    "tagged, text",
    [
        # One other-company name beside one of the tagged company's people
        # (the live row's shape: a mis-heard or passing name) must not flip it.
        ("ibx-5153-ai-campaign", "Email Janet the guardrails recap; cc Art"),
        # The same shape on the other two companies.
        ("slt-5196-brand-campaign-26", "Send Leah and Morgan the shoot recap; loop in Janet"),
        ("sap-5174-vision-update-2026", "Send Fiona & Michelle the vision recap; ask Leah for the template"),
        ("sap-5174-vision-update-2026", "Review Jenny deck notes with Fiona; Art to share brand guidelines"),
    ],
)
def test_own_names_outweigh_foreign_names(index, tagged, text) -> None:
    assert index.suggest(text, tagged) is None


# ── TRUE POSITIVES: genuinely off-project rows still flag ──────────────────


@pytest.mark.parametrize(
    "tagged, text, want",
    [
        # Only Salesloft's people — nobody of Infoblox's.
        ("ibx-5153-ai-campaign", "Follow up with Art and Leah on the QR code approval", "slt-5196-brand-campaign-26"),
        # The other company named outright, even with an own name on the row.
        ("ibx-5153-ai-campaign", "Email Leah the Salesloft photo picks; cc Janet", "slt-5196-brand-campaign-26"),
        # More of the other company's people than of the tagged one's.
        ("sap-5174-vision-update-2026", "Email Leah, Art and Morgan the interviewee lineup; cc Fiona", "slt-5196-brand-campaign-26"),
        # A vendor and a product, no names at all (#358's cases).
        ("sap-5174-vision-update-2026", "Email Andres (Thoropass) re: pen test cost impact", "cnc-5237-soc-2"),
        ("ibx-5153-ai-campaign", "Fix Euralift page: repair Vimeo video", "tel-5113-2025-collateral"),
        # SAP's people on a Salesloft row.
        ("slt-5196-brand-campaign-26", "Send Fiona and Michelle the vision shortlist", "sap-5174-vision-update-2026"),
    ],
)
def test_genuinely_off_project_rows_still_flag(index, tagged, text, want) -> None:
    got = index.suggest(text, tagged)
    assert got is not None, f"no flag for {text!r} on {tagged}"
    assert got.target_code == want


def test_a_card_read_failure_leaves_scoring_as_before(tmp_path, monkeypatch) -> None:
    """People are additive: a failed card read must not cost the index."""
    from cp_engine import stakeholders
    from cp_engine.commitment_routing import build_routing_index

    def boom(*a, **k):
        raise RuntimeError("cards unavailable")

    monkeypatch.setattr(stakeholders, "cards_from_view", boom)
    idx = build_routing_index(ROSTER, tenant_root=_tenant(tmp_path))
    assert idx is not None
    assert idx.people_of("ibx-5153-ai-campaign") == frozenset()
