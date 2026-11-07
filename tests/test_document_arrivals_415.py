"""#415: an open ask for a document beside the file that may already answer it.

The miss: Tara Haney attached "Creative Brief V2.docx" to her 10-09 email
(sap-5198); the ask "Written brief document …" was logged 10-16 and sat open
15 days. The rows below are the real ones (MC-2, 2026-11-07), so the match is
tested on the vocabulary that failed, not an invented one.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from cp_engine import document_arrivals as da
from cp_engine.commitments_sweep import _row

TODAY = date(2026, 10, 20)
PID = "3be1ecff-49d3-444d-93fe-fc07bc3e23b1"
CODE = "sap-5198-2027-ad-videos"

WRITTEN_BRIEF = ("Written brief document (preview was walked through live; Tara said "
                 "she'd send after the call) (from Tara Haney (SAP Concur))")
BRIEF_FILE = "SAP Concur_2027 Video Ads_Creative Brief V2.docx"
FATHOM_SUMMARY = "SAP Concur | Video Ad Brief Walkthrough"
LINK_CARD = "Link · SAP 5198 Creative Brief v01.md"
TARA = '"Haney, Tara" <tara.haney@sap.com>'


def _ask(id_: str, desc: str, created: str = "2026-10-16", owner: str | None = None):
    return _row({
        "id": id_, "description": desc, "owner_name": owner, "owner_email": None,
        "due_date": None, "date_status": "proposed", "status": "open",
        "source_kind": "meeting_ingest", "created_at": f"{created}T17:18:06+00:00",
    }, TODAY)


def _store(name: str, day: str, pid: str = PID) -> da.Arrival:
    return da.Arrival(name=name, arrived=date.fromisoformat(day), where="store",
                      ref=f"asset-{name[:6]}", project_id=pid)


# ── the match ─────────────────────────────────────────────────────────


def test_the_brief_answers_the_written_brief_ask():
    assert da.match(WRITTEN_BRIEF, BRIEF_FILE) == ("brief",)


def test_a_meeting_summary_or_link_card_is_not_a_file_sent_in():
    # The Fathom summary of the call where the ask was made shares "brief" —
    # it is not the brief. Nor is a link card's .md.
    assert da.match(WRITTEN_BRIEF, FATHOM_SUMMARY) is None
    assert da.match(WRITTEN_BRIEF, LINK_CARD) is None


def test_an_ask_that_names_no_document_matches_nothing():
    assert da.document_words("Add Jack to Slack channel") == frozenset()
    assert da.match("Add Jack to Slack channel", "Jack notes.docx") is None


def test_a_shared_word_that_is_not_a_document_word_is_not_enough():
    # "concur" is shared, but the ask asks for a deck and the file is a brief.
    assert da.match("Send the Concur research deck", BRIEF_FILE) is None


def test_versions_and_years_name_no_document():
    assert "v2" not in da.filename_words(BRIEF_FILE)
    assert "2027" not in da.filename_words(BRIEF_FILE)


# ── over a sweep ──────────────────────────────────────────────────────


def test_a_file_that_arrived_before_the_ask_was_logged_still_counts():
    """The brief arrived 10-09; the ask was logged at the 10-16 call."""
    groups = {CODE: [_ask("0f955d7c-1ff4", WRITTEN_BRIEF)]}
    found = da.arrivals(groups, today=TODAY, store=[_store(BRIEF_FILE, "2026-10-09")],
                        unrouted=[], code_ids={CODE: PID})
    (entry,) = found
    assert entry["key"] == "0f955d7c-1ff4"
    (f,) = entry["files"]
    assert f["name"] == BRIEF_FILE and f["where"] == "store" and f["shared"] == ["brief"]


def test_an_old_file_or_another_projects_file_does_not_count():
    groups = {CODE: [_ask("a", WRITTEN_BRIEF)]}
    old = _store("Old Creative Brief.docx", "2026-08-01")
    elsewhere = _store(BRIEF_FILE, "2026-10-17", pid="other-project")
    assert da.arrivals(groups, today=TODAY, store=[old, elsewhere], unrouted=[],
                       code_ids={CODE: PID}) == []


def test_an_unrouted_attachment_from_the_person_the_ask_names():
    """Sent to the bare cp@: the file never reached the store, only its name."""
    groups = {CODE: [_ask("a", WRITTEN_BRIEF)]}
    att = da.Arrival(name=BRIEF_FILE, arrived=date(2026, 10, 9), where="unrouted",
                     ref="msg-1@sap.com", sender=TARA)
    (entry,) = da.arrivals(groups, today=TODAY, store=[], unrouted=[att], code_ids={})
    assert entry["files"][0]["where"] == "unrouted"
    assert entry["files"][0]["sender"] == TARA


def test_an_unrouted_attachment_from_someone_else_is_not_matched():
    groups = {CODE: [_ask("a", WRITTEN_BRIEF)]}
    att = da.Arrival(name=BRIEF_FILE, arrived=date(2026, 10, 9), where="unrouted",
                     ref="m", sender="Pat Doe <pat@example.com>")
    assert da.arrivals(groups, today=TODAY, store=[], unrouted=[att], code_ids={}) == []


def test_an_unrouted_attachment_whose_code_tried_this_project():
    groups = {CODE: [_ask("a", "Send the final creative brief")]}
    att = da.Arrival(name=BRIEF_FILE, arrived=date(2026, 10, 18), where="unrouted",
                     ref="m", sender="Pat Doe <pat@example.com>", attempted_code="sap-5198")
    assert len(da.arrivals(groups, today=TODAY, store=[], unrouted=[att], code_ids={})) == 1


def test_names_sender_by_address_or_two_name_words():
    assert da.names_sender(_ask("a", "Email brief", owner="tara.haney@sap.com"), TARA)
    assert da.names_sender(_ask("a", "Brief (from Tara Haney)"), TARA)
    assert not da.names_sender(_ask("a", "Brief (from Tara Smith)"), TARA)


# ── readers + find ────────────────────────────────────────────────────


class _Q:
    def __init__(self, rows, boom=None):
        self.rows, self.boom, self.cols = rows, boom, None

    def select(self, cols):
        self.cols = cols
        assert "*" not in cols  # never SELECT *
        return self

    def eq(self, *_a):
        return self

    def in_(self, *_a):
        return self

    def execute(self):
        if self.boom:
            raise self.boom
        return SimpleNamespace(data=self.rows)


class _Client:
    def __init__(self, tables, boom=None):
        self.tables, self.boom = tables, boom or {}

    def table(self, name):
        return _Q(self.tables.get(name, []), self.boom.get(name))


def test_find_reads_both_halves_and_names_the_one_it_could_not_read():
    client = _Client(
        {"rag_assets": [
            {"id": "x1", "title": BRIEF_FILE, "created_at": "2026-10-09T19:02:00+00:00",
             "status": "active", "project_id": PID},
            {"id": "x2", "title": FATHOM_SUMMARY, "created_at": "2026-10-16T17:18:11+00:00",
             "status": "active", "project_id": PID},
        ]},
        # Before mc-2 migration 207 the attachments column does not exist.
        boom={"unrouted_emails": RuntimeError("column unrouted_emails.attachments does not exist")},
    )
    groups = {CODE: [_ask("a", WRITTEN_BRIEF)]}
    found, warnings = da.find(client, groups, today=TODAY, code_ids={CODE: PID})
    assert [f["name"] for f in found[0]["files"]] == [BRIEF_FILE]
    (w,) = warnings
    assert "unrouted email attachments unreadable" in w


def test_read_unrouted_keeps_document_attachments_only():
    client = _Client({"unrouted_emails": [{
        "id": "m1", "from_addr": TARA, "subject": "Recap", "received_at": "2026-10-09T19:02:00Z",
        "attempted_code": None, "status": "unrouted",
        "attachments": [{"filename": BRIEF_FILE, "size": 300000},
                        {"filename": "image001.png", "size": 900}],
    }]})
    (a,) = da.read_unrouted(client)
    assert a.name == BRIEF_FILE and a.sender == TARA and a.where == "unrouted"


def test_render_says_where_the_file_is():
    found = [{"project_code": CODE, "key": "0f955d7c-1ff4", "description": WRITTEN_BRIEF,
              "files": [{"name": BRIEF_FILE, "arrived": "2026-10-09", "where": "unrouted",
                         "sender": TARA, "ref": "m", "shared": ["brief"]}]}]
    text = "\n".join(da.render(found, ["x unreadable"]))
    assert "1 open ask(s) for a document that may already be here" in text
    assert "0f955d7c" in text and BRIEF_FILE in text
    assert "not in the source store" in text
    assert "document check incomplete — x unreadable" in text
    assert da.render([], []) == []


# ── daily health ──────────────────────────────────────────────────────


def test_daily_health_names_the_ask(monkeypatch):
    from cp_engine import commitments_sweep, daily_health

    groups = {CODE: [_ask("0f955d7c-1ff4", WRITTEN_BRIEF)]}
    monkeypatch.setattr(commitments_sweep, "sweep", lambda client, today=None: groups)
    monkeypatch.setattr(commitments_sweep, "canonical_codes", lambda client: {})
    client = _Client({
        "projects": [{"id": PID, "code": CODE}],
        "rag_assets": [{"id": "x1", "title": BRIEF_FILE, "status": "active",
                        "created_at": "2026-10-09T19:02:00+00:00", "project_id": PID}],
        "unrouted_emails": [],
    })
    check = daily_health.check_documents(client, TODAY)
    assert not check.ok and check.text == "1 asks"
    assert check.items == [f"{CODE} · 0f955d7c · {BRIEF_FILE}"]


def test_daily_health_never_reads_green_when_a_half_is_unreadable(monkeypatch):
    from cp_engine import commitments_sweep, daily_health

    monkeypatch.setattr(commitments_sweep, "sweep",
                        lambda client, today=None: {CODE: [_ask("a", "Add Jack to Slack")]})
    monkeypatch.setattr(commitments_sweep, "canonical_codes", lambda client: {})
    client = _Client({"projects": [{"id": PID, "code": CODE}]},
                     boom={"unrouted_emails": RuntimeError("no column")})
    check = daily_health.check_documents(client, TODAY)
    assert not check.ok and check.text == "partial"


@pytest.mark.parametrize("attempted,code,ok", [
    ("sap-5198", "sap-5198-2027-ad-videos", True),
    ("sap-5198-2027-ad-videos", "sap-5198", True),
    ("sap-519", "sap-5198", False),
    ("", "sap-5198", False),
])
def test_same_code(attempted, code, ok):
    assert da._same_code(attempted, code) is ok
