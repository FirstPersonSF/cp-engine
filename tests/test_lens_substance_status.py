"""The Lens status term on the LIVE (substance) path is not a constant (#315).

`score = recency × serves_active × layer_importance × status`. On the path that
actually runs — `load_spine_from_mc2` → `substance_row_to_element` — every row
came back `active`, because the status term's inputs (`stage`, `depends_on`)
have no substance column. So the term was always 1.0 and `derive_status`'s
auto-demotion never fired.

Substance DOES record one lifecycle fact: an active `absorbed_by` edge — the
element was sealed into a deliverable that shipped (spec v04 §3, "only
relevant in retrospect"). That is the Lens's `reference`, so it now reaches the
status term. `final`/`dormant` stay disk-path only, and a test here says so.

Also pinned: `actor` (mig 126) reaches the sweep — its first reader.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from cp_engine.spine import (
    load_spine_from_mc2,
    rank_elements,
    render_sweep,
    substance_row_to_element,
)
from cp_engine.spine_sweep import build_sweep_prompt

TODAY = date(2026, 9, 30)
PID = "11111111-1111-4111-8111-111111111111"


def _row(slug, **kw):
    r = {
        "id": f"p/_authored/{slug}/v1", "project_id": PID, "project_code": "p",
        "est_item_id": f"_authored/{slug}", "layer": "Synthesis",
        "placement": "context", "status": "live", "version_label": "v1",
        "version_date": "2026-09-28", "framing": slug.replace("-", " "),
        "body": "b", "serves": [], "sources": [], "rel_path": None,
        "actor": "inferred",
    }
    r.update(kw)
    return r


class _Q:
    def __init__(self, client, table):
        self.c, self.table = client, table
        self.filters = []

    def select(self, cols):
        self.c.selects[self.table] = cols
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self.filters.append(("in", col, tuple(vals)))
        return self

    def execute(self):
        if self.table == "spine_relations":
            if self.c.edges_fail:
                raise RuntimeError("42501 permission denied for table spine_relations")
            return SimpleNamespace(data=self.c.edges)
        return SimpleNamespace(data=self.c.rows)


class _Client:
    def __init__(self, rows, edges=(), edges_fail=False):
        self.rows, self.edges, self.edges_fail = rows, list(edges), edges_fail
        self.selects: dict[str, str] = {}

    def table(self, name):
        return _Q(self, name)


def _absorbed(src, into="_authored/the-deck"):
    return {"kind": "absorbed_by", "from_item_id": src, "to_item_id": into}


def test_a_sealed_element_loads_as_reference_and_ranks_below_its_twin():
    """CONTROL: against the unfixed loader both rows are `active` with equal
    scores — the status term was a constant."""
    rows = [_row("sealed-input"), _row("live-input")]
    client = _Client(rows, edges=[_absorbed("_authored/sealed-input")])
    els = {e.title: e for e in load_spine_from_mc2(client, "p")}

    assert els["sealed input"].status == "reference"
    assert els["live input"].status == "active"

    scored, effective = rank_elements(tuple(els.values()), today=TODAY)
    score = {e.title: s for s, e in scored}
    assert score["sealed input"] < score["live input"]
    assert effective[els["sealed input"].id] == "reference"
    assert "(reference)" in render_sweep("p", tuple(els.values()), today=TODAY)


def test_edges_are_read_for_the_rows_own_projects_and_select_is_explicit():
    client = _Client([_row("a")])
    load_spine_from_mc2(client, "p")
    assert "*" not in client.selects["spine_substance"]
    assert "actor" in client.selects["spine_substance"]
    assert "project_id" in client.selects["spine_substance"]
    assert "spine_relations" in client.selects


def test_a_failed_edge_read_degrades_loudly_not_silently(capsys):
    """An empty seal map looks exactly like "nothing was ever sealed", so the
    failure must be SAID — and must not take the whole MC-2 read down with it
    (that would fall back to last-known disk state)."""
    els = load_spine_from_mc2(_Client([_row("a")], edges_fail=True), "p")
    assert [e.status for e in els] == ["active"]
    assert "lifecycle edges unreadable" in capsys.readouterr().err


def test_final_and_dormant_are_not_reachable_from_substance():
    """Honest limit, pinned so nobody assumes otherwise: substance has no
    `stage` or `depends_on`, so a live row maps only to active/reference."""
    for sealed in (False, True):
        el = substance_row_to_element(_row("x"), sealed=sealed)
        assert el.stage is None and el.depends_on == ()
        assert el.status in {"active", "reference"}


def test_actor_reaches_the_sweep_prompt_and_the_display():
    els = (
        substance_row_to_element(_row("janet-said", actor="client")),
        substance_row_to_element(_row("drew-directs", actor="partner")),
        substance_row_to_element(_row("a-note")),
    )
    prompt = build_sweep_prompt("p", els, today=TODAY)
    assert "actor=client" in prompt and "actor=partner" in prompt
    assert "actor=inferred" not in prompt  # the default is not a statement
    assert "advise, they never override" in prompt
    shown = render_sweep("p", els, today=TODAY)
    assert "<client>" in shown and "<inferred>" not in shown


def test_an_untagged_spine_gets_no_precedence_instruction():
    els = (substance_row_to_element(_row("a")),)
    assert "never override" not in build_sweep_prompt("p", els, today=TODAY)
