"""Learning phase 5: freelancers — reviews, agreed vs actual hours, and who
to use next (Drew, 2026-11-11). Internal throughout.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from cp_engine import freelancers as F


class _Q:
    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]

    def select(self, _c):
        return self

    def eq(self, c, v):
        self.rows = [r for r in self.rows if r.get(c) == v]
        return self

    def in_(self, c, vals):
        self.rows = [r for r in self.rows if r.get(c) in vals]
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)


class _C:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Q(self.tables.get(name, []))


PEOPLE = [
    {"id": "geoff", "name": "Geoff Ahmann", "kind": "freelancer", "is_external": True,
     "roles": "art director", "notes": "", "default_hourly_rate": 100, "payment_method": None},
    {"id": "maya", "name": "Maya Ruiz", "kind": "freelancer", "is_external": True,
     "roles": "motion designer", "notes": "", "default_hourly_rate": 90, "payment_method": "gusto", "paid_by": "payroll"},
    {"id": "sam", "name": "Sam Lee", "kind": "freelancer", "is_external": True,
     "roles": "motion designer", "notes": "", "default_hourly_rate": 80, "payment_method": "ach", "paid_by": "invoice"},
    {"id": "drew", "name": "Drew Fiero", "kind": "staff", "is_external": False},
]


def _client(hours=(), payables=()):
    return _C({
        "entities": PEOPLE,
        "contractor_hours": list(hours),
        "payables": list(payables),
        "freelancer_reviews": [
            {"entity_id": "maya", "project_id": "p-vid", "status": "done", "rating": 5,
             "recommend": "yes", "role": "motion", "strengths": "fast motion work", "watch_outs": None},
            {"entity_id": "sam", "project_id": "p-vid", "status": "done", "rating": 2,
             "recommend": "no", "role": "motion", "strengths": "", "watch_outs": "missed two deadlines"},
        ],
        "sprint_allocations": [
            {"entity_id": "maya", "project_id": "p-vid", "hours": 40, "week_start": "2026-10-05"},
            {"entity_id": "sam", "project_id": "p-vid", "hours": 30, "week_start": "2026-10-05"},
            {"entity_id": "geoff", "project_id": "p-camp", "hours": 50, "week_start": "2026-09-01"},
        ],
        "projects": [{"id": "p-vid", "code": "sap-5198", "project_types": ["video", "campaign"]},
                     {"id": "p-camp", "code": "ibx-5153", "project_types": ["campaign"]}],
    })


def test_review_patch_rules():
    p = F.review_patch(rating=4, recommend="with-reservations", strengths=" quick ", reviewer="d@x")
    assert p["status"] == "done" and p["strengths"] == "quick"
    with pytest.raises(F.FreelancerError, match="1–5"):
        F.review_patch(rating=6, recommend="yes")
    with pytest.raises(F.FreelancerError, match="recommend"):
        F.review_patch(rating=3, recommend="maybe")


def test_hours_row_rules():
    assert F.hours_row(entity_id="e", project_id="p", kind="agreed", hours=40)["source"] == "agreement"
    assert F.hours_row(entity_id="e", project_id="p", kind="actual", hours=8, source="gusto")["source"] == "gusto"
    with pytest.raises(F.FreelancerError, match="actual hours come from"):
        F.hours_row(entity_id="e", project_id="p", kind="actual", hours=8, source="agreement")
    with pytest.raises(F.FreelancerError, match="negative"):
        F.hours_row(entity_id="e", project_id="p", kind="agreed", hours=-1)


def test_hours_status_tracks_against_the_newest_agreement():
    c = _client(hours=[
        {"entity_id": "maya", "project_id": "p-vid", "kind": "agreed", "hours": 20, "created_at": "2026-10-01"},
        {"entity_id": "maya", "project_id": "p-vid", "kind": "agreed", "hours": 40, "created_at": "2026-10-10"},
        {"entity_id": "maya", "project_id": "p-vid", "kind": "actual", "hours": 34, "source": "gusto"},
        {"entity_id": "sam", "project_id": "p-vid", "kind": "agreed", "hours": 10, "created_at": "2026-10-01"},
        {"entity_id": "sam", "project_id": "p-vid", "kind": "actual", "hours": 12, "source": "invoice"},
    ])
    by = {r["name"]: r for r in F.hours_status(c, project_id="p-vid")}
    assert by["Maya Ruiz"]["agreed"] == 40 and by["Maya Ruiz"]["state"] == "near"
    assert by["Maya Ruiz"]["actuals_from"] == "Gusto timesheets"
    assert by["Sam Lee"]["state"] == "over" and by["Sam Lee"]["actuals_from"] == "invoices"


def test_with_no_actuals_payables_give_a_labelled_estimate():
    c = _client(hours=[{"entity_id": "geoff", "project_id": "p-camp", "kind": "agreed",
                        "hours": 40, "created_at": "2026-09-01"}],
                payables=[{"entity_id": "geoff", "project_id": "p-camp", "amount": 3500}])
    (row,) = F.hours_status(c, project_id="p-camp")
    assert row["actual"] == 35.0 and row["estimated"] is True and row["state"] == "near"
    assert "set Paid by" in row["actuals_from"]  # how Geoff is paid isn't set yet


def test_recommend_ranks_with_evidence_and_never_hides_a_no():
    out = F.recommend(_client(), need="motion designer", project_types=["video"],
                      today=date(2026, 11, 11))
    names = [r["name"] for r in out]
    assert names[0] == "Maya Ruiz"
    assert "Sam Lee" in names and names.index("Sam Lee") > names.index("Maya Ruiz")
    sam = next(r for r in out if r["name"] == "Sam Lee")
    assert sam["not_recommended"] == 1 and sam["watch_outs"] == ["missed two deadlines"]
    assert "Drew Fiero" not in names  # staff are not freelancers


def test_find_person_exact_then_unique_substring():
    c = _client()
    assert F.find_person(c, "maya ruiz")["id"] == "maya"
    assert F.find_person(c, "Geoff")["id"] == "geoff"
    assert F.find_person(c, "a") is None  # ambiguous


def test_actuals_follow_paid_by():
    assert F._actuals_from({"paid_by": "payroll"}) == "Gusto timesheets"
    assert F._actuals_from({"paid_by": "invoice", "payment_method": "ach"}) == "invoices"
    assert F._actuals_from({"paid_by": None, "payment_method": "gusto"}) == "Gusto timesheets"
    assert "set Paid by" in F._actuals_from({"paid_by": None, "payment_method": "ach"})
