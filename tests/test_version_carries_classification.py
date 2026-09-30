# tests/test_version_carries_classification.py
"""A cxp-side version bump preserves `actor`, `lifetime` and a stored `card_kind` (#343).

The cxp write path builds new version rows with `spine_authoring.build_version_rows`
(re-exported here as `cp_engine.authored_element.build_version_rows`; mc-2's
POST /api/spine/{code}/element version path uses the same builder). Up to
spine-authoring 765bd18 that builder rebuilt the row without `actor` or
`lifetime`, so the INSERT fell back to actor 'inferred' (the column default)
and lifetime NULL, and it re-derived `card_kind` from structure over whatever
kind the element had. The hosted `add_spine_version` was fixed in #315
(prototypes/hosted-mcp/test_card_kind_stamp.py). These tests hold the library
path to the same three rules:

    actor     = base.actor or 'inferred'
    lifetime  = base.lifetime
    card_kind = base.card_kind, else the structural stamp

`base` is the prior LIVE row. Against the 765bd18 pin all four fail (the last
because the rebuilt row has no `actor` key at all).
"""
from cp_engine.authored_element import build_version_rows


def _bump(prior):
    (row,) = build_version_rows(
        project_id="pid", project_code="ibx-5153",
        est_item_id="_authored/field-notes", prior_versions=prior,
        body="revised body", version_note="r2",
        now_iso="2026-09-30T12:00:00+00:00",
    )
    return row


def _live(**extra):
    row = {"version_label": "v1", "status": "live", "framing": "Field notes",
           "layer": "Research", "serves": [], "sources": []}
    row.update(extra)
    return row


def test_version_bump_preserves_actor_and_lifetime():
    row = _bump([_live(actor="partner", lifetime="durable")])
    assert row["version_label"] == "v2"
    assert row["actor"] == "partner"
    assert row["lifetime"] == "durable"


def test_carry_is_from_the_live_row_not_a_stale_superseded_one():
    prior = [
        _live(version_label="v1", status="superseded", actor="inferred",
              lifetime=None, card_kind="reference"),
        _live(version_label="v2", actor="partner", lifetime="ephemeral",
              card_kind="link"),
    ]
    row = _bump(prior)
    assert row["version_label"] == "v3"
    assert (row["actor"], row["lifetime"], row["card_kind"]) == (
        "partner", "ephemeral", "link")


def test_stored_card_kind_is_not_rederived():
    # Structure alone would stamp this authored context row 'reference'.
    assert _bump([_live(card_kind="link")])["card_kind"] == "link"


def test_untagged_base_matches_the_hosted_fallbacks():
    row = _bump([_live(body="x" * 500)])
    assert row["actor"] == "inferred"
    assert row["lifetime"] is None
    assert row["card_kind"] == "reference"
