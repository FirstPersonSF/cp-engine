from datetime import date

from cp_engine.spine import (
    SpineElement,
    active_deliverable_ids,
    score_element,
)


def _el(**kw) -> SpineElement:
    base = dict(
        id="x",
        project="ibx-5153",
        layer="Research",
        title="t",
        status="active",
        last_touched="2026-06-13",
        path=None,
        body="",
    )
    base.update(kw)
    return SpineElement(**base)  # type: ignore[arg-type]


TODAY = date(2026, 6, 13)


def test_active_deliverable_ids_excludes_final_and_dormant() -> None:
    els = (
        _el(id="d1", layer="Deliverables", stage="revised", status="active"),
        _el(id="d2", layer="Deliverables", stage="final", status="final"),
        _el(id="d3", layer="Deliverables", stage="first", status="dormant"),
    )
    assert active_deliverable_ids(els) == {"d1"}


def test_active_deliverable_ids_excludes_stage_final_even_when_status_active() -> None:
    # The stage != "final" clause must do its own work: a final-stage deliverable
    # is excluded even when its status is "active".
    final_but_active = _el(
        id="d_final", layer="Deliverables", stage="final", status="active"
    )
    revised_active = _el(
        id="d_revised", layer="Deliverables", stage="revised", status="active"
    )
    assert active_deliverable_ids((final_but_active,)) == set()
    assert active_deliverable_ids((final_but_active, revised_active)) == {"d_revised"}


def test_serving_active_deliverable_scores_above_serving_nothing() -> None:
    active = {"d1"}
    serving = _el(id="a", serves=("d1",), last_touched="2026-06-13")
    idle = _el(id="b", serves=(), last_touched="2026-06-13")
    assert score_element(serving, active, TODAY) > score_element(idle, active, TODAY)


def test_framing_layer_scores_high_even_with_no_serves() -> None:
    active: set[str] = set()
    brief = _el(id="brief", layer="Brief", serves=(), last_touched="2026-06-13")
    cold_research = _el(id="r", layer="Research", serves=(), last_touched="2026-04-01")
    assert score_element(brief, active, TODAY) > score_element(cold_research, active, TODAY)


def test_recency_decays() -> None:
    active = {"d1"}
    fresh = _el(id="f", serves=("d1",), last_touched="2026-06-13")
    stale = _el(id="s", serves=("d1",), last_touched="2026-03-01")
    assert score_element(fresh, active, TODAY) > score_element(stale, active, TODAY)


def test_cold_element_still_scores_above_zero() -> None:
    # "Cold is a dimmer, not an off-switch" (design §2).
    active: set[str] = set()
    cold = _el(id="c", serves=(), layer="Research", last_touched="2026-01-01")
    assert score_element(cold, active, TODAY) > 0.0


def test_missing_last_touched_does_not_crash() -> None:
    active: set[str] = set()
    el = _el(id="nodate", serves=(), last_touched="")
    assert score_element(el, active, TODAY) > 0.0


def test_final_status_demotes_below_active_when_otherwise_equal() -> None:
    # A delivered (final) artifact serving an active deliverable should rank
    # BELOW a still-active one with the same recency/layer/serves wiring.
    active = {"d1"}
    live = _el(
        id="live", layer="Deliverables", serves=("d1",),
        status="active", last_touched="2026-06-13",
    )
    delivered = _el(
        id="done", layer="Deliverables", serves=("d1",),
        status="final", last_touched="2026-06-13",
    )
    assert score_element(delivered, active, TODAY) < score_element(live, active, TODAY)


def test_final_artifact_still_above_a_cold_reference() -> None:
    # Demoted, not dropped: a recent final artifact still outranks an old
    # reference element (the "dimmer, not off-switch" property).
    active = {"d1"}
    delivered = _el(
        id="done", layer="Deliverables", serves=("d1",),
        status="final", last_touched="2026-06-13",
    )
    cold_ref = _el(
        id="old", layer="SourceMaterial", serves=(),
        status="reference", last_touched="2026-01-01",
    )
    assert score_element(delivered, active, TODAY) > score_element(cold_ref, active, TODAY)


def test_reference_and_dormant_demote_but_less_than_final() -> None:
    active: set[str] = set()
    base = dict(layer="Research", serves=(), last_touched="2026-06-13")
    active_el = _el(id="a", status="active", **base)
    reference_el = _el(id="r", status="reference", **base)
    final_el = _el(id="f", status="final", **base)
    s_active = score_element(active_el, active, TODAY)
    s_reference = score_element(reference_el, active, TODAY)
    s_final = score_element(final_el, active, TODAY)
    assert s_active > s_reference > s_final


# ──────────────────────────────────────────────────────────────────────
#  #278 — layer comparisons must survive the spacing the tenant uses
#
#  This module is the FRONTMATTER domain: `layer` is a required key and there
#  is no `card_kind` to prefer, so these checks stay layer-keyed. What was
#  broken is that they compared exact-match against CamelCase constants while
#  real frontmatter carries the spaced forms. Measured across the cp tenant
#  2026-09-16: `Source material` 75 files, `Client feedback` 15, against
#  `SourceMaterial` 4 and `ClientFeedback` 3.
# ──────────────────────────────────────────────────────────────────────


def test_spaced_layer_scores_the_same_as_camelcase() -> None:
    """`LAYER_IMPORTANCE.get(el.layer, 0.5)` fell to its default for 90 files.

    `Client feedback` is the worst case: it scored 0.5 when the table says
    0.80, so real client feedback ranked BELOW an unmapped layer.
    """
    spaced = score_element(_el(layer="Client feedback"), active=set(), today=TODAY)
    camel = score_element(_el(layer="ClientFeedback"), active=set(), today=TODAY)
    assert spaced == camel

    spaced_src = score_element(_el(layer="Source material"), active=set(), today=TODAY)
    camel_src = score_element(_el(layer="SourceMaterial"), active=set(), today=TODAY)
    assert spaced_src == camel_src


def test_client_feedback_outranks_an_unmapped_layer() -> None:
    """The consequence, stated as the ordering it actually broke."""
    feedback = score_element(_el(layer="Client feedback"), active=set(), today=TODAY)
    unmapped = score_element(_el(layer="Nonsense"), active=set(), today=TODAY)
    assert feedback > unmapped


def test_a_lowercased_framing_layer_is_still_ambient() -> None:
    """`FRAMING_LAYERS` was compared with a bare `in` against TitleCase, so
    `layer: brief` silently lost its always-ambient treatment."""
    lower = score_element(_el(layer="brief"), active=set(), today=TODAY)
    title = score_element(_el(layer="Brief"), active=set(), today=TODAY)
    assert lower == title


def test_the_Output_alias_still_counts_as_a_deliverable() -> None:
    """#172 itself: `Output` was an alias nobody folded into `Deliverables`.

    `_layer_key` routes through the shared `canon_layer`, so the alias
    resolves here exactly as it does in the DB-row checks.
    """
    els = (
        _el(id="d1", layer="Output", stage="revised", status="active"),
        _el(id="d2", layer="Deliverable", stage="revised", status="active"),
    )
    assert active_deliverable_ids(els) == {"d1", "d2"}
