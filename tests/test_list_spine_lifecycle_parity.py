# tests/test_list_spine_lifecycle_parity.py — both servers' `list_spine_elements`
# give the same lifecycle answer for the same spine (#334, #335).
#
# Two drifts, found while fixing #330:
#
#   #334 — `absorbed_hidden` counted different things. Stdio counted sealed rows
#          among those its filters would list; hosted checked the seal BEFORE
#          its `tier` facet, so under tier="working" a sealed source stub (which
#          the tier hides anyway) was reported as hidden by the seal. The count
#          must answer "how many of the rows you asked for did I hide".
#   #335 — hosted badges canon members (an active `canon_of` edge) `canon: true`
#          and totals `canon_size`; stdio said nothing about canon.
#
# One fixture — rows and edges served by one fake PostgREST — goes through the
# REAL verbs of both servers, and every facet combination is compared. The
# expected hidden counts are asserted too, so the two cannot agree on a wrong
# number.
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("jwt")
pytest.importorskip("mcp")
pytest.importorskip("supabase")

import cp_engine.mcp_server as stdio  # noqa: E402
import cp_engine.project_sources as ps  # noqa: E402

_SERVER_PATH = (
    Path(__file__).resolve().parents[1] / "prototypes" / "hosted-mcp" / "server.py"
)


@pytest.fixture(scope="module")
def hosted():
    os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
    os.environ.setdefault("SUPABASE_ANON_KEY", "anon-key-for-tests")
    spec = importlib.util.spec_from_file_location("hosted_mcp_server_parity", _SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(eid, layer, *, pid="p1", scope="project", cid="co"):
    return {"id": f"{pid}/{eid}/v1", "est_item_id": eid, "framing": eid,
            "layer": layer, "binding": "unbound", "status": "live",
            "serves": [], "body": "b", "important": False, "note": None,
            "archived": False, "scope": scope, "company_id": cid,
            "project_id": pid, "version_label": "v1",
            "version_date": "2026-09-01", "synced_at": None, "actor": None}


def _edge(frm, to, kind, *, pid="p1", status="active"):
    return {"kind": kind, "from_item_id": frm, "to_item_id": to,
            "project_id": pid, "status": status}


_ROWS = [
    _row("_authored/plain-note", "Note"),
    _row("_authored/sealed-note", "Note"),
    _row("_authored/sealed-stub", "Source material"),
    _row("_authored/plain-stub", "Source material"),
    _row("_authored/deck", "Deliverables"),
    _row("_authored/canon-decision", "Decisions"),
    # Promoted off p9: its seal lives under its HOME project.
    _row("_authored/fred", "Stakeholders", pid="p9", scope="account"),
]
_EDGES = [
    _edge("_authored/sealed-note", "_authored/deck", "absorbed_by"),
    _edge("_authored/sealed-stub", "_authored/deck", "absorbed_by"),
    _edge("_authored/fred", "_authored/deck", "absorbed_by", pid="p9"),
    _edge("_authored/canon-decision", "_authored/brief", "canon_of"),
    _edge("_authored/old", "_authored/deck", "absorbed_by", status="retired"),
]


class _Q:
    def __init__(self, rows):
        self._rows, self._f = rows, []

    def select(self, cols, *a, **k):
        assert "*" not in cols  # never SELECT *
        return self

    def eq(self, col, val):
        self._f.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        vals = set(vals)
        self._f.append(lambda r: r.get(col) in vals)
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return SimpleNamespace(
            data=[dict(r) for r in self._rows if all(f(r) for f in self._f)])


class _DB:
    tables = {
        "projects": [{"id": "p1", "company_id": "co", "mc_status": "Open"}],
        "spine_substance": _ROWS,
        "spine_relations": _EDGES,
    }

    def table(self, name):
        return _Q(self.tables.get(name, []))


@pytest.fixture
def servers(hosted, monkeypatch):
    db = _DB()
    monkeypatch.setattr(hosted, "user_client", lambda: db)
    monkeypatch.setattr(hosted, "caller_subject", lambda: "u")
    monkeypatch.setattr(hosted, "audit", lambda *a, **k: None)
    monkeypatch.setattr(hosted, "resolve_project_id", lambda _c, code: "p1")
    monkeypatch.setattr(hosted, "_with_project_status", lambda out, *a: out)
    monkeypatch.setattr(stdio, "_resolve", lambda code: (db, "p1", "co"))
    monkeypatch.setattr(stdio, "_with_project_status", lambda rows, *a: rows)
    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    return hosted


def _stdio_view(rows):
    elements = [r for r in rows if "est_item_id" in r]
    notes = [r for r in rows if "est_item_id" not in r]
    assert len(notes) <= 1, notes
    note = notes[0] if notes else {}
    return {
        "ids": {r["est_item_id"] for r in elements},
        "absorbed_by": {r["est_item_id"]: r["absorbed_by"]
                        for r in elements if "absorbed_by" in r},
        "absorbed_hidden": note.get("absorbed_hidden", 0),
    }


def _hosted_view(out):
    elements = out["elements"]
    return {
        "ids": {e["slug"] for e in elements},
        "absorbed_by": {e["slug"]: e["absorbed_by"]
                        for e in elements if "absorbed_by" in e},
        "absorbed_hidden": out.get("absorbed_hidden", 0),
    }


# Sealed rows each tier would otherwise list: all three; the note and the
# account stakeholder (the stub is the tier's to hide); the stub alone.
_EXPECTED_HIDDEN = {"": 3, "working": 2, "stubs": 1}


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("include_absorbed", [False, True])
@pytest.mark.parametrize("tier", sorted(_EXPECTED_HIDDEN))
def test_both_servers_give_the_same_lifecycle_answer(servers, tier,
                                                     include_absorbed, compact):
    s = _stdio_view(stdio.list_spine_elements(
        "ibx-5192", tier=tier, compact=compact, include_absorbed=include_absorbed))
    h = _hosted_view(servers.list_spine_elements(
        "ibx-5192", tier=tier, compact=compact, include_absorbed=include_absorbed))
    assert s == h
    assert s["absorbed_hidden"] == (0 if include_absorbed else _EXPECTED_HIDDEN[tier])
