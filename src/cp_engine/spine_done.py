"""Derived spine `done` — reads through to the Gantt bar's completion.

`done` is NEVER stored on the spine; it mirrors `estimator.schedule_items.done`
via the work-item link. ANY bar marked done ⇒ done (the app-wide Rule-1
semantics from `execution_status.derive_status`), so the spine's `done` matches
the Gantt execution-status badge exactly. Three states:
  True  — bound to a work-item that has a done bar
  False — bound, but no bar is done
  None  — not bound to a real work-item (n/a)
"""
from __future__ import annotations

from cp_engine.estimate import fetch_estimate, fetch_schedule


def _field(bar, key, default=None):
    if isinstance(bar, dict):
        return bar.get(key, default)
    return getattr(bar, key, default)


def build_done_map(bars) -> dict:
    """Map work_item_id → done(bool), folding multiple bars with ANY-done wins.
    Bars without a work_item_id (schedule-native: milestones, holidays) are
    ignored — they bind to no spine element."""
    out: dict = {}
    for b in bars or []:
        wid = _field(b, "work_item_id")
        if wid is None:
            continue
        out[wid] = bool(out.get(wid, False) or _field(b, "done", False))
    return out


def derive_done(est_item_id, done_map: dict):
    """True/False if `est_item_id` is a real work-item in the schedule map;
    None when it isn't bound to one (unbound/orphaned/_authored → n/a)."""
    if not est_item_id or est_item_id not in done_map:
        return None
    return done_map[est_item_id]


def fetch_project_done_map(client, mc_project_id) -> dict:
    """The project's work_item_id → done map, or {} when it has no estimate.
    Two reads against the `estimator` schema (estimate, then its schedule)."""
    est = fetch_estimate(client, mc_project_id)
    if est is None:
        return {}
    return build_done_map(fetch_schedule(client, est.id))
