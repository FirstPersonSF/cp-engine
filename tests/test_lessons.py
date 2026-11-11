"""Lessons store helpers (learning phase 1, mc-2 migs 215–217).

The database enforces the rules; these tests pin what the helpers send and
the plain-language refusals they give before a round trip. The live rules
(trigger defaults, list membership, client scoping in match_lessons) were
probed on MC-2 in a rolled-back transaction on 2026-11-11.
"""
from __future__ import annotations

from datetime import date

import pytest

from cp_engine import lessons as L

TYPES = [{"slug": s, "name": s.title(), "sort": i, "active": True}
         for i, s in enumerate(["campaign", "video", "design"])]
MOMENTS = [{"slug": s, "name": s, "description": None, "sort": i, "active": True}
           for i, s in enumerate(["invoicing", "travel", "scope-change"])]


class _Q:
    def __init__(self, client, table):
        self.c, self.table, self.ops = client, table, []
        self.payload = None
        self.kind = "select"

    def select(self, cols):
        self.ops.append(("select", cols))
        return self

    def insert(self, row):
        self.kind, self.payload = "insert", row
        return self

    def update(self, patch):
        self.kind, self.payload = "update", patch
        return self

    def __getattr__(self, name):  # eq, overlaps, lte, is_, neq, order, limit
        def op(*args, **kw):
            self.ops.append((name, args, kw))
            return self
        return op

    def execute(self):
        self.c.calls.append((self.table, self.kind, self.payload, self.ops))
        data = self.c.responses.get((self.table, self.kind))
        if callable(data):
            data = data(self)
        return type("R", (), {"data": data})()


class _Client:
    def __init__(self):
        self.calls, self.rpcs = [], []
        self.responses = {
            ("project_types", "select"): TYPES,
            ("lesson_moments", "select"): MOMENTS,
            ("lessons", "insert"): lambda q: [{"id": "L1", **q.payload}],
            ("lessons", "update"): lambda q: [{"id": "L1", "text": "x" * 20, **q.payload}],
            ("lessons", "select"): [],
        }

    def table(self, name):
        return _Q(self, name)

    def rpc(self, name, params):
        self.rpcs.append((name, params))
        return type("R", (), {"execute": lambda s: type("D", (), {"data": [{"id": "L1"}]})()})()


def _writes(client, kind):
    return [c for c in client.calls if c[0] == "lessons" and c[1] == kind]


def test_add_lesson_cleans_and_embeds():
    c = _Client()
    seen = []
    row = L.add_lesson(
        c, text="  Get travelling directors' expenses in before the final invoice.  ",
        scope="practice", moments=["travel", "invoicing", "travel"],
        embed=lambda t: seen.append(t) or [0.1] * 4)
    insert = _writes(c, "insert")[0][2]
    assert insert["text"] == "Get travelling directors' expenses in before the final invoice."
    assert insert["moments"] == ["travel", "invoicing"]
    assert insert["status"] == "candidate"
    assert row["embedded"] is True and seen == [insert["text"]]
    assert _writes(c, "update")[0][2]["embedding"] == [0.1] * 4


def test_a_failed_embed_keeps_the_lesson():
    c = _Client()

    def boom(_t):
        raise RuntimeError("voyage down")

    row = L.add_lesson(c, text="Book the shoot crew two weeks out.", scope="practice", embed=boom)
    assert row["id"] == "L1"
    assert row["embedded"] is False and "voyage down" in row["embed_reason"]


@pytest.mark.parametrize("kwargs,message", [
    ({"scope": "client"}, "needs the client"),
    ({"scope": "person"}, "person's name"),
    ({"scope": "type"}, "at least one project type"),
    ({"scope": "practice", "moments": ["made-up"]}, "other_moment"),
    ({"scope": "type", "project_types": ["podcast"]}, "unknown project type"),
    ({"scope": "nope"}, "scope must be"),
])
def test_refusals_are_plain_and_nothing_is_written(kwargs, message):
    c = _Client()
    with pytest.raises(L.LessonError, match=message):
        L.add_lesson(c, text="A lesson long enough to pass.", **kwargs)
    assert not _writes(c, "insert")


def test_a_moment_added_to_the_list_is_accepted_without_a_release():
    c = _Client()
    c.responses[("lesson_moments", "select")] = MOMENTS + [
        {"slug": "podcast-booking", "name": "Podcast booking", "description": None, "sort": 99, "active": True}]
    L.add_lesson(c, text="Book podcast slots a month out.", scope="practice", moments=["podcast-booking"])
    assert _writes(c, "insert")


def test_confirmed_on_insert_carries_its_date():
    c = _Client()
    L.add_lesson(c, text="Janet ends rounds only when legal has seen it.", scope="person",
                 stakeholder_name="Janet Noe", company_id="ibx", status="confirmed")
    assert _writes(c, "insert")[0][2]["confirmed_at"]


def test_reconfirming_after_review_restarts_the_year():
    c = _Client()
    L.set_status(c, "L1", "confirmed")
    patch = _writes(c, "update")[0][2]
    assert patch["review_due"] == L._a_year_on(date.today()).isoformat()


def test_a_year_on_handles_leap_day():
    assert L._a_year_on(date(2028, 2, 29)) == date(2029, 2, 28)
    assert L._a_year_on(date(2026, 11, 11)) == date(2027, 11, 11)


def test_retire_keeps_the_reason():
    c = _Client()
    L.set_status(c, "L1", "retired", reason="Client changed their review process")
    assert _writes(c, "update")[0][2] == {"status": "retired",
                                          "retired_reason": "Client changed their review process"}


def test_edit_reembeds_on_text_change_only():
    c = _Client()
    out = L.edit_lesson(c, "L1", moments=["invoicing"], embed=lambda t: [0.2])
    assert "embedded" not in out
    out = L.edit_lesson(c, "L1", text="Expenses in before the last invoice goes out.", embed=lambda t: [0.2])
    assert out["embedded"] is True
    with pytest.raises(L.LessonError, match="cannot edit"):
        L.edit_lesson(c, "L1", status="confirmed")


def test_list_never_reads_the_embedding_and_filters():
    c = _Client()
    L.list_lessons(c, company_id="ibx", moments=["invoicing"], review_due_by=date(2027, 1, 1))
    _table, _kind, _payload, ops = c.calls[-1]
    cols = ops[0][1]
    assert "embedding," not in cols and not cols.endswith(" embedding")
    names = [o[0] for o in ops[1:]]
    assert names[:4] == ["eq", "eq", "overlaps", "lte"]


def test_search_passes_client_scope_to_the_rpc():
    c = _Client()
    L.search_lessons(c, [0.1, 0.2], company_id="ibx", moments=["travel"], limit=500)
    name, params = c.rpcs[0]
    assert name == "match_lessons"
    assert params["p_company_id"] == "ibx" and params["match_count"] == 50
    assert params["p_moments"] == ["travel"] and params["p_project_types"] is None
