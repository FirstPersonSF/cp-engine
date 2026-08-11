"""Tests for ``cp stub-sweep`` (#178).

Shapes taken from the real tenant (queried 2026-08-11): every stub's body is
the ingest boilerplate ``Ingested document: **X** (doc)\\n\\nrag_asset: <uuid>``
with the SAME rag_asset already in `sources`, and 79 of 82 carry a `serves`
binding — the fact that makes this a transfer rather than a delete.
"""

from __future__ import annotations

from cp_engine.stub_sweep import find_stubs, render_sweep

RAG = "1fb5e23e-0cfe-4d85-87e8-903d46c48a33"
BOILERPLATE = f"Ingested document: **Marcello Grande** (doc)\n\nrag_asset: `{RAG}`"


def _row(eid, framing, layer, body="", serves=None, sources=None):
    return {
        "est_item_id": eid,
        "framing": framing,
        "layer": layer,
        "status": "live",
        "archived": False,
        "body": body,
        "serves": serves or [],
        "sources": sources or [],
    }


def _stub(eid="_authored/marcello-grande", serves=None):
    return _row(
        eid, "Marcello Grande", "Source material",
        body=BOILERPLATE, serves=serves or [],
        sources=[{"id": RAG, "type": "rag_asset", "title": "Marcello Grande"}],
    )


ACTIVITY = _row("_authored/deck-build", "Deck build", "Activity")


def test_a_boilerplate_card_serving_a_real_element_is_attachable():
    stubs = find_stubs([_stub(serves=["_authored/deck-build"]), ACTIVITY])
    assert len(stubs) == 1
    s = stubs[0]
    assert s.attachable
    assert not s.orphan
    assert s.targets == [("_authored/deck-build", "Deck build", True)]


def test_a_stub_serving_a_bare_estimate_slot_is_an_orphan():
    """48 of the tenant's routings point at a slot that is not a spine
    element. There is nothing to attach to, so nothing may be proposed —
    inventing a target is the mistake that killed #174's backfill."""
    stubs = find_stubs([_stub(serves=["some-estimate-slot-uuid"])])
    assert stubs[0].orphan
    assert not stubs[0].attachable
    assert stubs[0].unresolved == ["some-estimate-slot-uuid"]


def test_a_stub_routed_nowhere_at_all_is_an_orphan():
    stubs = find_stubs([_stub(serves=[])])
    assert stubs[0].orphan


def test_a_hand_written_short_note_is_never_a_stub():
    """Thin is not empty. Only the ingest's own boilerplate qualifies — a
    short human note is somebody's thought, and not this sweep's call."""
    rows = [_row("_authored/note", "A real thought", "Source material",
                 body="Carol's framework is the alignment target.")]
    assert find_stubs(rows) == []


def test_a_long_source_card_is_never_a_stub():
    rows = [_row("_authored/big", "A distilled source", "Source material",
                 body="Ingested document: **X** (doc)\n\n" + "x" * 500)]
    assert find_stubs(rows) == []


def test_non_source_layers_are_ignored():
    rows = [_row("_authored/d", "A deck", "Deliverables", body=BOILERPLATE)]
    assert find_stubs(rows) == []


def test_a_stub_serving_itself_does_not_become_its_own_target():
    eid = "_authored/self"
    stubs = find_stubs([_stub(eid=eid, serves=[eid])])
    assert stubs[0].orphan
    assert stubs[0].targets == []


def test_typed_edges_are_flagged_because_retiring_cascades_them():
    """`retire` deletes a card's edges. A stub that something points at is
    not safe to retire silently."""
    stub = _stub(serves=["_authored/deck-build"])
    stubs = find_stubs(
        [stub, ACTIVITY],
        [{"kind": "informs", "from_item_id": stub["est_item_id"],
          "to_item_id": "_authored/deck-build"}],
    )
    assert stubs[0].has_edges
    assert "has typed edges" in render_sweep(stubs, code="ibx-5192")


def test_a_stub_with_no_sources_is_not_attachable():
    """Nothing to transfer — retiring it would lose nothing, but the sweep
    must not claim there is provenance to move."""
    row = _row("_authored/empty", "Empty", "Source material",
               body=BOILERPLATE, serves=["_authored/deck-build"], sources=[])
    stubs = find_stubs([row, ACTIVITY])
    assert not stubs[0].attachable


def test_render_names_the_source_and_the_destination():
    stubs = find_stubs([_stub(serves=["_authored/deck-build"]), ACTIVITY])
    text = render_sweep(stubs, code="ibx-5192")
    assert "Marcello Grande" in text
    assert "→ serves: Deck build" in text
    assert "attachable" in text


def test_render_keeps_orphans_visible_and_says_why():
    stubs = find_stubs([_stub(serves=["bare-slot"])])
    text = render_sweep(stubs, code="ibx-5192")
    assert "bare estimate slot" in text
    assert "Inventing a target would be a guess." in text


def test_attachable_stubs_sort_before_orphans():
    rows = [
        _stub(eid="_authored/z-orphan", serves=[]),
        _stub(eid="_authored/a-attachable", serves=["_authored/deck-build"]),
        ACTIVITY,
    ]
    stubs = find_stubs(rows)
    assert [s.est_item_id for s in stubs] == [
        "_authored/a-attachable", "_authored/z-orphan"]


def test_render_empty_is_quiet():
    assert "no empty Source-material cards" in render_sweep([], code="ibx-5192")


UNLAYERED = _row("5fca0b9c", "This is our post meeting conversation…", None)


def test_a_target_with_no_layer_is_flagged_as_unsound():
    """34 of the 65 attachable stubs route to a card with `layer: null` —
    one spine-lint already calls unfilable. Moving provenance onto it buries
    the document in a card that is itself broken, so the migration has an
    ordering constraint: fix the destination first."""
    stubs = find_stubs([_stub(serves=["5fca0b9c"]), UNLAYERED])
    assert stubs[0].attachable          # it still HAS somewhere to go...
    assert stubs[0].unsound_targets     # ...but that somewhere is broken
    text = render_sweep(stubs, code="ibx-5192")
    assert "UNLAYERED TARGET" in text
    assert "Fix the destination's layer first" in text


def test_a_sound_target_produces_no_warning():
    stubs = find_stubs([_stub(serves=["_authored/deck-build"]), ACTIVITY])
    assert stubs[0].unsound_targets == []
    text = render_sweep(stubs, code="ibx-5192")
    assert "UNLAYERED TARGET" not in text
    assert "Fix the destination" not in text
