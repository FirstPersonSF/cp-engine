"""Tests for cp_engine.aggregators (v0.8.5).

Two aggregators tested in isolation against synthetic SprintFile fixtures:
- aggregate_project_strips: per-project scope (inbound, recent decisions,
  open asks, stakeholders).
- aggregate_tenant_strips: tenant-wide scope (cross-cutting decisions,
  themes, carry-forward).

Plus the shared carry_forward_rollup that both master-cp.md's `agenda`
region and weekly-cp.md's carry-forward-strip share.
"""

from __future__ import annotations

from datetime import date

from cp_engine.aggregators import (
    aggregate_project_strips,
    aggregate_tenant_strips,
    carry_forward_rollup,
)
from cp_engine.state import (
    CarryForward,
    ClientAsk,
    DecisionEntry,
    InboundUpdate,
    HorizonItem,
    Risk,
    SprintFacts,
    SprintFile,
    Stakeholder,
    Theme,
    WhereItStands,
)


def _empty_facts() -> SprintFacts:
    return SprintFacts(None, None, None, None, None, 0, 0)


def _empty_where() -> WhereItStands:
    return WhereItStands(None, None, None, (), ())


def _empty_carry() -> CarryForward:
    return CarryForward((), (), ())


def _make_sprint(
    project_code: str = "ggl-5168",
    week_iso: str = "2026-W20",  # ISO 8601 — Mon May 11 2026 is W20
    week_start: str = "2026-05-11",
    *,
    inbound: tuple[InboundUpdate, ...] = (),
    open_asks: tuple[ClientAsk, ...] = (),
    decisions: tuple[DecisionEntry, ...] = (),
    stakeholders: tuple[Stakeholder, ...] = (),
    risks: tuple[Risk, ...] = (),
    horizon: tuple[HorizonItem, ...] = (),
) -> SprintFile:
    return SprintFile(
        project_code=project_code,
        week_iso=week_iso,
        week_start=week_start,
        week_end=week_start,
        prior_sprint=None,
        facts=_empty_facts(),
        where_it_stands=_empty_where(),
        carry_forward=_empty_carry(),
        client_outbound=(),
        client_open_asks=open_asks,
        client_inbound=inbound,
        risks=risks,
        allocation=(),
        deliverables=(),
        definition_of_done="",
        horizon=horizon,
        meeting_notes=None,
        stakeholders=stakeholders,
        decisions=decisions,
    )


# ──────────────────────────────────────────────────────────────────────
#  aggregate_project_strips
# ──────────────────────────────────────────────────────────────────────


def test_project_strips_filters_by_project_code() -> None:
    target = _make_sprint("ggl-5168", inbound=(InboundUpdate("2026-05-12", "Rena", "ack"),))
    other = _make_sprint("ibx-5167", inbound=(InboundUpdate("2026-05-12", "Janet", "noise"),))
    strips = aggregate_project_strips("ggl-5168", (target, other), date(2026, 5, 12))
    assert len(strips.inbound) == 1
    assert strips.inbound[0].who == "Rena"


def test_project_strips_inbound_window_28_days() -> None:
    recent = _make_sprint("p1", inbound=(InboundUpdate("2026-05-01", "x", "recent"),))
    old = _make_sprint(
        "p1", week_start="2026-04-01",
        inbound=(InboundUpdate("2026-04-01", "x", "stale"),),
    )
    strips = aggregate_project_strips("p1", (recent, old), date(2026, 5, 12))
    texts = [ib.text for ib in strips.inbound]
    assert "recent" in texts
    assert "stale" not in texts


def test_project_strips_open_asks_includes_aged_days_and_filters_closed() -> None:
    asks = (
        ClientAsk("still open", asked_date="2026-04-30", status="open", who="Rena"),
        ClientAsk("closed long ago", asked_date="2026-04-30", status="answered", who="Rena"),
    )
    sf = _make_sprint("p1", open_asks=asks)
    strips = aggregate_project_strips("p1", (sf,), date(2026, 5, 12))
    assert len(strips.open_asks) == 1
    assert strips.open_asks[0]["aged_days"] == 12  # May 12 - Apr 30
    assert strips.open_asks[0]["who"] == "Rena"


