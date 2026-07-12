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

from datetime import date

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


DRIFT_THRESHOLD_DAYS = 7
DRIFT_HEADER = "**⚠ Drift (estimate vs linked reality)**"


def drift_warnings(estimate, bars, meetings=None, *, today=None,
                   threshold_days: int = DRIFT_THRESHOLD_DAYS) -> list[str]:
    """Flag undone items whose estimate date has lost touch with reality.

    Two rules, one warning per item (the meeting rule wins — it names the
    real date, which is more actionable than a bare past-due):

      - a linked meeting's actual `meeting_date` diverges from the item's
        estimated date by more than `threshold_days` (when several meetings
        link, the CLOSEST one is compared — if even that diverges, it's real
        drift, not a prep call);
      - the item is past due (`today` given, estimated date behind it) with
        no done-mark.

    Done items are settled truth and never flagged. `meetings` rows need only
    `work_item_id` + `meeting_date` (list_project_meetings' shape). Returns
    display-ready strings; empty when nothing drifts (or without inputs —
    no `today` disables the past-due rule, no `meetings` the divergence rule).
    """
    idx = _bar_index(bars)
    dates_by_item: dict[str, list] = {}
    for m in meetings or []:
        wid, when = m.get("work_item_id"), m.get("meeting_date")
        if not (wid and when):
            continue
        try:
            dates_by_item.setdefault(str(wid), []).append(
                date.fromisoformat(str(when)[:10]))
        except ValueError:
            continue

    out: list[str] = []
    for item in estimate.all_items():
        bar = (idx["by_id"].get(str(item.id))
               or idx["by_label"].get(item.name.strip().lower()))
        if bar is None or bar.done:
            continue
        due = week_to_date(estimate.start_date, bar.start_week)
        if due is None:
            continue
        linked = dates_by_item.get(str(item.id))
        if linked:
            closest = min(linked, key=lambda d: abs((d - due).days))
            delta = (closest - due).days
            if abs(delta) > threshold_days:
                out.append(
                    f"⚠ {item.name} — linked meeting {closest.isoformat()} "
                    f"vs estimate ~{due.isoformat()} ({delta:+d}d)")
                continue
        if today is not None and due < today:
            out.append(f"⚠ {item.name} — past due ~{due.isoformat()}, "
                       "no done-mark")
    return out


def render_engagement_block(estimate, bars, *, drift=None) -> str:
    """The Agreement projection: phases → deliverables (dated, done-marked)
    and activities, rendered from live estimator data. `drift` is an optional
    pre-computed `drift_warnings` list, rendered as its own sub-block so the
    projection is self-auditing rather than merely true-at-read-time."""
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
    if drift:
        lines.append(DRIFT_HEADER)
        lines.extend(f"- {w}" for w in drift)
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
