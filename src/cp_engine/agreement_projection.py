"""SOW-as-projection — the Agreement element's derived engagement block.

The Agreement spine element stores ONLY the human-authored side of the signed
scope (terms, exclusions, change orders). The operational facts — phases,
deliverables, activities, dates, done-ness — belong to the estimator and are
composed into the READ-side projection by `render_engagement_block`, so a
change order edited in the estimate is instantly true in every SOW read and
nothing hand-retyped can rot.

Design: cp tenant `docs/plans/2026-07-11-sow-as-projection-design.md`.
Consumed by `mcp_server.pull_spine_element` (Agreement-layer elements of
engagements; initiatives have no estimate → no block). Pure functions: plain
data in, markdown out. Fail-soft at the call site, never here.
"""

from __future__ import annotations

from cp_engine.estimate import week_to_date

HEADER = "## Engagement shape (derived from the estimate — live)"
FOOTER = ("_Derived at read time from the estimator — edit deliverables, "
          "dates, and phases there, not here. This element's stored body "
          "carries only the human side of the agreement._")


def _bar_index(bars) -> dict:
    """work_item_id → bar, plus a lowercase-label fallback index.

    A bar names its work item via `work_item_id` (mc-2 #183); older bars may
    only match by label. ANY-done-wins when several bars serve one item,
    matching `spine_done.build_done_map`'s rule.
    """
    by_id: dict = {}
    by_label: dict = {}
    for b in bars or []:
        for key, index in ((str(b.work_item_id) if b.work_item_id else None, by_id),
                           ((b.label or "").strip().lower() or None, by_label)):
            if key is None:
                continue
            prior = index.get(key)
            if prior is None or (b.done and not prior.done):
                index[key] = b
    return {"by_id": by_id, "by_label": by_label}


def _item_line(item, estimate, idx) -> str:
    bar = idx["by_id"].get(str(item.id)) or idx["by_label"].get(item.name.strip().lower())
    when = week_to_date(estimate.start_date, bar.start_week) if bar else None
    parts = [item.name]
    if when is not None:
        parts.append(f"due ~{when.isoformat()}")
    if bar is not None and bar.done:
        parts.append("done ✓")
    return " · ".join(parts)


def render_engagement_block(estimate, bars) -> str:
    """The Agreement projection: phases → deliverables (dated, done-marked)
    and activities, rendered from live estimator data."""
    lines = [HEADER, ""]
    idx = _bar_index(bars)
    for phase in estimate.phases:
        lines.append(f"**{phase.name}**")
        deliverables = [i for i in phase.items if i.kind == "deliverable"]
        activities = [i for i in phase.items if i.kind == "activity"]
        for d in deliverables:
            lines.append(f"- Deliverable: {_item_line(d, estimate, idx)}")
        if activities:
            lines.append("- Activities: "
                         + " · ".join(_item_line(a, estimate, idx) for a in activities))
        lines.append("")
    if estimate.start_date:
        lines.append(f"Kickoff: {estimate.start_date}")
        lines.append("")
    lines.append(FOOTER)
    return "\n".join(lines)


def sow_attach_nudge(sources_listing: list[dict]) -> str | None:
    """A one-line nudge naming an ingested SOW-looking doc when the Agreement
    element has no attached source yet ('the scaffold gets smarter')."""
    for doc in sources_listing or []:
        title = (doc.get("title") or "")
        if "sow" in title.lower():
            return (f"_A document named '{title}' is in the source store but "
                    "not attached to this element — attach it so the signed "
                    "scope travels with the agreement._")
    return None
