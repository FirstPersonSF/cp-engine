"""The morning spine fix: binds only what the spine already answers.

The failure that matters is a GUESS: an element bound to work nobody linked
it to, which hides a real gap behind a green line. So most tests here are
the cases that must be left alone.
"""
from __future__ import annotations

from cp_engine import daily_health as dh
from cp_engine import spine_autofix as sa

P = "p1"


def _row(eid, *, status="live", archived=False, serves=(), layer="Synthesis"):
    return {"project_id": P, "est_item_id": eid, "status": status,
            "archived": archived, "serves": list(serves), "layer": layer}


def _edge(kind, frm, to):
    return {"project_id": P, "kind": kind, "from_item_id": frm, "to_item_id": to,
            "status": "active"}


FLOAT = [{"project_id": P, "est_item_id": "new"}]
DELIV = _row("deliv", layer="Deliverables")


def test_successor_inherits_what_its_predecessor_served():
    fixes = sa.plan(FLOAT, [_edge("supersedes", "new", "old")],
                    [_row("old", status="superseded", serves=["deliv"]), DELIV])
    assert [(f.est_item_id, f.serves) for f in fixes] == [("new", ["deliv"])]
    assert "old" in fixes[0].reason


def test_estimate_work_item_targets_are_inherited():
    """`serves` usually holds estimate item ids, which have no spine row."""
    fixes = sa.plan(FLOAT, [_edge("supersedes", "new", "old")],
                    [_row("old", status="superseded", serves=["ef7b-uuid"])])
    assert fixes[0].serves == ["ef7b-uuid"]


def test_read_fetches_the_predecessors_serve_targets():
    """A dead target must be SEEN to be dropped: _read loads it."""
    calls = []

    class Q:
        def __init__(self, name):
            self.name, self.ids = name, None

        def select(self, *a):
            return self

        def eq(self, *a):
            return self

        def limit(self, *a):
            return self

        def in_(self, col, vals):
            if col == "est_item_id":
                self.ids = set(vals)
                calls.append(self.ids)
            return self

        def execute(self):
            if self.name == "spine_relations":
                data = [_edge("supersedes", "new", "old")]
            elif self.ids == {"new", "old"}:
                data = [_row("old", status="superseded", serves=["gone"])]
            else:
                data = [_row("gone", archived=True)]
            return type("R", (), {"data": data})()

    client = type("C", (), {"table": lambda self, n: Q(n)})()
    rel, sub = sa._read(client, {(P, "new")})
    assert {"gone"} in calls
    assert sa.plan(FLOAT, rel, sub) == []


def test_successor_takes_predecessors_place_in_the_brief():
    fixes = sa.plan(FLOAT, [_edge("supersedes", "new", "old"),
                            _edge("canon_of", "old", "_authored/inputs-briefing")],
                    [_row("old")])
    assert fixes[0].canon_brief == "_authored/inputs-briefing"
    assert fixes[0].canon_from == "old" and fixes[0].serves == []


def test_confirmed_informs_edge_to_a_live_deliverable_binds():
    fixes = sa.plan(FLOAT, [_edge("informs", "new", "deliv")], [DELIV])
    assert fixes[0].serves == ["deliv"]


# ── left alone: these stay on the line for a person ───────────────────────


def test_no_edges_is_left_alone():
    """The 2026-11-03 SAP RFP: nothing in the spine says what it serves."""
    assert sa.plan(FLOAT, [], []) == []


def test_predecessor_that_was_itself_unbound_gives_nothing():
    """The 2026-11-03 StoryOS Metrics Library: it replaced an unbound note."""
    assert sa.plan(FLOAT, [_edge("supersedes", "new", "old")], [_row("old")]) == []


def test_dead_serve_target_is_not_inherited():
    fixes = sa.plan(FLOAT, [_edge("supersedes", "new", "old")],
                    [_row("old", serves=["gone"]), _row("gone", archived=True,
                                                        layer="Deliverables")])
    assert fixes == []


def test_informs_to_a_non_deliverable_does_not_bind():
    fixes = sa.plan(FLOAT, [_edge("informs", "new", "note")], [_row("note")])
    assert fixes == []


def test_predecessor_versions_that_disagree_are_not_picked_between():
    fixes = sa.plan(FLOAT, [_edge("supersedes", "new", "old")],
                    [_row("old", status="superseded", serves=["deliv"]),
                     _row("old", status="superseded", serves=["deliv2"]),
                     DELIV, _row("deliv2", layer="Deliverables")])
    assert fixes == []


def test_another_elements_edges_do_not_leak():
    fixes = sa.plan(FLOAT, [_edge("informs", "other", "deliv")], [DELIV])
    assert fixes == []


