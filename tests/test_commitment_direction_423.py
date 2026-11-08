"""#423: meeting ingest filed what the client owes us as ours.

On 2026-11-07, 3 of 350 open commitments were ``them_to_us``. Two causes:
``set-client-ask-task`` (the prompt's "external commitment we're waiting
on") was stamped ``us_to_them``, and every other writer left a client
stakeholder's item at the ``internal`` default. The fix: a client-side
owner — carded on the company's Stakeholders layer, not on our roster —
settles the row ``them_to_us`` at the ingest writers.
"""
from __future__ import annotations

from datetime import date

from cp_engine import commitments
from cp_engine.asks import build_import_row
from cp_engine.commitments import (
    INTERNAL,
    THEM_TO_US,
    US_TO_THEM,
    is_client_side_owner,
    settle_direction,
    write_commitment,
)
from cp_engine.mc2_db import Tables

_PEOPLE = [
    {"name": "Drew Fiero", "email": "drew@firstperson.is", "archived_at": None},
    {"name": "Jessica Schorer", "email": None, "archived_at": None},
]
_PROJECTS = [
    {"id": "p-job", "company_id": "co-slt"},
    {"id": "p-acct", "company_id": "co-slt"},
    {"id": "p-other", "company_id": "co-ibx"},
]
_CARDS = [
    # On the ACCOUNT node — must still count for a job beneath it.
    {"project_id": "p-acct", "framing": "Leah Ward — Salesloft brand lead",
     "layer": "Stakeholders", "status": "live", "archived": False},
    {"project_id": "p-job", "framing": "Tara Haney - SAP Concur lead",
     "layer": "Stakeholders", "status": "live", "archived": False},
    # One of OURS carded on the client's project (real: "Jess — Editor").
    {"project_id": "p-job", "framing": "Jess — Editor",
     "layer": "Stakeholders", "status": "live", "archived": False},
    {"project_id": "p-job", "framing": "Old Contact — left",
     "layer": "Stakeholders", "status": "live", "archived": True},
    # Another company's person: not client-side HERE.
    {"project_id": "p-other", "framing": "Janet Noe — Infoblox",
     "layer": "Stakeholders", "status": "live", "archived": False},
]


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.filters, self.ins = db, name, [], None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        vals = list(vals)
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def limit(self, *_a):
        return self

    def insert(self, row):
        self.ins = row
        return self

    def execute(self):
        class R:
            pass
        r = R()
        if self.ins is not None:
            self.db.inserted.append(self.ins)
            r.data = [self.ins]
            return r
        if self.name == Tables.ENTITIES:
            r.data = list(_PEOPLE)
            return r
        rows = {Tables.PROJECTS: _PROJECTS, Tables.SPINE_SUBSTANCE: _CARDS}.get(
            self.name, [])
        r.data = [x for x in rows if all(f(x) for f in self.filters)]
        return r


class _DB:
    def __init__(self):
        self.inserted: list[dict] = []

    def table(self, name):
        return _Q(self, name)


OWNER = {"id": "p-job", "code": "slt-5196", "kind": "project",
         "canonical_code": "slt-5196"}


def test_carded_client_owner_is_client_side():
    db = _DB()
    assert is_client_side_owner(db, "p-job", "Leah Ward")  # account-level card
    assert is_client_side_owner(db, "p-job", "Tara Haney")  # " - " separator
    assert is_client_side_owner(db, "p-job", "Leah Ward / Animal")  # first owner


def test_unknown_or_ours_is_not_client_side():
    db = _DB()
    assert not is_client_side_owner(db, "p-job", "Someone Unknown")  # uncarded
    assert not is_client_side_owner(db, "p-job", "Jess")  # carded but on roster
    assert not is_client_side_owner(db, "p-job", "Old Contact")  # archived card
    assert not is_client_side_owner(db, "p-job", "Janet Noe")  # other company
    assert not is_client_side_owner(db, "p-job", "Leah Ward", "leah@x.com")
    assert not is_client_side_owner(db, "p-job", None)


def test_settle_direction_only_ever_moves_to_them_to_us():
    db = _DB()
    assert settle_direction(db, "p-job", US_TO_THEM, "Leah Ward") == THEM_TO_US
    assert settle_direction(db, "p-job", INTERNAL, "Tara Haney") == THEM_TO_US
    assert settle_direction(db, "p-job", US_TO_THEM, "Drew Fiero") == US_TO_THEM
    assert settle_direction(db, "p-job", INTERNAL, "Unknown") == INTERNAL


def test_write_commitment_settles_only_when_asked():
    db = _DB()
    write_commitment(db, owner=OWNER, description="Upload exports to Vidyard",
                     cp_hash="h1", source_kind="meeting_ingest",
                     owner_name="Leah Ward", infer_client_side=True)
    # An explicit writer (a person naming the direction) is never overridden.
    write_commitment(db, owner=OWNER, description="Send Leah the cut",
                     cp_hash="h2", source_kind="manual",
                     direction=US_TO_THEM, owner_name="Leah Ward")
    assert [r["direction"] for r in db.inserted] == [THEM_TO_US, US_TO_THEM]


def test_sprint_import_ask_owned_by_client_is_them_to_us():
    row = build_import_row(
        text="Send updated Competitive Differentiation Library when ready.",
        code="slt-5196", project_id="p-job", who="Tara Haney", by=None,
        asked_date="2026-11-02", today=date(2026, 11, 8), client=_DB(),
    )
    assert row["direction"] == THEM_TO_US


def test_sprint_import_ask_owned_by_us_keeps_default():
    row = build_import_row(
        text="Pull selects for the 30s cut", code="slt-5196",
        project_id="p-job", who="Drew", by=None, asked_date="2026-11-02",
        today=date(2026, 11, 8), client=_DB(),
    )
    assert "direction" not in row  # column default (internal)


def test_unreadable_cards_degrade_loudly_not_silently():
    class Boom(_DB):
        def table(self, name):
            if name == Tables.SPINE_SUBSTANCE:
                raise RuntimeError("42501")
            return super().table(name)

    warnings: list = []
    assert settle_direction(Boom(), "p-job", INTERNAL, "Leah Ward",
                            warnings=warnings) == INTERNAL
    assert any("stakeholder cards unreadable" in w for w in warnings)
    assert commitments._CLIENT_NAMES_CACHE.get("p-job") is None  # not cached
