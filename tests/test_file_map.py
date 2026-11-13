"""The one map of where project files are kept (`cp_engine.file_map`).

WHAT BROKE. On 2026-11-13 a session pushed an invoice to Dropbox
`03 Assets/06 Spine` when it was already filed on Drive under
`Management/02 Contracts and Financials/05 Invoices PDFs`: cp knew two folder
names and nothing about the rest of the template. Drew: "I think we should
teach cp where all of the files are kept between dropbox and drive".

WHAT THESE PIN.
1. The map is the one Drew approved, entry for entry.
2. A lookup finds a kind by key, alias or a distinctive word, returns the
   candidates when several match, and never picks one for the caller.
3. The tenant `CLAUDE.md` section is rendered from the map and carries the
   client-inputs rule.
4. `folder_ingest_gap` answers "does ingest read this folder?" by the same
   rules as `ingest_reach_gap`.
"""
from __future__ import annotations

import pytest

from cp_engine import file_map
from cp_engine.asset_ingest import ProjectFolders, folder_ingest_gap, ingest_reach_gap

# Drew's confirmed map, 2026-11-13. Typed out here on purpose: the test is
# the second copy that a wrong edit to the module has to disagree with.
APPROVED = {
    "invoices": ("drive", "Management/02 Contracts and Financials/05 Invoices PDFs"),
    "sows": ("drive", "Management/02 Contracts and Financials/03 SOWs"),
    "estimates": ("drive", "Management/02 Contracts and Financials/02 Estimate PDFs"),
    "purchase-orders": ("drive", "Management/02 Contracts and Financials/04 Purchase Orders"),
    "engagement-summaries": (
        "drive", "Management/02 Contracts and Financials/01 Engagement Summary"),
    "vendor-invoices": ("drive", "Management/01 1P Expenses/Vendor Invoices"),
    "vendor-contracts": ("drive", "Management/01 1P Expenses/Vendor Contracts and Estimates"),
    "receipts": ("drive", "Management/01 1P Expenses/Credit Card Receipts"),
    "live-action": ("drive", "Management/03 Live Action and Talent/Live Action"),
    "talent": ("drive", "Management/03 Live Action and Talent/Talent"),
    "wrap": ("drive", "Management/04 Wrap"),
    "research": ("drive", "Define and Approach/01 Research"),
    "trackers": ("drive", "Define and Approach/02 Trackers and Calcs"),
    "narrative": ("drive", "Define and Approach/03 Narrative"),
    "client-inputs-google": ("drive", "Define and Approach/04 Client Inputs"),
    "client-share": ("drive", "Define and Approach/05 Client Share"),
    "client-inputs": ("dropbox", "03 Assets/01 Client Assets"),
    "audio": ("dropbox", "03 Assets/02 Audio"),
    "renders": ("dropbox", "03 Assets/03 Renders"),
    "sources-and-stock": ("dropbox", "03 Assets/04 Sources and Stock"),
    "working-files": ("dropbox", "02 Working Files"),
    "deliverables": ("dropbox", "04 Deliverables"),
    "project-notes": ("dropbox", "01 Project Notes"),
    "spine": ("dropbox", "03 Assets/06 Spine"),
}


def test_the_map_is_the_one_drew_approved():
    assert {k.key: (k.store, k.path) for k in file_map.FILE_KINDS} == APPROVED


def test_keys_are_unique_and_every_kind_has_a_plain_label():
    keys = [k.key for k in file_map.FILE_KINDS]
    assert len(keys) == len(set(keys))
    assert all(k.label.strip() for k in file_map.FILE_KINDS)


def test_a_kind_cannot_be_edited_in_place():
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        file_map.get("invoices").path = "somewhere else"  # type: ignore[misc]


# ── lookup ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("asked, key", [
    ("invoices", "invoices"),                 # exact key
    ("Purchase_Orders ", "purchase-orders"),  # key, however it is typed
    ("client inputs", "client-inputs"),       # the key with a space
    ("PO", "purchase-orders"),                # alias
    ("statement of work", "sows"),            # alias
    ("wrap report", "wrap"),                  # alias
    ("interviews", "research"),               # a distinctive word of the label
    ("receip", "receipts"),                   # the start of a word
])
def test_lookup_finds_one_kind(asked, key):
    found = file_map.find(asked)
    assert found.kind is not None and found.kind.key == key
    assert found.candidates == ()


def test_an_exact_key_wins_over_a_longer_key_that_contains_it():
    """`invoices` is the client's invoices, not a choice between those and
    `vendor-invoices`."""
    assert file_map.find("invoices").kind.key == "invoices"
    assert file_map.find("client-inputs").kind.key == "client-inputs"


def test_an_ambiguous_word_returns_the_candidates_and_picks_none():
    found = file_map.find("vendor")
    assert found.kind is None
    assert [k.key for k in found.candidates] == ["vendor-invoices", "vendor-contracts"]
    footage = file_map.find("footage")
    assert footage.kind is None
    assert {k.key for k in footage.candidates} == {"client-inputs", "sources-and-stock"}


@pytest.mark.parametrize("asked", ["zzz", "", "   ", "timesheets"])
def test_an_unknown_word_finds_nothing(asked):
    found = file_map.find(asked)
    assert found.kind is None and found.candidates == ()