# ── the health line ───────────────────────────────────────────────────────


class _Q:
    def __init__(self, rows):
        self.rows = rows

    def __getattr__(self, name):
        return lambda *a, **k: self

    def execute(self):
        return type("R", (), {"data": self.rows})()


class _Client:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Q(self.tables.get(name, []))


UNBOUND = [
    {"project_id": "a", "est_item_id": "fixable", "important": True, "serves": [],
     "layer": "Synthesis"},
    {"project_id": "a", "est_item_id": "needs-a-person", "important": True,
     "serves": [], "layer": "Brief"},
]


def test_line_names_only_what_the_fix_could_not_clear(monkeypatch):
    seen = {}

    def fake_run(client, *, floating, apply, today):
        seen.update(apply=apply, keys={f["est_item_id"] for f in floating})
        return {"fixed": [{"project_id": "a", "est_item_id": "fixable", "serves": ["d"],
                           "canon_brief": None, "reason": "r"}], "failed": []}

    monkeypatch.setattr(sa, "run", fake_run)
    c = dh.check_unbound(_Client({"spine_substance": UNBOUND}), fix=True, today="2026-11-03")
    assert seen == {"apply": True, "keys": {"fixable", "needs-a-person"}}
    assert c.detail["floating"] == 1 and not c.ok
    assert [e["est_item_id"] for e in c.detail["floating_elements"]] == ["needs-a-person"]
    assert c.detail["fixed"][0]["est_item_id"] == "fixable"
    assert "fixable" not in c.render()


def test_everything_fixed_reads_clear(monkeypatch):
    monkeypatch.setattr(sa, "run", lambda client, **k: {
        "fixed": [{"project_id": "a", "est_item_id": e["est_item_id"]} for e in k["floating"]],
        "failed": []})
    c = dh.check_unbound(_Client({"spine_substance": UNBOUND}), fix=True, today="2026-11-03")
    assert c.ok and c.detail["floating"] == 0 and c.detail["floating_closed"] == 0


def test_without_fix_nothing_is_written_and_both_are_named(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a plain `cxp health` must not write")

    monkeypatch.setattr(sa, "run", boom)
    c = dh.check_unbound(_Client({"spine_substance": UNBOUND}))
    assert c.detail["floating"] == 2 and "fixed" not in c.detail


def test_a_failed_fix_keeps_the_element_and_reaches_the_log(monkeypatch):
    monkeypatch.setattr(sa, "run", lambda client, **k: {
        "fixed": [], "failed": [{"project_id": "a", "est_item_id": "fixable",
                                 "error": "APIError: 42501"}]})
    c = dh.check_unbound(_Client({"spine_substance": UNBOUND}), fix=True, today="2026-11-03")
    assert c.detail["floating"] == 2
    rep = dh.HealthReport(when=None, checks=[c])
    assert "42501" in rep.errors()["Spine unbound fix"]
    assert "42501" not in c.render()


# ── the write ─────────────────────────────────────────────────────────────


class _RecQ(_Q):
    def __init__(self, rows, log, table):
        super().__init__(rows)
        self.log, self.table = log, table

    def update(self, patch):
        self.log.append((self.table, "update", patch))
        return self

    def insert(self, row):
        self.log.append((self.table, "insert", row))
        return self

    def delete(self):
        self.log.append((self.table, "delete", None))
        return self


class _RecClient:
    def __init__(self, tables):
        self.tables, self.log = tables, []

    def table(self, name):
        return _RecQ(self.tables.get(name, []), self.log, name)


def test_apply_marks_serves_confirmed_and_moves_the_brief_seat(monkeypatch):
    from cp_engine import spine_steps
    steps = []
    monkeypatch.setattr(spine_steps, "upsert_auto_step",
                        lambda *a, **k: steps.append(a[3]))
    client = _RecClient({"spine_substance": [{"id": "row1", "field_states": {"layer": "confirmed"},
                                              "project_code": "x"}]})
    err = sa._apply(client, sa.Fix(P, "new", ["deliv"], "brief", "old", "r"), today="2026-11-03")
    assert err is None
    upd = next(p for t, op, p in client.log if op == "update")
    assert upd["serves"] == ["deliv"] and upd["binding"] == "live"
    assert upd["field_states"] == {"layer": "confirmed", "serves": "confirmed"}
    ins = next(p for t, op, p in client.log if op == "insert")
    assert (ins["kind"], ins["from_item_id"], ins["to_item_id"]) == ("canon_of", "new", "brief")
    assert any(op == "delete" for _, op, _ in client.log)
    assert steps and steps[0].startswith("Bound by the morning spine fix")
