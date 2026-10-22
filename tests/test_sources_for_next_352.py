"""Sources per version (#352 item 3, Option A) — engine side.

WHAT BROKE. `add_element_source` wrote the typed link onto EVERY version row.
Attaching a new input to a deliverable whose current version had already
shipped therefore claimed the shipped version (and every older one) drew on
it. History lied.

WHAT THESE PIN.
  * An attach writes the LIVE row only; superseded rows keep the sources they
    actually had.
  * When the live version is a shipped deliverable (seal-sweep's "shipped"
    test), the link carries `for_next: true` — it arrived after this version
    and is an input to the NEXT one. `for_next_version` overrides either way.
  * Readers treat a `for_next` link as not yet consumed: pull labels it,
    seal-sweep does not count it as having fed the shipped version, and
    spine-lint does not let it make a shipped deliverable look sourced.
  * A detach still strips every version (a wrong attach is wrong everywhere).

    python -m pytest tests/test_sources_for_next_352.py -v
"""
from __future__ import annotations

from datetime import date

import cp_engine.project_sources as ps
from cp_engine import seal_sweep as ss
from cp_engine.spine_lint import lint_curation


def _asset(aid, title):
    return {"id": aid, "title": title, "source_type": "drive",
            "created_at": "2026-07-01", "file_hash": None}


def _version(vid, status="live", sources=None, layer="Deliverables",
             label="v2"):
    return {"id": vid, "est_item_id": "_authored/deck", "framing": "Deck",
            "status": status, "archived": False, "scope": None,
            "company_id": None, "project_id": "pid", "layer": layer,
            "card_kind": None, "version_label": label,
            "sources": sources or []}


class _Client:
    def __init__(self):
        self.updates = []

    def table(self, name):
        client = self

        class _T:
            def update(self, patch):
                self._patch = patch
                return self
            def select(self, c): return self
            def eq(self, c, v):
                if hasattr(self, "_patch"):
                    client.updates.append((v, self._patch))
                return self
            def order(self, *a, **k): return self
            def execute(self): return type("R", (), {"data": []})()
        return _T()


def _run(monkeypatch, versions, *, add=True, **kw):
    monkeypatch.setattr(
        ps, "resolve_element_versions",
        lambda client, pid, key, *, columns, company_id=None:
        ("_authored/deck", versions))
    monkeypatch.setattr(ps, "list_sources",
                        lambda c, p, cid, **k: [_asset("a1", "Round 3 notes")])
    client = _Client()
    out = ps.modify_element_sources(client, "pid", "deck", "Round 3 notes",
                                    add=add, **kw)
    return out, client


# --- the write: live row only, for_next on a shipped deliverable -----------

def test_attach_writes_the_live_row_only(monkeypatch):
    versions = [_version("v1", status="superseded", label="v1"),
                _version("v2")]
    _, client = _run(monkeypatch, versions)
    assert [u[0] for u in client.updates] == ["v2"]


def test_attach_to_a_shipped_deliverable_is_for_the_next_version(monkeypatch):
    out, client = _run(monkeypatch, [_version("v2")])
    written = client.updates[0][1]["sources"]
    assert written == [{"type": "rag_asset", "id": "a1",
                        "title": "Round 3 notes", "for_next": True}]
    assert out["for_next"] is True


def test_attach_to_a_non_deliverable_is_a_normal_source(monkeypatch):
    out, client = _run(monkeypatch, [_version("v2", layer="Synthesis")])
    assert client.updates[0][1]["sources"] == [
        {"type": "rag_asset", "id": "a1", "title": "Round 3 notes"}]
    assert not out.get("for_next")


def test_explicit_for_next_version_overrides_both_ways(monkeypatch):
    _, client = _run(monkeypatch, [_version("v2", layer="Synthesis")],
                     for_next_version=True)
    assert client.updates[0][1]["sources"][0]["for_next"] is True
    _, client = _run(monkeypatch, [_version("v2")], for_next_version=False)
    assert "for_next" not in client.updates[0][1]["sources"][0]


def test_detach_still_strips_every_version(monkeypatch):
    link = {"type": "rag_asset", "id": "a1", "title": "Round 3 notes"}
    versions = [_version("v1", status="superseded", sources=[link], label="v1"),
                _version("v2", sources=[dict(link, for_next=True)])]
    _, client = _run(monkeypatch, versions, add=False)
    assert sorted(u[0] for u in client.updates) == ["v1", "v2"]


# --- shared helpers --------------------------------------------------------

def test_split_and_clear_helpers():
    a = {"type": "rag_asset", "id": "a", "title": "A"}
    b = {"type": "rag_asset", "id": "b", "title": "B", "for_next": True}
    consumed, pending = ss.split_for_next([a, b])
    assert consumed == [a] and pending == [b]
    assert ss.clear_for_next([a, b]) == [a, {"type": "rag_asset", "id": "b",
                                             "title": "B"}]
    assert ss.clear_for_next(None) == []


def test_shipped_test_is_seal_sweeps():
    assert ss.is_shipped_deliverable({"layer": "Deliverables",
                                      "version_label": "v1"})
    assert not ss.is_shipped_deliverable({"layer": "Synthesis",
                                          "version_label": "v3"})


# --- readers ---------------------------------------------------------------

def test_pull_labels_next_version_sources():
    a = {"type": "rag_asset", "id": "a", "title": "Fed v2"}
    b = {"type": "rag_asset", "id": "b", "title": "Arrived later",
         "for_next": True}
    out = ps._spine_element({"est_item_id": "_authored/deck",
                             "sources": [a, b], "body": "x"})
    assert out["sources_for_next_version"] == [b]


def test_seal_sweep_does_not_count_for_next_as_fed():
    a = {"type": "rag_asset", "id": "a", "title": "Fed v2"}
    b = {"type": "rag_asset", "id": "b", "title": "Arrived later",
         "for_next": True}
    rows = [{"est_item_id": "_authored/deck", "framing": "Deck",
             "layer": "Deliverables", "version_label": "v2",
             "version_date": "2026-10-20", "sources": [a, b]}]
    (rnd,) = ss.build_rounds(rows, [], today=date(2026, 10, 22))
    assert [s["id"] for s in rnd.sources] == ["a"]
    assert [s["id"] for s in rnd.next_sources] == ["b"]
    text = ss.render_sweep([rnd], code="ibx-5153")
    assert "Arrived later" in text and "next version" in text


def test_lint_a_for_next_source_does_not_source_a_shipped_deliverable():
    row = {"est_item_id": "_authored/deck", "framing": "Deck",
           "layer": "Deliverables", "version_label": "v1",
           "version_date": "2026-10-20", "body": "short",
           "sources": [{"type": "rag_asset", "id": "b", "title": "Later",
                        "for_next": True}]}
    warns = lint_curation([row], today=date(2026, 10, 22), has_agreement=False)
    assert any("empty deliverable" in w for w in warns), warns


def test_seal_sweep_columns_carry_sources():
    from cp_engine import mc2_db
    assert "sources" in mc2_db.SEAL_SWEEP_COLUMNS