def test_get_is_for_code_and_refuses_anything_but_a_key():
    assert file_map.get("spine").path == "03 Assets/06 Spine"
    with pytest.raises(KeyError):
        file_map.get("invoice")


# ── the tenant CLAUDE.md section ──────────────────────────────────────


def test_the_markdown_lists_every_kind_with_its_folder():
    md = file_map.render_markdown()
    for key, (_store, path) in APPROVED.items():
        assert f"`{key}`" in md and f"`{path}`" in md
    assert md.index("**Google Drive**") < md.index("**Dropbox**")
    # Each kind sits under its own store's heading.
    drive_half = md.split("**Dropbox**")[0]
    assert "`invoices`" in drive_half and "`deliverables`" not in drive_half


def test_the_markdown_carries_the_client_inputs_rule():
    md = file_map.render_markdown()
    assert file_map.CLIENT_INPUTS_RULE in md
    rule = file_map.CLIENT_INPUTS_RULE
    assert "Dropbox" in rule and "Google-native" in rule
    assert "`03 Assets/01 Client Assets`" in rule
    assert "`Define and Approach/04 Client Inputs`" in rule


def test_the_tenant_claude_md_has_the_section_rendered_from_the_map():
    from cp_engine.render import render_claude_md
    from tests.test_render import make_tenant

    out = render_claude_md(make_tenant(name="1p"))
    section = out.split("## Where files live", 1)[1].split("\n## ", 1)[0]
    assert file_map.render_markdown() in section
    assert "where_is" in section
    assert "upload" in section and "missing" in section


def test_the_template_holds_no_hand_typed_copy_of_a_folder():
    from pathlib import Path

    import cp_engine

    template = (Path(cp_engine.__file__).parent / "templates" / "CLAUDE.md.j2").read_text()
    for _store, path in APPROVED.values():
        assert path not in template


# ── does ingest read this folder? ─────────────────────────────────────

ROOT = "/1P Active Projects/SLT 5196 Brand Campaign 26"


def _folders(**kw):
    base = dict(project_id="p1", company_id="co1", company_kind="client",
                google_drive_folder_id="drive-root", mc_dropbox_folder_id=ROOT,
                enable_google_drive=True, enable_dropbox=True)
    base.update(kw)
    return ProjectFolders(**base)


def test_with_no_allowlist_ingest_reads_every_folder_of_an_enabled_store():
    f = _folders()
    assert folder_ingest_gap(f, "dropbox", dropbox_path=f"{ROOT}/04 Deliverables") is None
    assert folder_ingest_gap(f, "drive", drive_segments=("Management", "04 Wrap")) is None


def test_an_allowlist_names_the_folders_ingest_reads():
    f = _folders(asset_ingest_folders=("Client Assets", "Spine", "Research"))
    assert folder_ingest_gap(
        f, "dropbox", dropbox_path=f"{ROOT}/03 Assets/01 Client Assets") is None
    gap = folder_ingest_gap(f, "dropbox", dropbox_path=f"{ROOT}/04 Deliverables")
    assert "not one of the project's ingest folders" in gap and "Client Assets" in gap
    assert folder_ingest_gap(
        f, "drive", drive_segments=("Define and Approach", "01 Research")) is None
    assert "ingest folders" in folder_ingest_gap(
        f, "drive", drive_segments=("Management", "04 Wrap"))


def test_a_store_that_is_off_or_unbound_is_not_read():
    off = _folders(enable_google_drive=False)
    assert "Google Drive source is not enabled" in folder_ingest_gap(
        off, "drive", drive_segments=("Management",))
    unbound = _folders(mc_dropbox_folder_id=None)
    assert "no Dropbox folder" in folder_ingest_gap(
        unbound, "dropbox", dropbox_path="/x/04 Deliverables")
    assert "no MC-2 folder configuration" in folder_ingest_gap(None, "dropbox", dropbox_path="/x")


def test_a_child_workstreams_folder_is_left_to_the_child():
    f = _folders(child_dropbox_roots=(f"{ROOT}/SLT 5200 Child",),
                 child_drive_folders=("SLT 5200 Child",))
    assert "child workstream" in folder_ingest_gap(
        f, "dropbox", dropbox_path=f"{ROOT}/SLT 5200 Child/04 Deliverables")
    assert "child workstream" in folder_ingest_gap(
        f, "drive", drive_segments=("SLT 5200 Child", "Management"))


@pytest.mark.parametrize("kw", [
    {}, {"enable_dropbox": False}, {"mc_dropbox_folder_id": None},
    {"asset_ingest_folders": ("Client Assets",)},
    {"asset_ingest_folders": ("Deliverables",)},
    {"company_kind": "self-fpsf", "has_agreement": True},
    {"child_dropbox_roots": (f"{ROOT}/04 Deliverables",)},
])
def test_the_folder_answer_agrees_with_the_file_answer(kw):
    """A folder is read exactly when a file dropped in it would be: the two
    functions apply the same rules, and this is what stops them drifting."""
    f = _folders(**kw)
    folder = f"{ROOT}/04 Deliverables"
    by_folder = folder_ingest_gap(f, "dropbox", dropbox_path=folder)
    by_file = ingest_reach_gap(f, f"{folder}/deck.pdf")
    assert (by_folder is None) == (by_file is None)
