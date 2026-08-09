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
