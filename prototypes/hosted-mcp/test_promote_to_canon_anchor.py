"""`promote_to_canon` creates the canon anchor when it is missing (#351).

WHAT BROKE. The canon anchors on the standing Inputs & Briefing element
(`_authored/inputs-briefing`). A workstream whose spine predates the standing-
element seed — or whose brief lived under another slug — had none, so every
promotion refused ("scaffold/author it first") and no verb could scaffold it.
On 10-05 seven approved canon rules on four Google jobs refused; on 10-07 the
anchors were hand-made with `create_spine_element` and boilerplate.

THE FIX. A missing anchor is created on promotion with the same six-section
scaffold MC-2 seeds at spine creation, then the promotion proceeds. The result
says so (`scaffolded_anchor: true` + a note), so it is never silent. An anchor
that EXISTS but has no live version is still a refusal.

    python -m pytest prototypes/hosted-mcp/test_promote_to_canon_anchor.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_canonical_write_code import (  # noqa: E402
    CANONICAL,
    COMPANY_ID,
    PID,
    SHORT,
    FakeClient,
    _columns,
)

BRIEF = "_authored/inputs-briefing"
MEMBER = "_authored/rule-one"


@pytest.fixture
def server(monkeypatch):
    for k, v in {
        "SUPABASE_URL": "http://example.invalid",
        "SUPABASE_ANON_KEY": "x",
    }.items():
        monkeypatch.setenv(k, v)
    import server as mod

    return mod


@pytest.fixture
def client(server, monkeypatch):
    known = {
        "spine_substance": _columns(server.SPINE_PULL_COLUMNS)
        | _columns(server._ELEMENT_RESOLVE_COLUMNS),
    }
    c = FakeClient(known)
    c.store["projects"] = [{
        "id": PID, "code": "SLT-brand-campaign-26",
        "full_job_name": "SLT 5196 Brand Campaign 26",
        "company_id": COMPANY_ID, "number": 5196,
    }]
    c.store["companies"] = [{"id": COMPANY_ID, "code": "SLT"}]
    monkeypatch.setattr(server, "user_client", lambda: c)
    monkeypatch.setattr(server, "caller_subject", lambda: "user-sub-1")
    monkeypatch.setattr(server, "caller_email", lambda: "drew@example.com")
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_paths_index_rows", lambda: {})
    monkeypatch.setattr(server, "upsert_auto_step", lambda *a, **k: {"ok": True})
    return c


def _row(est_item_id, framing, layer, status="live", body="x"):
    return {
        "id": f"{CANONICAL}/{est_item_id}/v1", "project_id": PID,
        "project_code": CANONICAL, "est_item_id": est_item_id,
        "phase": None, "binding": "unbound", "layer": layer,
        "placement": "context", "serves": [], "version_label": "v1",
        "version_date": "2026-09-01", "status": status,
        "framing": framing, "body": body, "sources": [], "origin": "authored",
        "important": False, "note": None, "scope": None, "company_id": None,
    }


def _anchors(client):
    return [r for r in client.store.get("spine_substance", []) if r["est_item_id"] == BRIEF]


def _canon_edges(client):
    return [r for r in client.store.get("spine_relations", []) if r["kind"] == "canon_of"]


def test_missing_anchor_is_scaffolded_and_the_promotion_lands(server, client):
    client.store["spine_substance"] = [_row(MEMBER, "Rule one", "Decisions")]

    out = server.promote_to_canon(project_code=SHORT, key=MEMBER)

    assert "error" not in out, out
    # The anchor exists now, live, as the standing brief with the six headings.
    (anchor,) = _anchors(client)
    assert anchor["status"] == "live"
    assert anchor["framing"] == "Inputs & Briefing"
    assert anchor["layer"] == "Brief"
    assert anchor["project_code"] == CANONICAL
    for heading in ("## Objective", "## Audience", "## The problem",
                    "## Constraints", "## Inputs", "## Key dates"):
        assert heading in anchor["body"]
    # The promotion went through against it.
    (edge,) = _canon_edges(client)
    assert edge["from_item_id"] == MEMBER and edge["to_item_id"] == BRIEF
    assert out["promoted"] == MEMBER and out["canon_anchor"] == BRIEF
    assert out["created"] is True
    # Never silent.
    assert out["scaffolded_anchor"] is True
    assert "author" in out["anchor_note"].lower()


def test_existing_anchor_is_used_unchanged(server, client):
    client.store["spine_substance"] = [
        _row(BRIEF, "Inputs & Briefing", "Brief", body="Real brief."),
        _row(MEMBER, "Rule one", "Decisions"),
    ]

    out = server.promote_to_canon(project_code=SHORT, key=MEMBER)

    assert "error" not in out, out
    (anchor,) = _anchors(client)  # no second anchor
    assert anchor["body"] == "Real brief."
    assert "scaffolded_anchor" not in out
    assert "anchor_note" not in out
    (edge,) = _canon_edges(client)
    assert edge["to_item_id"] == BRIEF


def test_unresolvable_member_creates_no_anchor(server, client):
    """A bad key must not leave a stray scaffold behind."""
    client.store["spine_substance"] = [_row(MEMBER, "Rule one", "Decisions")]

    out = server.promote_to_canon(project_code=SHORT, key="_authored/no-such-thing")

    assert "promoted" not in out
    assert _anchors(client) == []
    assert _canon_edges(client) == []


def test_anchor_with_no_live_version_still_refuses(server, client):
    """A retired brief is a genuine error — not something to paper over."""
    client.store["spine_substance"] = [
        _row(BRIEF, "Inputs & Briefing", "Brief", status="superseded"),
        _row(MEMBER, "Rule one", "Decisions"),
    ]

    out = server.promote_to_canon(project_code=SHORT, key=MEMBER)

    assert "error" in out
    assert len(_anchors(client)) == 1
    assert _canon_edges(client) == []
