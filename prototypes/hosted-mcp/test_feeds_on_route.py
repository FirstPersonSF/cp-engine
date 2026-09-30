"""Hosted routing proposes the feeds edge (#174).

`set_spine_element(serves=[...])` is the hosted routing act. When the work
item it routes to leads to a deliverable, the verb must leave a PROPOSED
`informs` edge for the Suggestions inbox — under the caller's email, never
`active` — and must not fail the routing if the proposal fails.

    python -m pytest prototypes/hosted-mcp/test_feeds_on_route.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_canonical_write_code import (  # noqa: E402
    CANONICAL,
    COMPANY_ID,
    PID,
    SHORT,
    FakeClient,
    _Query,
)


class _RouteQuery(_Query):
    """The shared fake plus the two ops this path needs: `in_` and `update`."""

    def in_(self, column, values):
        self._check(column)
        vals = {str(v) for v in values}
        self._filters.append(lambda r, c=column: str(r.get(c)) in vals)
        return self

    def update(self, patch):
        self._op, self._payload = "update", patch
        return self

    def execute(self):
        if self._op == "update":
            hit = [r for r in self._rows if all(f(r) for f in self._filters)]
            for r in hit:
                r.update(self._payload)
            return SimpleNamespace(data=[dict(r) for r in hit])
        return super().execute()


class _RouteClient(FakeClient):
    def table(self, name):
        return _RouteQuery(self, name)


@pytest.fixture
def server(monkeypatch):
    for k, v in {
        "SUPABASE_URL": "http://example.invalid",
        "SUPABASE_ANON_KEY": "x",
    }.items():
        monkeypatch.setenv(k, v)
    import server as mod

    return mod


def _row(eid, **kw):
    base = {
        "id": f"{CANONICAL}/{eid}/v1", "project_id": PID,
        "project_code": CANONICAL, "est_item_id": eid, "status": "live",
        "archived": False, "version_label": "v1", "serves": [], "sources": [],
        "field_states": {}, "important": False,
    }
    base.update(kw)
    return base


@pytest.fixture
def client(server, monkeypatch):
    c = _RouteClient({})
    c.store["projects"] = [{
        "id": PID, "code": "SLT-brand-campaign-26",
        "full_job_name": "SLT 5196 Brand Campaign 26",
        "company_id": COMPANY_ID, "number": 5196,
    }]
    c.store["companies"] = [{"id": COMPANY_ID, "code": "SLT"}]
    c.store["spine_substance"] = [
        _row("_authored/brief-doc", framing="Brand brief", layer="Source material",
             card_kind="attachment", placement="context",
             version_date="2026-08-01", sources=[{"id": "a1"}]),
        _row("act-uuid", framing="Shoot planning", layer="Activity",
             card_kind="activity", placement="item", version_date="2026-08-20"),
        _row("dlv-uuid", framing="Project Schedule", layer="Deliverables",
             card_kind="deliverable", placement="item", version_date="2026-09-01"),
    ]
    c.store["spine_relations"] = [{
        "id": "e1", "project_id": PID, "project_code": CANONICAL,
        "kind": "informs", "from_item_id": "act-uuid", "to_item_id": "dlv-uuid",
        "status": "active", "source": "manual",
    }]
    c.store["rag_assets"] = [{"id": "a1", "created_at": "2026-08-02T09:00:00Z"}]
    monkeypatch.setattr(server, "user_client", lambda: c)
    monkeypatch.setattr(server, "caller_subject", lambda: "user-sub-1")
    monkeypatch.setattr(server, "caller_email", lambda: "drew@firstperson.is")
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_paths_index_rows", lambda: {})
    monkeypatch.setattr(server, "upsert_auto_step", lambda *a, **k: {"ok": True})
    return c


def _proposed(c):
    return [r for r in c.store["spine_relations"] if r.get("status") == "proposed"]


def test_routing_to_an_activity_that_feeds_a_deliverable_proposes_the_edge(server, client):
    out = server.set_spine_element(
        project_code=SHORT, key="_authored/brief-doc", serves=["act-uuid"])
    assert "error" not in out, out
    (edge,) = _proposed(client)
    assert (edge["kind"], edge["from_item_id"], edge["to_item_id"]) == (
        "informs", "_authored/brief-doc", "dlv-uuid")
    assert edge["source"] == "auto_ingest"
    assert edge["created_by"] == "drew@firstperson.is"
    assert edge["confidence"] < 0.8
    # Nothing active was minted — the only active edge is the seeded one.
    assert [r["id"] for r in client.store["spine_relations"]
            if r.get("status") == "active"] == ["e1"]
    assert [p["to_item_id"] for p in out["feeds_proposed"]["proposed"]] == ["dlv-uuid"]


def test_a_document_newer_than_the_activity_is_not_proposed(server, client):
    client.store["rag_assets"] = [{"id": "a1", "created_at": "2026-08-25T09:00:00Z"}]
    out = server.set_spine_element(
        project_code=SHORT, key="_authored/brief-doc", serves=["act-uuid"])
    assert "error" not in out, out
    assert _proposed(client) == []
    assert [s["reason"] for s in out["feeds_proposed"]["skipped"]] == ["postdates"]


def test_a_failed_proposal_never_fails_the_routing(server, client, monkeypatch):
    import cp_engine.feeds_propose as fp

    def boom(*a, **k):
        raise RuntimeError("relations read failed")

    monkeypatch.setattr(fp, "propose_on_route", boom)
    out = server.set_spine_element(
        project_code=SHORT, key="_authored/brief-doc", serves=["act-uuid"])
    assert "error" not in out, out
    assert out["serves"] == ["act-uuid"]
    assert "relations read failed" in out["feeds_proposed"]["error"]


def test_unrouting_proposes_nothing(server, client):
    out = server.set_spine_element(
        project_code=SHORT, key="_authored/brief-doc", serves=[])
    assert "error" not in out, out
    assert _proposed(client) == []
