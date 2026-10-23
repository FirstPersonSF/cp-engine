"""Hosted list/pull say when an element has been REPLACED (#352 item 1).

WHAT BROKE. 08-26, ibx-5153: a fresh agent drafted against `Narrative
Messaging Architecture` (July, v1), which a client redirect on 08-24 had
withdrawn. A `supersedes` edge (new -> old) is how the tenant records that,
and the hosted `list_spine_elements` / `pull_spine_element` never read it —
only `list_spine_relations`, asked explicitly, did. The replaced element read
exactly like current truth.

WHAT THESE PIN. An incoming ACTIVE `supersedes` edge from a LIVE element puts
`superseded_by: {key, framing, date}` on the listed row and leads the pull
with a one-line `warning`. An active `contradicts` edge marks its target
`contradicted_by`. A retired edge or a dead successor marks nothing.

    python -m pytest prototypes/hosted-mcp/test_supersession_352.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_project_status_reads import _Q  # noqa: E402

PID = "p-ibx"


def _row(eid, framing, vdate="2026-07-21"):
    return {"id": f"s-{eid}", "project_id": PID, "est_item_id": eid,
            "framing": framing, "layer": "Synthesis", "status": "live",
            "body": f"body of {framing}", "project_code": "ibx-5153",
            "version_date": vdate, "archived": False}


def _edge(kind, frm, to, created, status="active", note=None):
    return {"id": f"r-{frm}-{to}", "project_id": PID, "kind": kind,
            "from_item_id": frm, "to_item_id": to, "status": status,
            "created_at": created, "note": note}


class _DB:
    def __init__(self):
        self.tables = {
            "projects": [{"id": PID, "mc_status": "Open", "company_id": None}],
            "spine_substance": [
                _row("_authored/narrative-messaging-architecture",
                     "Narrative Messaging Architecture"),
                _row("_authored/janet-redirect-08-24",
                     "Janet redirect — drop the pillar gate (08-24)",
                     "2026-08-26"),
                _row("_authored/talking-points", "Talking points v02",
                     "2026-10-16"),
                _row("_authored/plain", "Untouched card"),
            ],
            "spine_relations": [
                _edge("supersedes", "_authored/janet-redirect-08-24",
                      "_authored/narrative-messaging-architecture",
                      "2026-08-25T12:00:00Z", note="redirect withdrew it"),
                _edge("supersedes", "_authored/janet-redirect-08-24",
                      "_authored/plain", "2026-08-25T12:00:00Z",
                      status="retired"),
                _edge("supersedes", "_authored/retired-thing",
                      "_authored/talking-points", "2026-09-01T00:00:00Z"),
                _edge("contradicts", "_authored/janet-redirect-08-24",
                      "_authored/talking-points", "2026-10-19T00:00:00Z",
                      note="reset, $49.5K"),
            ],
        }

    def table(self, name):
        return _Q(self.tables.get(name, []))


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "http://example.invalid")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "x")
    import server as mod

    db = _DB()
    monkeypatch.setattr(mod, "user_client", lambda: db)
    monkeypatch.setattr(mod, "caller_subject", lambda: "u")
    monkeypatch.setattr(mod, "audit", lambda *a, **k: None)
    monkeypatch.setattr(mod, "resolve_project_id", lambda _c, code: code)
    monkeypatch.setattr(mod, "resolve_company_id", lambda _c, _p: None)
    monkeypatch.setattr(
        mod, "read_spine_rows",
        lambda c, pid, cols: [dict(r) for r in db.tables["spine_substance"]
                              if r["project_id"] == pid],
    )
    return mod


NMA = "_authored/narrative-messaging-architecture"


@pytest.mark.parametrize("compact", [False, True])
def test_listing_marks_the_replaced_element(server, compact):
    out = server.list_spine_elements(PID, compact=compact)
    by = {e["slug"]: e for e in out["elements"]}
    assert by[NMA]["superseded_by"] == {
        "key": "_authored/janet-redirect-08-24",
        "framing": "Janet redirect — drop the pillar gate (08-24)",
        "date": "2026-08-25",
    }
    # Retired edge; successor not live; the successor itself: no marker.
    for slug in ("_authored/plain", "_authored/talking-points",
                 "_authored/janet-redirect-08-24"):
        assert "superseded_by" not in by[slug]
    assert out["superseded_count"] == 1


def test_listing_marks_contradicted_targets(server):
    by = {e["slug"]: e for e in server.list_spine_elements(PID)["elements"]}
    assert [c["key"] for c in by["_authored/talking-points"]["contradicted_by"]] == [
        "_authored/janet-redirect-08-24"]
    assert "contradicted_by" not in by[NMA]


def test_pull_leads_with_the_warning(server):
    out = server.pull_spine_element(key=NMA, project_code=PID)
    assert next(iter(out)) == "warning", list(out)[:3]
    assert out["warning"] == (
        "Superseded by 'Janet redirect — drop the pillar gate (08-24)' "
        "(_authored/janet-redirect-08-24) on 2026-08-25 — read that first.")
    assert out["superseded_by"]["key"] == "_authored/janet-redirect-08-24"
    assert out["superseded_by"]["note"] == "redirect withdrew it"
    assert out["body"] == "body of Narrative Messaging Architecture"


def test_pull_of_a_current_element_is_unchanged(server):
    out = server.pull_spine_element(key="_authored/plain", project_code=PID)
    assert "warning" not in out
    assert "superseded_by" not in out and "contradicted_by" not in out


def test_pull_reports_contradictions(server):
    out = server.pull_spine_element(key="_authored/talking-points",
                                    project_code=PID)
    assert "warning" not in out
    assert out["contradicted_by"] == [{
        "key": "_authored/janet-redirect-08-24",
        "framing": "Janet redirect — drop the pillar gate (08-24)",
        "date": "2026-10-19", "note": "reset, $49.5K"}]
