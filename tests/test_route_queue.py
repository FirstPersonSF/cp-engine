"""What needs a home — the rule, stated identically on both sides.

`needs_home` here and `needsRouting` in the frontend's `lib/spine/route.ts`
encode the SAME three exclusions against different substrates (a raw substance
row vs. an already-fetched outline entry). They are deliberately duplicated
rather than shared, so these assertions and the TypeScript ones must read the
same — a change to one should fail the other's expectations loudly.
"""

from cp_engine.route_queue import needs_home


def row(**over):
    base = {
        "id": "sub-1",
        "serves": [],
        "card_kind": None,
        "est_item_id": "_authored/some-decision",
        "lifetime": "canon",
        "placement": "context",
    }
    base.update(over)
    return base


def test_unrouted_canon_needs_a_home():
    assert needs_home(row(lifetime="canon")) is True


def test_unrouted_feedback_needs_a_home():
    assert needs_home(row(lifetime="feedback")) is True


def test_unclassified_needs_a_home():
    # Not yet sorted is not the same as not needing a home — hiding these would
    # let a row fall through both queues.
    assert needs_home(row(lifetime=None)) is True


def test_BACKGROUND_never_needs_a_home():
    # The decision this module turns on. Background belongs to no single work
    # item by definition — 14 of ibx-5192's 19 unrouted background rows are the
    # Infoblox use-case briefs and the corporate library. Queueing them asks a
    # human to file the library under one deliverable.
    assert needs_home(row(lifetime="background")) is False


def test_something_already_routed_does_not():
    assert needs_home(row(serves=["est-1"])) is False


def test_a_null_serves_counts_as_unrouted():
    # jsonb NULL and `[]` both mean "no home"; only one of them is falsy in SQL,
    # which is why this filter runs in Python.
    assert needs_home(row(serves=None)) is True


def test_work_cards_never_need_a_home():
    # A deliverable does not serve a work item; it IS one. Routing it files the
    # deck inside itself.
    for kind in ("activity", "deliverable", "engagement"):
        assert needs_home(row(card_kind=kind)) is False


def test_standing_elements_are_unbound_by_contract():
    # Inputs & Briefing and the SOW frame the whole engagement. Both are canon
    # and both are correctly homeless, so the lifetime rule alone would queue
    # them — this exclusion is what keeps ibx-5192's canon backlog at 10 and
    # not 12.
    for eid in ("_authored/inputs-briefing", "_authored/sow"):
        assert needs_home(row(est_item_id=eid)) is False


def test_the_exclusions_compose():
    # A background work card that is already routed is excluded three times
    # over; no single check should be load-bearing on its own.
    assert (
        needs_home(
            row(lifetime="background", card_kind="deliverable", serves=["est-1"])
        )
        is False
    )
