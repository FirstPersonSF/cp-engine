"""Client feedback is a layer the write verbs accept and never re-file (#352 item 2).

WHAT BROKE. On slt-5196 (2026-10-06) Leah's Storyline Map — client input —
was written with layer "Feedback". `canon_layer` passed the unknown value
through, MC-2's `spine_substance_layer_check` rejected it with a raw
constraint error that names no valid layer, and the author concluded there
was no feedback layer and filed it as `Note`: beside working notes, below
where the authority precedence ranks stakeholder signals.

The layer already existed as `Client feedback` (in the DB check, the mc-2 UI
and the engine). One layer, one name: `feedback` is an ALIAS of it. And an
unknown layer is refused at the verb boundary with the list of real ones —
never passed to the DB, never silently swapped for `note`.

    python -m pytest prototypes/hosted-mcp/test_layer_feedback_352.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_canonical_write_code import (  # noqa: E402
    COMPANY_ID,
    PID,
    SHORT,
    _columns,
)
from test_feeds_on_route import _RouteClient, _row  # noqa: E402


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
    c = _RouteClient({
        "spine_substance": _columns(server.SPINE_PULL_COLUMNS)
        | _columns(server._ELEMENT_RESOLVE_COLUMNS),
    })
    c.store["projects"] = [{
        "id": PID, "code": "SLT-brand-campaign-26",
        "full_job_name": "SLT 5196 Brand Campaign 26",
        "company_id": COMPANY_ID, "number": 5196,
    }]
    c.store["companies"] = [{"id": COMPANY_ID, "code": "SLT"}]
    c.store["spine_substance"] = []
    monkeypatch.setattr(server, "user_client", lambda: c)
    monkeypatch.setattr(server, "caller_subject", lambda: "user-sub-1")
    monkeypatch.setattr(server, "caller_email", lambda: "drew@firstperson.is")
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_paths_index_rows", lambda: {})
    monkeypatch.setattr(server, "upsert_auto_step", lambda *a, **k: {"ok": True})
    return c


BODY = "Storyline map: overview tab plus one tab per customer story. " * 4


def _rows(client):
    return client.store["spine_substance"]


# ── create_spine_element ──────────────────────────────────────────────────


@pytest.mark.parametrize("spelling", ["Feedback", "feedback", "clientfeedback",
                                      "Client feedback"])
def test_create_files_feedback_as_client_feedback(server, client, spelling):
    out = server.create_spine_element(
        project_code=SHORT, framing="Leah storyline map", body=BODY, layer=spelling)
    assert "error" not in out, out
    (row,) = _rows(client)
    assert row["layer"] == "Client feedback"


def test_create_refuses_an_unknown_layer_and_writes_nothing(server, client):
    out = server.create_spine_element(
        project_code=SHORT, framing="Leah storyline map", body=BODY, layer="Storyline")
    assert "error" in out
    assert "'Storyline'" in out["error"]
    # The refusal names the layer the author was looking for.
    assert "Client feedback" in out["error"]
    # Nothing landed — in particular, not a Note.
    assert _rows(client) == []


# ── add_spine_document (routes through create) ────────────────────────────


def test_add_spine_document_accepts_feedback(server, client):
    out = server.add_spine_document(
        project_code=SHORT, label="Leah storyline map", content=BODY, type="feedback")
    assert "error" not in out, out
    (row,) = _rows(client)
    assert row["layer"] == "Client feedback"


def test_add_spine_document_refuses_an_unknown_type(server, client):
    out = server.add_spine_document(
        project_code=SHORT, label="Leah storyline map", content=BODY, type="Storyline")
    assert "error" in out and "Client feedback" in out["error"]
    assert _rows(client) == []


# ── set_spine_element (reclassify) ────────────────────────────────────────


def _seed_note(client):
    client.store["spine_substance"] = [
        _row("_authored/leah-storyline-map", framing="Leah storyline map",
             layer="Note", placement="context", card_kind="reference",
             version_date="2026-10-06"),
    ]


def test_set_reclassifies_a_note_to_client_feedback(server, client):
    """The repair for the 10-06 element: re-file it with the alias."""
    _seed_note(client)
    out = server.set_spine_element(
        project_code=SHORT, key="_authored/leah-storyline-map", layer="feedback")
    assert "error" not in out, out
    (row,) = _rows(client)
    assert row["layer"] == "Client feedback"


def test_set_refuses_an_unknown_layer_and_leaves_the_row(server, client):
    _seed_note(client)
    out = server.set_spine_element(
        project_code=SHORT, key="_authored/leah-storyline-map", layer="Storyline")
    assert "error" in out and "Client feedback" in out["error"]
    (row,) = _rows(client)
    assert row["layer"] == "Note"

