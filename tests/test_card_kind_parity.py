"""The write-time stamp and the reader must agree (#179).

`card_kind` has two authorities and they are in different repositories:

    spine_authoring.authored_element.card_kind_for   stamps it on write
    cp_engine.card_class.classify                    reads it (and infers when
                                                     the column is NULL)

That is precisely the shape of duplication this whole effort exists to remove —
"is this work?" was being decided in five places, and #172 and #178 are what it
costs when two of them disagree. The stamp lives in the shared package because
mc-2 writes through the same builder; the reader lives here. They cannot be one
function, so they are pinned to each other instead.

THE CONTRACT
------------
`card_kind_for` may DECLINE (return None → column left NULL for a human), but
it may never CONTRADICT `classify`. A contradiction is worse than a NULL: a
stored kind is indistinguishable from a human decision once written, so a wrong
stamp is laundered into fact and `classify_is_inferred()` can never flag it.

This is the same guard as tests/test_resolver_parity.py, for the same reason —
that one was added after the engine and hosted resolvers silently drifted.
"""
from __future__ import annotations

import pytest

from cp_engine.card_class import classify
from spine_authoring.authored_element import card_kind_for

# (est_item_id, layer, placement) — the axes the stamp reads. Covers every
# branch of both functions plus the straddling layers named in card_class's
# docstring (`Email`, `Activity`).
CASES = [
    # authored elements: always placement='context' by construction
    ("_authored/email-from-janet", "Email", "context"),
    ("_authored/some-synthesis", "Synthesis", "context"),
    ("_authored/a-write-up", "Activity", "context"),
    ("_authored/srs-field-deck", "Deliverables", "context"),
    ("_authored/a-draft", "Drafts", "context"),
    ("_authored/inputs-briefing", "Brief", "context"),
    ("_authored/a-brief", "Brief", "context"),
    ("_authored/an-agreement", "Agreement", "context"),
    ("_authored/a-decision", "Decisions", "context"),
    ("_authored/a-source", "Source material", "context"),
    # distilled rows: uuid-keyed estimate slots
    ("3f1c9a2e-0000-4000-8000-000000000001", "Activity", "item"),
    ("3f1c9a2e-0000-4000-8000-000000000002", "Synthesis", "item"),
    ("3f1c9a2e-0000-4000-8000-000000000003", "Client feedback", "item"),
    ("3f1c9a2e-0000-4000-8000-000000000004", "Deliverables", "item"),
]


@pytest.mark.parametrize("est_item_id,layer,placement", CASES)
def test_stamp_never_contradicts_the_reader(est_item_id, layer, placement):
    stamped = card_kind_for(
        est_item_id=est_item_id, layer=layer, placement=placement
    )
    if stamped is None:
        return  # declining is allowed; contradicting is not
    read = classify(
        {"est_item_id": est_item_id, "layer": layer, "placement": placement}
    )
    assert stamped == read.value, (
        f"write-time stamp {stamped!r} contradicts classify() {read.value!r} "
        f"for layer={layer!r} placement={placement!r}. A stored kind cannot be "
        "told from a human decision, so a wrong stamp is permanent."
    )


def test_a_stamped_row_reads_back_as_itself():
    """`classify` prefers an explicit `card_kind`, so a stamped row must not be
    re-derived differently on read — the round trip has to be stable."""
    for est_item_id, layer, placement in CASES:
        stamped = card_kind_for(
            est_item_id=est_item_id, layer=layer, placement=placement
        )
        if stamped is None:
            continue
        row = {
            "est_item_id": est_item_id, "layer": layer,
            "placement": placement, "card_kind": stamped,
        }
        assert classify(row).value == stamped


def test_authored_rows_are_never_left_null():
    """Every authored element is placement='context', so the stamp must always
    resolve one — an authored write that left NULL is the leak this fixes."""
    for est_item_id, layer, placement in CASES:
        if placement != "context":
            continue
        assert card_kind_for(
            est_item_id=est_item_id, layer=layer, placement=placement
        ) is not None, f"{layer!r} authored row would still write NULL"
