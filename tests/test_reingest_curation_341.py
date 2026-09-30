"""#341 — a re-ingest carries the predecessor's HUMAN curation forward.

Unit level: the pure rule in `source_curation`, the DB step
`_carry_forward_curation`, and the summariser's fingerprint stamp that makes
"hand-written vs generated" decidable. Behaviour through the real ingest loop
lives in test_reingest_curation_341_control.py.
"""
from __future__ import annotations

from cp_engine.asset_ingest import ProjectFolders, _carry_forward_curation
from cp_engine.project_sources import _persist_description
from cp_engine.source_curation import (
    GENERATED_SHA_KEY,
    TRACKED_KEY,
    carry_forward_payload,
    description_fingerprint,
    is_hand_written_description,
    is_human_note,
)
from cp_engine.source_markers import detect_markings, status_note_for

from tests._source_store_fake import _DB, _asset

AUTO_NOTE = status_note_for(detect_markings("CAB 2026 | CONFIDENTIAL"))
HUMAN = "form-gated — what ingested is the landing-page abstract"

_FOLDERS = ProjectFolders(
    project_id="p-job", company_id="co-1", company_kind="client",
    google_drive_folder_id="F", mc_dropbox_folder_id=None,
    enable_google_drive=True, enable_dropbox=False,
)


# ── the rule ──────────────────────────────────────────────────────────


def test_human_vs_auto_note():
    assert is_human_note(HUMAN)
    assert not is_human_note(AUTO_NOTE)
    assert not is_human_note(None) and not is_human_note("  ")


def test_description_authorship():
    gen = "A two-page brief on the Q4 launch."
    stamped = {TRACKED_KEY: True, GENERATED_SHA_KEY: description_fingerprint(gen)}
    assert not is_hand_written_description(gen, stamped)          # summariser's
    assert is_hand_written_description("Corrected by Drew", stamped)  # edited after
    assert is_hand_written_description(gen, {TRACKED_KEY: True})  # never summarised
    # Pre-#341 row: authorship unknown → not proof → not carried.
    assert not is_hand_written_description("anything", {})
    assert not is_hand_written_description("anything", None)
    assert not is_hand_written_description("", {TRACKED_KEY: True})


def test_payload_always_tracks_and_merges_meta():
    p = carry_forward_payload({"meta": {"chunks": 5}}, None)
    assert p == {"meta": {"chunks": 5, TRACKED_KEY: True}}
    assert carry_forward_payload({"meta": None}, None) == {"meta": {TRACKED_KEY: True}}


def test_human_note_beats_an_auto_note_on_the_new_row():
    p = carry_forward_payload({"status_note": AUTO_NOTE}, {"status_note": HUMAN})
    assert p["status_note"] == HUMAN


def test_an_auto_note_is_never_carried():
    assert "status_note" not in carry_forward_payload({}, {"status_note": AUTO_NOTE})


def test_a_generated_description_is_not_carried():
    gen = "A two-page brief."
    prior = {"description": gen,
             "meta": {TRACKED_KEY: True, GENERATED_SHA_KEY: description_fingerprint(gen)}}
    assert "description" not in carry_forward_payload({}, prior)


def test_the_new_rows_own_description_is_never_overwritten():
    prior = {"description": "hand", "meta": {TRACKED_KEY: True}}
    assert "description" not in carry_forward_payload({"description": "own"}, prior)


# ── the DB step ───────────────────────────────────────────────────────


def test_carry_writes_only_the_new_row():
    db = _DB(rag_assets=[
        _asset("old", "B.pdf", status="superseded", file_path="/o",
               status_note=HUMAN, description="hand", meta={TRACKED_KEY: True}),
        _asset("new", "B.pdf", file_path="/n", prev_asset_id="old", meta={"chunks": 2}),
    ])
    _carry_forward_curation(db, _FOLDERS, "/n")
    old, new = db.tables["rag_assets"]
    assert new["status_note"] == HUMAN and new["description"] == "hand"
    assert new["meta"] == {"chunks": 2, TRACKED_KEY: True}
    assert [u[2] for u in db.updates] == [1]
    assert old["meta"] == {TRACKED_KEY: True}  # predecessor untouched
    for _, cols in db.selects:
        assert "*" not in cols


def test_carry_without_a_predecessor_only_tracks():
    db = _DB(rag_assets=[_asset("new", "B.pdf", file_path="/n")])
    assert _carry_forward_curation(db, _FOLDERS, "/n") == {"meta": {TRACKED_KEY: True}}


def test_carry_with_no_row_is_a_no_op():
    db = _DB(rag_assets=[])
    assert _carry_forward_curation(db, _FOLDERS, "/n") == {}
    assert db.updates == []


# ── the summariser stamp ──────────────────────────────────────────────


def test_persisted_summary_is_fingerprinted_into_meta():
    db = _DB(rag_assets=[_asset("a", "B.pdf", meta={"chunks": 2})])
    _persist_description(db, "a", "A two-page brief.")
    row = db.tables["rag_assets"][0]
    assert row["description"] == "A two-page brief."
    assert row["meta"] == {"chunks": 2,
                           GENERATED_SHA_KEY: description_fingerprint("A two-page brief.")}
    # …which is exactly what makes it read as generated at the next re-ingest.
    row["meta"][TRACKED_KEY] = True
    assert not is_hand_written_description(row["description"], row["meta"])
