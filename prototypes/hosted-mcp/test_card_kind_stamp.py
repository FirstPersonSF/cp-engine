"""Every hosted INSERT into `spine_substance` stamps `card_kind` (#315).

WHAT BROKE. `card_kind` (mc-2 mig 140) is read by `card_class.classify`, which reads
NULL as "not work" (as the retired `route_queue` / `weekly_sort` did). The cxp write paths began stamping it in #246, but the hosted server
builds its INSERT rows by hand and none of them carried the column. Measured
live 2026-09-30: 29 of the 38 live NULL-kind rows carried an `author_id` — the
hosted create path's signature. `add_spine_version` was worse: it rebuilt the
row from the base and dropped `card_kind`, `actor` and `lifetime`, so every
version bump silently reset a deliberate tag.

WHY A STRUCTURAL CHECK AS WELL AS BEHAVIOURAL ONES. The behavioural tests pin
the verbs that exist today. The structural one fails the next time someone
adds an INSERT site and builds the row by hand again — which is how all four
of these came to exist.

    python -m pytest prototypes/hosted-mcp/test_card_kind_stamp.py -v
"""
from __future__ import annotations

import ast
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

_SERVER = Path(__file__).resolve().parent / "server.py"


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
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_paths_index_rows", lambda: {})
    monkeypatch.setattr(server, "upsert_auto_step", lambda *a, **k: {"ok": True})
    return c


# ── structural: no INSERT site may build a row without the stamp ──────────


def _inserts_into_spine_substance(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "insert"):
            continue
        for inner in ast.walk(node.func.value):
            if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "table" and inner.args
                    and isinstance(inner.args[0], ast.Constant)
                    and inner.args[0].value == "spine_substance"):
                return True
    return False


def _writes_card_kind(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Dict) and any(
            isinstance(k, ast.Constant) and k.value == "card_kind" for k in node.keys
        ):
            return True
        if (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store)
                and isinstance(node.slice, ast.Constant) and node.slice.value == "card_kind"):
            return True
    return False


def test_every_spine_substance_insert_site_stamps_card_kind():
    tree = ast.parse(_SERVER.read_text(encoding="utf-8"))
    sites = [
        fn for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef) and _inserts_into_spine_substance(fn)
    ]
    # Derived, not listed — but an empty derivation would make this vacuous.
    assert len(sites) >= 4, [f.name for f in sites]
    missing = sorted(f.name for f in sites if not _writes_card_kind(f))
    assert not missing, (
        f"{missing} INSERT into spine_substance without writing card_kind. "
        "NULL reads as 'not work' to card_class.classify; route the row "
        "through _stamp_card_kind (#315)."
    )


# ── the stamp itself ──────────────────────────────────────────────────────


def test_stamp_agrees_with_the_reader_and_declines_without_placement(server):
    from cp_engine.card_class import classify

    authored = {"est_item_id": "_authored/a-decision", "layer": "Decisions",
                "placement": "context", "body": "x" * 300, "sources": []}
    assert server._stamp_card_kind(authored) == classify(authored).value == "reference"
    deliv = dict(authored, layer="Deliverables")
    assert server._stamp_card_kind(deliv) == "deliverable"
    item = {"est_item_id": "3f1c9a2e-0000-4000-8000-000000000001",
            "layer": "Activity", "placement": "item"}
    assert server._stamp_card_kind(item) == "activity"
    # No placement: the one genuinely ambiguous case — left for a human.
    assert server._stamp_card_kind({"est_item_id": "x", "layer": "Email"}) is None


# ── behavioural: the verbs ────────────────────────────────────────────────


def test_create_spine_element_stamps_card_kind(server, client):
    out = server.create_spine_element(
        project_code=SHORT, framing="Participant: Erik Hagen",
        body="Interview notes, long enough to be authored thinking. " * 5,
    )
    assert "error" not in out, out
    (row,) = client.store["spine_substance"]
    assert row["card_kind"] == "reference"


def _seed_live(client, **extra):
    base = {
        "id": f"{CANONICAL}/_authored/brief-note/v1", "project_id": PID,
        "project_code": CANONICAL, "est_item_id": "_authored/brief-note",
        "phase": None, "binding": "unbound", "layer": "Note",
        "placement": "context", "serves": [], "version_label": "v1",
        "version_date": "2026-09-01", "status": "live",
        "framing": "Brief note", "sources": [], "origin": "authored",
        "important": False, "note": None, "scope": None, "company_id": None,
    }
    base.update(extra)
    client.store["spine_substance"] = [base]


def _new_version(client):
    return next(r for r in client.store["spine_substance"] if r["version_label"] == "v2")


def test_add_spine_version_carries_a_stored_kind_and_the_tags(server, client):
    """A stored kind may be a human decision (`link`): the new version keeps
    it rather than re-deriving. `actor` and `lifetime` ride forward too."""
    _seed_live(client, card_kind="link", actor="client", lifetime="canon")
    out = server.add_spine_version(
        project_code=SHORT, element_id="_authored/brief-note", body="v2 body " * 40,
    )
    assert "error" not in out, out
    v2 = _new_version(client)
    assert v2["card_kind"] == "link"
    assert v2["actor"] == "client"
    assert v2["lifetime"] == "canon"


def test_add_spine_version_stamps_when_the_base_never_had_a_kind(server, client):
    _seed_live(client, card_kind=None, actor=None, lifetime=None)
    out = server.add_spine_version(
        project_code=SHORT, element_id="_authored/brief-note", body="v2 body " * 40,
    )
    assert "error" not in out, out
    v2 = _new_version(client)
    assert v2["card_kind"] == "reference"
    assert v2["actor"] == "inferred"  # the column default, written explicitly
