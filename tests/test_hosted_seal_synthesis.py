# tests/test_hosted_seal_synthesis.py — issue #166: sealing PRODUCES a
# synthesis, it does not just end a round.
#
# Drew, 2026-08-09: "we will need to produce synthesis documents when we seal
# an activity or deliverable and that synthesis should then feed the next
# activity/deliverable. That's how the process works."
#
# Same harness as the other hosted tests: the decorated verbs are MCP Tool
# objects, so this targets the module-level helpers they are built from.
import importlib.util
import os
from pathlib import Path

import pytest

pytest.importorskip("jwt")
pytest.importorskip("mcp")
pytest.importorskip("supabase")

_SERVER_PATH = (
    Path(__file__).resolve().parents[1] / "prototypes" / "hosted-mcp" / "server.py"
)


@pytest.fixture(scope="module")
def srv():
    os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
    os.environ.setdefault("SUPABASE_ANON_KEY", "anon-key-for-tests")
    spec = importlib.util.spec_from_file_location("hosted_mcp_server", _SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ABSORBED = [
    {"est_item_id": "_authored/a", "framing": "DDI/DNS AI landscape analysis", "layer": "Research"},
    {"est_item_id": "_authored/b", "framing": "Core team synthesis (Apr 23)", "layer": "Synthesis"},
]


def test_draft_is_a_scaffold_not_a_summary(srv):
    """The one synthesis in the tenant that works (ibx-5153's Perspectives &
    Possibilities) is ~300 chars: what it is, what it decided, what it feeds.
    A draft that reads as finished prose invites a rubber-stamp."""
    body = srv._draft_synthesis_body("SRS Arc B", ABSORBED, None)
    assert "What this settles" in body
    assert "What it feeds" in body
    # Prompts the author deletes, not assertions they might leave standing.
    assert "_One line:" in body
    assert "scaffold, not a summary" in body


def test_draft_lists_what_was_absorbed_with_layers(srv):
    body = srv._draft_synthesis_body("SRS Arc B", ABSORBED, None)
    assert "Absorbed (2)" in body
    assert "DDI/DNS AI landscape analysis · Research" in body
    assert "Core team synthesis (Apr 23) · Synthesis" in body
    # And says what absorbed MEANS — out of retrieval, kept for retro.
    assert "retrospectives" in body


def test_draft_carries_the_seal_note_when_given(srv):
    body = srv._draft_synthesis_body("SRS Arc B", ABSORBED, "shipped 8/6 to Janet")
    assert "shipped 8/6 to Janet" in body
    assert srv._draft_synthesis_body("SRS Arc B", ABSORBED, None).count("Seal note") == 0


def test_draft_survives_an_empty_absorbed_list(srv):
    body = srv._draft_synthesis_body("SRS Arc B", [], None)
    assert "What this settles" in body
    assert "Absorbed" not in body


def test_element_meta_falls_back_to_the_id_when_lookup_fails(srv):
    """A metadata miss must never fail a seal — the edges have committed."""

    class Boom:
        def table(self, *_a, **_k):
            raise RuntimeError("unreachable")

    out = srv._element_meta(Boom(), "proj-1", ["_authored/x"])
    assert out == [{"est_item_id": "_authored/x", "framing": "_authored/x", "layer": ""}]


def test_element_meta_preserves_caller_order(srv):
    """The draft lists absorbed elements in the order they were sealed."""

    class Fake:
        def table(self, _n):
            return self

        def select(self, _c):
            return self

        def eq(self, *_a):
            return self

        def in_(self, *_a):
            return self

        def execute(self):
            # Deliberately reversed relative to the requested order.
            return type("R", (), {"data": [
                {"est_item_id": "_authored/b", "framing": "B", "layer": "Synthesis"},
                {"est_item_id": "_authored/a", "framing": "A", "layer": "Research"},
            ]})()

    out = srv._element_meta(Fake(), "proj-1", ["_authored/a", "_authored/b"])
    assert [o["framing"] for o in out] == ["A", "B"]


# ---- #167: what carries forward -------------------------------------------


class _FakeCanonClient:
    """Minimal PostgREST shape: canon edges + element metadata."""

    def __init__(self, canon_ids, meta):
        self._canon_ids = canon_ids
        self._meta = meta
        self._table = None
        self._filters = {}

    def table(self, name):
        self._table = name
        self._filters = {}
        return self

    def select(self, _c):
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def in_(self, _col, vals):
        self._filters["in"] = list(vals)
        return self

    def limit(self, _n):
        return self

    def execute(self):
        if self._table == "spine_relations":
            return type("R", (), {
                "data": [{"from_item_id": i} for i in self._canon_ids]
            })()
        wanted = self._filters.get("in", [])
        return type("R", (), {
            "data": [m for m in self._meta if m["est_item_id"] in wanted]
        })()


SCOPE = {"id": "proj-1", "project_code": "ibx-5192"}
DELIV = {"est_item_id": "_authored/arc-b", "framing": "SRS Arc B", "layer": "Output"}
SYN = {"est_item_id": "_authored/syn", "framing": "Synthesis — SRS Arc B", "layer": "Synthesis"}


def test_proposes_both_the_deliverable_and_the_synthesis(srv, monkeypatch):
    """The deck is the baseline; the synthesis is the thinking. Both feed
    forward, and they are not the same thing."""
    monkeypatch.setattr(srv, "resolve_live_element_id", lambda *a: ("brief", None))
    client = _FakeCanonClient([], [DELIV, SYN])
    out = srv._canon_proposal(client, SCOPE, [DELIV, SYN])
    assert [p["est_item_id"] for p in out["proposed"]] == [
        "_authored/arc-b",
        "_authored/syn",
    ]
    assert out["verb"] == "promote_to_canon"


def test_never_writes_only_proposes(srv, monkeypatch):
    """Deciding what displaces what IS the editorial act."""
    monkeypatch.setattr(srv, "resolve_live_element_id", lambda *a: ("brief", None))
    monkeypatch.setattr(
        srv, "_insert_lifecycle_edge",
        lambda *a, **k: pytest.fail("proposal must not write an edge"),
    )
    srv._canon_proposal(_FakeCanonClient([], [DELIV]), SCOPE, [DELIV])


def test_skips_what_is_already_canon(srv, monkeypatch):
    monkeypatch.setattr(srv, "resolve_live_element_id", lambda *a: ("brief", None))
    client = _FakeCanonClient(["_authored/arc-b"], [DELIV, SYN])
    out = srv._canon_proposal(client, SCOPE, [DELIV, SYN])
    assert [p["est_item_id"] for p in out["proposed"]] == ["_authored/syn"]
    assert [a["est_item_id"] for a in out["already_canon"]] == ["_authored/arc-b"]


def test_flags_when_promotion_would_exceed_the_target(srv, monkeypatch):
    """ibx-5153 carries 9 deliverables against a 7-member canon. Past target
    is a SIGNAL that the round replaced nothing — surfaced, not suppressed."""
    monkeypatch.setattr(srv, "resolve_live_element_id", lambda *a: ("brief", None))
    existing = [
        {"est_item_id": f"_authored/m{i}", "framing": f"M{i}", "layer": "Decisions"}
        for i in range(srv.CANON_TARGET_MAX)
    ]
    client = _FakeCanonClient([m["est_item_id"] for m in existing], existing + [DELIV])
    out = srv._canon_proposal(client, SCOPE, [DELIV])
    assert out["displacement_needed"] is True
    assert "replaces_key" in out["note"]
    # Still proposed — the caller decides, the verb does not block.
    assert len(out["proposed"]) == 1


def test_stays_quiet_when_within_target(srv, monkeypatch):
    monkeypatch.setattr(srv, "resolve_live_element_id", lambda *a: ("brief", None))
    out = srv._canon_proposal(_FakeCanonClient([], [DELIV]), SCOPE, [DELIV])
    assert "displacement_needed" not in out


def test_explains_itself_when_the_project_has_no_brief_anchor(srv, monkeypatch):
    """Canon anchors on the brief; without one there is nothing to promote to."""
    monkeypatch.setattr(srv, "resolve_live_element_id", lambda *a: (None, None))
    out = srv._canon_proposal(_FakeCanonClient([], []), SCOPE, [DELIV])
    assert out["proposed"] == []
    assert "anchors" in out["note"]
