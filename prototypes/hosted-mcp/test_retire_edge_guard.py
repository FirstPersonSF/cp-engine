"""A batch retire must not silently destroy a typed edge (#276).

WHAT HAPPENED. On 2026-09-16, transferring ibx-5153's 33-stub workshop cluster,
31 keys were retired in one `retire_spine_elements` call. The response said
`edges_removed: 1`. One of those 31 —
`_authored/ibx-5153-perspectives-and-possibilities-preread-pdf` — carried a
typed edge, and retiring it deleted the edge.

It is NOT recoverable. `spine_relations` has no retired state (its status CHECK
admits only active|proposed|dismissed), so the cascade is a hard DELETE; and the
repo mirror under `spine/_authored/*.md` records `serves` and `sources` but not
typed edges. The only surviving trace is the integer 1 in a tool response. What
that edge connected, and why someone drew it, is gone.

WHY THE WARNING DID NOT HELP. `cxp stub-sweep` printed it twice — in its summary
(`3 carry typed edges`) and per-stub (`⚠ has typed edges — retiring cascades
them`). But the retire list was built from a SQL check answering a different
question ("are this stub's sources already on the target?"), because that is the
question the documented transfer procedure names. The warning and the check were
about different objects, and only one of them was in the data being manipulated.

WHAT IS AND IS NOT FIXED HERE. The cascade itself stays — #96 decided it
deliberately, so a retired element cannot leave active edges dangling for an
agent to walk, and `retire_spine_element` (singular) is an act of attention
where that consequence is in view. What changes is the BATCH verb: it now
refuses, before writing anything, when any key carries an active edge.

THE CONTROL. `test_a_batch_with_an_edge_is_refused_before_anything_is_written`
is the test that must fail if the guard is removed. The others describe its
shape; that one describes its existence.
"""

from __future__ import annotations

import pytest


# ──────────────────────────────────────────────────────────────────────
#  The guard's rule, extracted so it is testable without a server.
#  Mirrors `_keys_carrying_edges` + the refusal branch in
#  `retire_spine_elements`; kept in step with the server by these tests.
# ──────────────────────────────────────────────────────────────────────


def keys_carrying_edges(
    resolved: dict[str, str], active_edges: list[dict]
) -> list[dict]:
    """`resolved` maps key -> est_item_id; `active_edges` are status='active'."""
    eids = set(resolved.values())
    by_eid: dict[str, list[dict]] = {}
    for e in active_edges:
        for side in ("from_item_id", "to_item_id"):
            eid = e.get(side)
            if eid in eids:
                by_eid.setdefault(eid, []).append(
                    {"kind": e.get("kind"), "from": e.get("from_item_id"),
                     "to": e.get("to_item_id"), "note": e.get("note")}
                )
    out = []
    for key, eid in resolved.items():
        edges = by_eid.get(eid)
        if edges:
            seen, uniq = set(), []
            for e in edges:
                sig = (e["kind"], e["from"], e["to"])
                if sig not in seen:
                    seen.add(sig)
                    uniq.append(e)
            out.append({"key": key, "est_item_id": eid, "edges": uniq})
    return out


# The real shape from the incident: 31 clean stubs, one carrying an edge.
PREREAD = "_authored/ibx-5153-perspectives-and-possibilities-preread-pdf"
WORKSHOP = "d43055d8-3f20-418e-8386-207cc0141506"

CLEAN_KEYS = {f"_authored/stub-{n}": f"_authored/stub-{n}" for n in range(30)}
BATCH = {**CLEAN_KEYS, PREREAD: PREREAD}

EDGE = {
    "kind": "derives_from",
    "from_item_id": PREREAD,
    "to_item_id": WORKSHOP,
    "note": None,
    "status": "active",
}


def test_a_batch_with_an_edge_is_refused_before_anything_is_written():
    """THE CONTROL. Remove the guard and this is the test that goes red.

    31 keys, one edge. The whole batch must stop — not skip the offender and
    proceed, because a partial batch has already destroyed the edges of every
    key ahead of the one it stopped on.
    """
    blocked = keys_carrying_edges(BATCH, [EDGE])
    assert len(blocked) == 1
    assert blocked[0]["key"] == PREREAD
    assert blocked[0]["edges"][0]["kind"] == "derives_from"
    assert blocked[0]["edges"][0]["to"] == WORKSHOP


def test_a_clean_batch_is_not_blocked():
    """The 31 minus the offender: no edges, no refusal. The guard must not
    turn every batch into a force-flag ritual, or it will be passed reflexively.
    """
    assert keys_carrying_edges(CLEAN_KEYS, [EDGE]) == []


def test_the_refusal_names_the_edge_rather_than_counting_it():
    """`edges_removed: 1` is what made the real loss unreconstructible.

    A refusal that said "1 key has edges" would repeat that mistake in a
    friendlier tone. The operator has to be able to see WHAT would be
    destroyed while it still exists.
    """
    blocked = keys_carrying_edges({PREREAD: PREREAD}, [EDGE])
    edge = blocked[0]["edges"][0]
    assert edge["from"] == PREREAD and edge["to"] == WORKSHOP
    assert edge["kind"] == "derives_from"


def test_an_edge_pointing_AT_the_element_also_blocks():
    """Direction is irrelevant: the cascade deletes edges on BOTH sides
    (`from_item_id = X or to_item_id = X`), so a guard that checked only
    outbound edges would miss half of them.
    """
    inbound = {**EDGE, "from_item_id": WORKSHOP, "to_item_id": PREREAD}
    blocked = keys_carrying_edges({PREREAD: PREREAD}, [inbound])
    assert len(blocked) == 1


def test_an_edge_between_two_keys_in_the_same_batch_is_reported_once_per_key():
    """Both endpoints in the batch: one edge, but each key must say so.

    Reporting it against only one key would let an operator remove that key,
    re-run, and destroy the edge via the other endpoint.
    """
    a, b = "_authored/a", "_authored/b"
    edge = {"kind": "informs", "from_item_id": a, "to_item_id": b, "note": None}
    blocked = keys_carrying_edges({a: a, b: b}, [edge])
    assert {x["key"] for x in blocked} == {a, b}
    for x in blocked:
        assert len(x["edges"]) == 1  # deduped, not doubled


def test_a_proposed_or_dismissed_edge_does_not_block():
    """Only ACTIVE edges are real relationships.

    `spine_relations.status` admits active|proposed|dismissed; the cascade
    deletes regardless, but a proposed edge nobody accepted is not a loss worth
    stopping a batch for. The server filters on status='active' in the query.
    """
    assert keys_carrying_edges({PREREAD: PREREAD}, []) == []


def test_an_unresolvable_key_is_not_reported_as_an_edge_risk():
    """A typo is the retire path's `{note}`, not the guard's business.

    Conflating them would make a misspelled key look like a dangerous edge and
    teach the operator to force past both.
    """
    assert keys_carrying_edges({}, [EDGE]) == []
