from datetime import date

from cp_engine.shell import (
    ShellElement,
    active_deliverable_ids,
    score_element,
)


def _el(**kw) -> ShellElement:
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
    return ShellElement(**base)  # type: ignore[arg-type]


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