def test_project_strips_dedupes_stakeholders_by_name_keeping_most_recent() -> None:
    new_sprint = _make_sprint(
        "p1", week_start="2026-05-11",
        stakeholders=(Stakeholder(name="Rena", role="Director", context="updated"),),
    )
    old_sprint = _make_sprint(
        "p1", week_start="2026-05-04",
        stakeholders=(Stakeholder(name="Rena", role="PM", context="old"),),
    )
    strips = aggregate_project_strips("p1", (old_sprint, new_sprint), date(2026, 5, 12))
    assert len(strips.stakeholders) == 1
    # newest sprint wins (most-recent role + context)
    assert strips.stakeholders[0].role == "Director"
    assert strips.stakeholders[0].context == "updated"


# ──────────────────────────────────────────────────────────────────────
#  aggregate_tenant_strips
# ──────────────────────────────────────────────────────────────────────


def test_tenant_strips_collects_only_cross_cutting_decisions() -> None:
    decisions_a = (
        DecisionEntry("local-only", "2026-05-12", cross_cutting=False),
        DecisionEntry("org-wide", "2026-05-12", cross_cutting=True),
    )
    decisions_b = (
        DecisionEntry("another org-wide", "2026-05-12", cross_cutting=True),
    )
    sf_a = _make_sprint("p1", decisions=decisions_a)
    sf_b = _make_sprint("p2", decisions=decisions_b)
    out = aggregate_tenant_strips((sf_a, sf_b), themes=(), today=date(2026, 5, 12))
    texts = [d["text"] for d in out.cross_cutting_decisions]
    assert "org-wide" in texts
    assert "another org-wide" in texts
    assert "local-only" not in texts


def test_tenant_strips_filters_themes_by_14_day_window() -> None:
    recent = Theme(text="recent theme", date="2026-05-10")
    old = Theme(text="old theme", date="2026-04-15")
    out = aggregate_tenant_strips((), themes=(recent, old), today=date(2026, 5, 12))
    assert len(out.themes) == 1
    assert out.themes[0].text == "recent theme"


def test_tenant_strips_carry_forward_includes_escalated_risks_and_stale_asks() -> None:
    sf = _make_sprint(
        "p1",
        risks=(
            Risk(text="big problem", severity="escalated", category="contract", raised_date="2026-05-01"),
            Risk(text="watching", severity="watching", category="schedule", raised_date="2026-05-01"),
        ),
        open_asks=(
            ClientAsk(text="old open", asked_date="2026-04-30", status="open", who="Rena"),  # 12 days
            ClientAsk(text="fresh open", asked_date="2026-05-10", status="open", who="Rena"),  # 2 days
        ),
    )
    out = aggregate_tenant_strips((sf,), themes=(), today=date(2026, 5, 12))
    assert len(out.carry_forward["escalated_risks"]) == 1
    assert out.carry_forward["escalated_risks"][0]["text"] == "big problem"
    assert len(out.carry_forward["stale_asks"]) == 1  # only "old open" passes the > 7d filter
    assert out.carry_forward["stale_asks"][0]["aged_days"] == 12


# ──────────────────────────────────────────────────────────────────────
#  carry_forward_rollup (shared with master-cp.md agenda region)
# ──────────────────────────────────────────────────────────────────────


def test_carry_forward_rollup_horizon_window_includes_only_next_two_sprints() -> None:
    # Today: 2026-05-12 (Tuesday of W20 — ISO 8601 week numbering, v0.10.0+)
    sf = _make_sprint(
        "p1",
        horizon=(
            HorizonItem(text="W19 (past)", bucket="decision", target_date="W19"),
            HorizonItem(text="W20 (current)", bucket="decision", target_date="W20"),
            HorizonItem(text="W21 (next)", bucket="decision", target_date="W21"),
            HorizonItem(text="W22 (next+1)", bucket="decision", target_date="W22"),
            HorizonItem(text="W23 (out of range)", bucket="decision", target_date="W23"),
            HorizonItem(text="non-week target", bucket="decision", target_date="2026-06-01"),
            HorizonItem(text="empty target", bucket="decision", target_date=""),
            HorizonItem(text="not a decision", bucket="milestone", target_date="W21"),
        ),
    )
    out = carry_forward_rollup((sf,), date(2026, 5, 12))
    texts = [d["text"] for d in out["decisions_due"]]
    # Within range: W21 (current+1) and W22 (current+2). Non-week and empty targets pass through.
    assert "W21 (next)" in texts
    assert "W22 (next+1)" in texts
    assert "non-week target" in texts
    assert "empty target" in texts
    # Out of range or wrong bucket: dropped.
    assert "W19 (past)" not in texts
    assert "W20 (current)" not in texts
    assert "W23 (out of range)" not in texts
    assert "not a decision" not in texts
