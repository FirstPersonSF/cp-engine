"""`serves` names which id space each target is in (#416).

`spine_substance.serves` holds estimate work-item UUIDs (most rows) and spine
`_authored/...` ids, with no marker. Binding the sap-5198 RFP was a guess
between the two; the hosted reads now echo `serves_kinds` beside `serves`.

    python -m pytest prototypes/hosted-mcp/test_serves_kinds_416.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_feeds_on_route import client, server  # noqa: E402,F401 — fixtures

WORK_ITEM = "3f1a9284-616d-4c2b-9a51-0d6e7f8a9b0c"


def test_set_spine_element_echoes_each_targets_id_space(server, client):  # noqa: F811
    out = server.set_spine_element(
        project_code="slt-5196", key="_authored/brief-doc",
        serves=[WORK_ITEM, "_authored/rfp-deliverable"])
    assert "error" not in out, out
    assert out["serves_kinds"] == {
        WORK_ITEM: "work_item",
        "_authored/rfp-deliverable": "spine_element",
    }


def test_pull_shape_carries_serves_and_kinds(server):  # noqa: F811
    assert server._serves_kinds_field({"serves": ["act-uuid"]}) == {
        "serves_kinds": {"act-uuid": "unknown"}}
    assert server._serves_kinds_field({"serves": []}) == {}


def test_the_docstrings_name_both_id_spaces(server):  # noqa: F811
    for verb in (server.set_spine_element, server.pull_spine_element):
        doc = verb.__doc__ or ""
        assert "_authored/" in doc and "work-item UUID" in doc, verb.__name__
        assert "serves_kinds" in doc, verb.__name__


def test_pull_spine_element_returns_serves_with_kinds(server, client, monkeypatch):  # noqa: F811
    for r in client.store["spine_substance"]:
        if r["est_item_id"] == "_authored/brief-doc":
            r["serves"] = [WORK_ITEM]
    monkeypatch.setattr(server, "_with_project_status", lambda result, *a, **k: result)
    out = server.pull_spine_element(key="_authored/brief-doc")
    assert "error" not in out, out
    assert out["serves"] == [WORK_ITEM]
    assert out["serves_kinds"] == {WORK_ITEM: "work_item"}


def test_pull_selects_the_serves_column(server):  # noqa: F811
    """The fake client returns whole rows whatever is selected, so the pull
    test above cannot see a missing column; production PostgREST can."""
    cols = {c.strip() for c in server.SPINE_PULL_COLUMNS.split(",")}
    assert "serves" in cols
