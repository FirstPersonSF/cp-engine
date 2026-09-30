"""Source-store visibility gaps (cp-engine #324).

Four ways a document EXISTS and a read still cannot see or trust it:

  (a) a company-level doc filed under the account node was ingested
      `scope='project'`, so no sibling workstream's scoped read reached it;
  (b) a CONFIDENTIAL / under-NDA / EMBARGOED marking in the body never set
      `status_note`, so it read as an ordinary source;
  (c) a derived doc's `Source reviewed:` naming a file that was never
      ingested went unflagged (ibx-5153, 2026-08-28 → 09-11);
  (d) a zero-chunk source answered "no source named …" — indistinguishable
      from a title that was never in the store.

The fake (tests/_source_store_fake.py) is a small in-memory PostgREST: it FILTERS (eq / in_ / is_ /
range), so a query that asks the wrong question gets the wrong answer here
too, rather than whatever canned rows a permissive fake hands back.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from cp_engine import mc2_db
from cp_engine.asset_ingest import (
    FileRef,
    ProjectFolders,
    _PROJECT_COLUMNS,
    _flag_confidentiality,
    _row_to_folders,
    _stamp_scope,
)
from cp_engine.project_sources import (
    fetch_source,
    ingested_source_titles,
    list_sources,
    pull_source,
)
from cp_engine.source_markers import detect_markings, status_note_for
from cp_engine.spine import workstream_docs
from cp_engine.spine_lint import (
    extract_source_reviewed,
    lint_source_reviewed,
    run_all_lints,
)


from tests._source_store_fake import _DB, _asset, _chunk


# ──────────────────────────────────────────────────────────────────────
#  (a) account scope — decided by tree position, never by title
# ──────────────────────────────────────────────────────────────────────


def _row(**kw):
    base = {"id": "p1", "company_id": "co-1", "deal_stage": None,
            "companies": {"kind": "client"}}
    base.update(kw)
    return base


def test_project_columns_select_parent_id():
    assert "parent_id" in _PROJECT_COLUMNS
    assert "*" not in _PROJECT_COLUMNS


def test_the_account_node_ingests_at_account_scope():
    folders = _row_to_folders(_row(parent_id=None))
    assert folders.is_account_node and folders.ingest_scope == "account"


@pytest.mark.parametrize("row", [
    _row(parent_id="acct"),                                   # a child job
    _row(parent_id=None, deal_stage="Won"),                   # parentless job
    _row(parent_id=None, companies={"kind": "self"}),         # self-company
    _row(),                                                   # parent_id NOT read
], ids=["child", "parentless-job", "self-company", "column-not-selected"])
def test_everything_else_stays_project_scope(row):
    assert _row_to_folders(row).ingest_scope == "project"


def _folders(**kw):
    base = dict(project_id="p-acct", company_id="co-1", company_kind="client",
                google_drive_folder_id="F", mc_dropbox_folder_id=None,
                enable_google_drive=True, enable_dropbox=False)
    base.update(kw)
    return ProjectFolders(**base)


_REF = FileRef(source="drive", id="D1", name="Company Deck.pdf",
               mime_type=None, size=None, modified=None)


def test_stamp_writes_account_scope_for_the_account_node():
    db = _DB(rag_assets=[_asset("x", "Company Deck.pdf", project="p-acct",
                                file_path="/t/x")])
    _stamp_scope(db, _folders(is_account_node=True), "/t/x", _REF)
    assert db.tables["rag_assets"][0]["scope"] == "account"


def test_an_account_stamped_doc_reaches_a_sibling_read():
    """The payoff: a doc filed under the account node is pullable AND
    listable AND fetchable from a sibling job of the same company."""
    db = _DB(
        rag_assets=[_asset("x", "Company Deck.pdf", project="p-acct", file_path="/t/x",
                           source_provider="drive", source_file_id="D1")],
        asset_chunks=[_chunk("x", "company positioning")],
    )
    _stamp_scope(db, _folders(is_account_node=True), "/t/x", _REF)

    pulled = pull_source(db, "p-sibling", "co-1", "Company Deck.pdf")
    assert pulled["chunks"] == ["company positioning"]

    listed = list_sources(db, "p-sibling", "co-1", include_account=True)
    assert [(e["title"], e.get("scope")) for e in listed] == [("Company Deck.pdf", "account")]
    # Default listing is unchanged: the project's own rows only.
    assert list_sources(db, "p-sibling", "co-1") == []


def test_a_job_stamp_stays_project_scope_and_invisible_to_siblings():
    db = _DB(rag_assets=[_asset("x", "Job Deck.pdf", project="p-acct", file_path="/t/x")],
             asset_chunks=[_chunk("x", "job text")])
    _stamp_scope(db, _folders(), "/t/x", _REF)
    assert db.tables["rag_assets"][0]["scope"] == "project"
    assert list_sources(db, "p-sibling", "co-1", include_account=True) == []


def test_fetch_reaches_an_account_doc_when_given_the_company(monkeypatch, tmp_path):
    db = _DB(rag_assets=[_asset("x", "Company Deck.pdf", project="p-acct", scope="account",
                                source_provider="drive", source_file_id="D1",
                                source_path=None, url=None)])
    import cp_engine.project_sources as ps
    monkeypatch.setattr(ps, "download_file", lambda ref, d: Path(d) / ref.name)
    assert "error" in fetch_source(db, "p-sibling", "Company Deck", tmp_path)
    out = fetch_source(db, "p-sibling", "Company Deck", tmp_path, company_id="co-1")
    assert out["title"] == "Company Deck.pdf"


# ──────────────────────────────────────────────────────────────────────
#  (b) confidentiality markings → status_note
# ──────────────────────────────────────────────────────────────────────

# Every phrase here is verbatim from a live source (2026-09-30 scan).
_TRUE_MARKINGS = [
    ("INFOBLOX | CAB 2026 | CONFIDENTIAL 12", "confidential"),
    ("11 | 24 INTERNAL – SAP Concur Confidential | 2025 SAP Concur AI Messaging Guide",
     "confidential"),
    ("©2026 Mastercard. Proprietary and Confidential 10 Agenda", "confidential"),
    ("AMEX GBT STORYBOOK SAP and External Parties under NDA Only At launch", "nda"),
    ("Concur Compete team CONFIDENTIAL – FOR INTERNAL USE ONL Y Global", "internal-only"),
    ("# CEO rebrand announcement — DRAFT, EMBARGOED until 2026-09-02", "embargoed"),
    ("UNPUBLISHED AND NOT FINAL. Do not quote externally, do not share outside",
     "no-distribution"),
]

# Also verbatim — prose that TALKS about confidentiality, which must not fire.
_PROSE = [
    "interview transcripts to preserve key meaning while maintaining confidentiality.",
    "The process is location-specific and requires a confidential ticket",
    "Workshop posters will be covered to protect confidential client frameworks",
    "Our proprietary AI model detects brand abuse in visual",
    "Confidential Information and Assignment Agreement (CIIAA)",
    "Subscribers Say Buyers Council Announcements Reprints Ethics Embargoes Privacy",
    "3 | Launch date and embargo — when teams may begin using this language",
    "Block numbers/names are for internal use only and must be in all caps",
    "tion shows WHY and HOW CONFIDENT.",
    "9. CONFIDENTIAL INFORMATION. Each party shall",
    "we signed the NDA last week",
]


@pytest.mark.parametrize("text,kind", _TRUE_MARKINGS)
def test_real_markings_are_detected(text, kind):
    assert kind in {m.kind for m in detect_markings(text)}


@pytest.mark.parametrize("text", _PROSE)
def test_prose_about_confidentiality_is_not_a_marking(text):
    assert detect_markings(text) == []


def test_a_repeated_footer_is_one_marking():
    body = "\n".join(f"page {i} INFOBLOX | CAB 2026 | CONFIDENTIAL {i}" for i in range(24))
    assert len(detect_markings(body)) == 1


def test_the_note_says_detected_and_unconfirmed():
    note = status_note_for(detect_markings("INFOBLOX | CAB 2026 | CONFIDENTIAL 12"))
    assert note.startswith("auto-detected at ingest")
    assert "unconfirmed" in note and "CAB 2026" in note
    assert status_note_for([]) is None


def _ingested(note=None, text="INFOBLOX | CAB 2026 | CONFIDENTIAL 12"):
    return _DB(
        rag_assets=[_asset("x", "EBC Slides.pptx", project="p-job", file_path="/t/x",
                           status_note=note)],
        asset_chunks=[_chunk("x", "intro"), _chunk("x", text, 1)],
    )


def test_ingest_writes_the_detected_note_to_an_empty_status_note():
    db = _ingested()
    note = _flag_confidentiality(db, _folders(project_id="p-job"), "/t/x")
    assert note and db.tables["rag_assets"][0]["status_note"] == note


def test_ingest_never_overwrites_a_human_note():
    db = _ingested(note="embargoed until launch — Janet 09-01")
    assert _flag_confidentiality(db, _folders(project_id="p-job"), "/t/x") is None
    assert db.tables["rag_assets"][0]["status_note"] == "embargoed until launch — Janet 09-01"
    assert db.updates == []


def test_an_unmarked_body_writes_nothing():
    db = _ingested(text="a public case study")
    assert _flag_confidentiality(db, _folders(project_id="p-job"), "/t/x") is None
    assert db.updates == []


def test_a_marking_past_the_first_page_of_chunks_is_still_found(monkeypatch):
    """The scan pages the chunk read — a marking in chunk 1,001 counts."""
    import cp_engine.asset_ingest as ai
    monkeypatch.setattr(ai, "_CHUNK_PAGE", 3)
    db = _DB(rag_assets=[_asset("x", "Long.pdf", project="p-job", file_path="/t/x")],
             asset_chunks=[_chunk("x", "body", i) for i in range(7)]
             + [_chunk("x", "SAP Confidential Page 7 of 7", 7)])
    assert _flag_confidentiality(db, _folders(project_id="p-job"), "/t/x")


def test_the_ingest_loop_counts_and_announces_a_flag(monkeypatch, tmp_path):
    """End to end through `ingest_project_assets`: one created file whose
    body is marked lands a note, a count and a visible source_note."""
    import cp_engine.asset_ingest as ai

    db = _DB()
    folders = _folders(project_id="p-job")
    monkeypatch.setattr(ai, "resolve_project_folders", lambda c, code: folders)
    monkeypatch.setattr(ai, "list_files", lambda *a, **k: ([_REF], []))
    monkeypatch.setattr(ai, "_unchanged_since_last_ingest", lambda *a, **k: False)
    monkeypatch.setattr(ai, "_existing_dup_at_other_path", lambda *a, **k: False)
    monkeypatch.setattr(ai, "_supersede_same_title", lambda *a, **k: 0)
    monkeypatch.setattr(ai, "_existing_source_titles", lambda *a, **k: set())

    def _download(ref, d, *_a):
        p = Path(d) / ref.name
        p.write_bytes(b"x")
        return p

    monkeypatch.setattr(ai, "download_file", _download)

    class _Pipeline:
        def ingest_file(self, file_path, title, url):
            db.tables.setdefault("rag_assets", []).append(
                _asset("new", title, project="p-job", file_path=file_path))
            db.tables.setdefault("asset_chunks", []).append(
                _chunk("new", "SAP and External Parties under NDA Only"))
            return SimpleNamespace(action="created")

    result = ai.ingest_project_assets("ggl-5168", client=db, pipeline=_Pipeline(),
                                      tmp_root=tmp_path)
    assert result.created == 1 and result.flagged_confidential == 1
    assert result.failures == []
    assert any("under NDA" in n["note"] for n in result.source_notes)
    assert "nda" in db.tables["rag_assets"][0]["status_note"]


# ──────────────────────────────────────────────────────────────────────
#  (c) dangling `Source reviewed:`
# ──────────────────────────────────────────────────────────────────────

_REVIEW = (
    "---\nProject: ibx-5153\n---\n# ECD creative room review\n\n"
    "**Source reviewed:** `Infoblox_Truth_AI_Depends_On_ECD_Creative_Room.md` "
    "(Marcello + creative team, 17 routes)\n"
)


def test_the_real_header_shape_is_extracted():
    assert extract_source_reviewed(_REVIEW) == [
        "Infoblox_Truth_AI_Depends_On_ECD_Creative_Room.md"]
    assert extract_source_reviewed("Sources reviewed: deck v02.pptx, notes.md") == [
        "deck v02.pptx", "notes.md"]
    # Prose makes no claim to resolve.
    assert extract_source_reviewed("Source reviewed: the Q3 deck, from memory") == []


def test_a_never_ingested_source_is_flagged():
    out = lint_source_reviewed({"review-v01.md": _REVIEW}, {"Some Other Deck.pptx"})
    assert len(out) == 1 and "Infoblox_Truth_AI_Depends_On_ECD_Creative_Room.md" in out[0]


@pytest.mark.parametrize("titles,local", [
    ({"Infoblox_Truth_AI_Depends_On_ECD_Creative_Room.md"}, ()),
    ({"infoblox truth ai depends on ecd creative room.md.pdf"}, ()),   # re-exported
    ({"Infoblox_Truth_AI_Depends_On_ECD_Creative_Room"}, ()),          # Drive-native
    (set(), {"Infoblox_Truth_AI_Depends_On_ECD_Creative_Room.md"}),    # in the tree
], ids=["exact", "extension-and-case", "no-extension", "local-file"])
def test_a_reachable_source_is_not_flagged(titles, local):
    assert lint_source_reviewed({"review-v01.md": _REVIEW}, titles, local) == []


def test_the_walk_stops_at_a_child_workstream(tmp_path):
    (tmp_path / "cp.md").write_text("parent")
    (tmp_path / "review.md").write_text(_REVIEW)
    child = tmp_path / "ibx-5199-child"
    child.mkdir()
    (child / "cp.md").write_text("child")
    (child / "child-review.md").write_text(_REVIEW)
    plain = tmp_path / "notes"
    plain.mkdir()
    (plain / "deep.md").write_text("x")
    docs, names = workstream_docs(tmp_path)
    assert set(docs) == {"cp.md", "review.md", "notes/deep.md"}
    assert "child-review.md" not in names


def test_run_all_lints_reports_it_even_with_no_spine_rows():
    """The check reads the tree + the store, not the spine: a workstream
    with no live elements must still hear about a dangling source."""
    db = _DB(spine_substance=[])
    out = run_all_lints(db, ["ibx-5153"], workstream_docs={"review-v01.md": _REVIEW},
                        source_titles=set())
    assert len(out) == 1 and "never ingested" in out[0]
    # Omitted inputs skip the check rather than guessing.
    assert run_all_lints(db, ["ibx-5153"]) == []


def test_ingested_titles_include_every_status_and_the_account():
    db = _DB(rag_assets=[
        _asset("a", "Archived.pdf", status="archived"),
        _asset("b", "Live.pdf"),
        _asset("c", "Company.pdf", project="p-acct", scope="account"),
        _asset("d", "Other Company.pdf", project="p-x", company="co-2", scope="account"),
    ])
    assert ingested_source_titles(db, "p-job", "co-1") == {
        "Archived.pdf", "Live.pdf", "Company.pdf"}


# ──────────────────────────────────────────────────────────────────────
#  (d) exists-but-empty ≠ not found
# ──────────────────────────────────────────────────────────────────────


def _empty_store():
    return _DB(
        rag_assets=[_asset("e", "Scanned Brief.pdf"), _asset("r", "Readable.docx")],
        asset_chunks=[_chunk("r", "real text")],
    )


def test_a_zero_chunk_source_pulls_as_empty_not_missing():
    out = pull_source(_empty_store(), "p-job", "co-1", "Scanned Brief.pdf")
    assert out["empty"] is True and out["chunk_count"] == 0
    assert out["asset_id"] == "e"
    assert "EXISTS" in out["note"] and "no source named" not in out["note"]


def test_a_title_that_was_never_ingested_is_still_not_found():
    out = pull_source(_empty_store(), "p-job", "co-1", "Never Ingested.pdf")
    assert "empty" not in out
    assert out["note"].startswith("no source named")


def test_the_listing_flags_the_empty_source_only():
    listed = {e["title"]: e for e in list_sources(_empty_store(), "p-job", "co-1")}
    assert listed["Scanned Brief.pdf"]["empty"] is True
    assert listed["Scanned Brief.pdf"]["chunk_count"] == 0
    assert "empty" not in listed["Readable.docx"]


def test_a_failed_chunk_check_flags_nothing():
    """Never call a document empty because the check could not run."""
    db = _empty_store()
    real_table = db.table

    def table(name):
        if name == "asset_chunks":
            raise RuntimeError("42501")
        return real_table(name)

    db.table = table
    assert all("empty" not in e for e in list_sources(db, "p-job", "co-1"))


def test_chunk_presence_pages_past_max_rows():
    """3 assets, the first with more chunks than a page: the other two must
    not read as empty just because page one was all asset A."""
    db = _DB(asset_chunks=[_chunk("a", "t", i) for i in range(5)]
             + [_chunk("b", "t"), _chunk("c", "t")])
    assert mc2_db.asset_ids_with_chunks(db, ["a", "b", "c", "z"], page=2) == {"a", "b", "c"}


def test_drive_creds_load_from_the_mc2_env_resolving_relative_paths(monkeypatch, tmp_path):
    backend = tmp_path / "mc-2" / "backend"
    backend.mkdir(parents=True)
    (backend / ".env").write_text("GOOGLE_SERVICE_ACCOUNT_FILE=sa.json\nOTHER=1\n")
    for k in mc2_db.DRIVE_CRED_KEYS:
        monkeypatch.delenv(k, raising=False)
    cfg = SimpleNamespace(local_repos={"mc-2": str(tmp_path / "mc-2")})
    mc2_db.load_drive_creds(cfg)
    import os
    assert os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"] == str(backend / "sa.json")
    # An already-set var wins.
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "/explicit.json")
    mc2_db.load_drive_creds(cfg)
    assert os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"] == "/explicit.json"


def test_a_drive_fetch_without_creds_says_why(monkeypatch, tmp_path):
    db = _DB(rag_assets=[_asset("x", "Brief.pdf", source_provider="drive",
                                source_file_id="D1", source_path=None, url=None)])
    import cp_engine.project_sources as ps

    def _boom(*_a, **_k):
        raise RuntimeError("could not authenticate")

    monkeypatch.setattr(ps, "download_file", _boom)
    for k in mc2_db.DRIVE_CRED_KEYS:
        monkeypatch.delenv(k, raising=False)
    out = fetch_source(db, "p-job", "Brief.pdf", tmp_path)
    assert "no Google Drive credentials" in out["error"]
