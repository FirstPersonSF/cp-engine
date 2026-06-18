from datetime import date

from cp_engine.estimate import Estimate, EstimateItem, EstimatePhase, ScheduleItem
from cp_engine.where import build_where_report, fetch_substance_status


def _estimate():
    """Two-phase estimate with a handful of work items, start_date 2026-04-27."""
    ph0 = EstimatePhase(
        id="ph-0",
        name="Message Strategy & Campaign Framing",
        overview=None,
        position=0,
        items=(
            EstimateItem("a-0", "ph-0", "activity", "Narrative Messaging Architecture", None, 0, None),
            EstimateItem("a-1", "ph-0", "activity", "Leadership Alignment", None, 1, None),
            EstimateItem("d-0", "ph-0", "deliverable", "Messaging System", None, 2, None),
        ),
    )
    ph1 = EstimatePhase(
        id="ph-1",
        name="Campaign Creative & Workflow Enablement",
        overview=None,
        position=1,
        items=(
            EstimateItem("a-2", "ph-1", "activity", "Campaign Asset Development", None, 0, None),
        ),
    )
    return Estimate(
        id="est-1",
        mc_project_id="mc-1",
        name="IBX-5153 — AI Campaign",
        phases=(ph0, ph1),
        start_date="2026-04-27",
    )


def _schedule():
    return [
        # a-0: bar overlaps today (W0 + 10w → 2026-04-27..2026-07-06)
        ScheduleItem("s-0", "Narrative Messaging Architecture", "ph-0", 0.0, 10.0,
                     "activity", None, work_item_id="a-0"),
        # a-1: past-ended bar (W1 + 2w → ends 2026-05-25), no substance → flag
        ScheduleItem("s-1", "Leadership Alignment", "ph-0", 1.0, 2.0,
                     "activity", None, work_item_id="a-1"),
        # a-2: future bar (W9) → next
        ScheduleItem("s-2", "Campaign Asset Development", "ph-1", 9.0, 4.0,
                     "activity", None, work_item_id="a-2"),
        # native milestone
        ScheduleItem("m-0", "Messaging Workshop", "ph-0", 7.0, 0.0,
                     "milestone", "important", work_item_id=None),
        # native holiday
        ScheduleItem("h-0", "Memorial Day", None, 4.0, 0.0,
                     "holiday", None, work_item_id=None),
    ]


def _substance():
    # d-0 has live, recent substance; everything else absent.
    return {
        "d-0": {"has_live": True, "version_dates": ["2026-06-12"]},
    }


def test_build_where_report_full():
    today = date(2026, 6, 15)
    out = build_where_report(_estimate(), _schedule(), _substance(), today=today)

    # (a) project name + week-of-N + start date
    assert "IBX-5153 — AI Campaign" in out
    assert "week 8 of 13" in out  # ~7 weeks elapsed → "week 8"; span = latest bar end W13
    assert "2026-04-27" in out

    # (c) phase headers
    assert "Message Strategy & Campaign Framing" in out
    assert "Campaign Creative & Workflow Enablement" in out

    # (b) every work-item appears
    for name in (
        "Narrative Messaging Architecture",
        "Leadership Alignment",
        "Messaging System",
        "Campaign Asset Development",
    ):
        assert name in out

    # status words
    assert "active" in out   # a-0 overlapping bar
    assert "flag" in out     # a-1 past-ended, no substance
    assert "next" in out     # a-2 future bar

    # a live+recent deliverable shows active
    # (find the Messaging System line, assert it is active)
    msg_line = next(l for l in out.splitlines() if "Messaging System" in l)
    assert "active" in msg_line

    # a past-ended-no-substance item shows flag
    la_line = next(l for l in out.splitlines() if "Leadership Alignment" in l)
    assert "flag" in la_line

    # (e) every factual line carries a [source] marker
    assert "[" in msg_line and "]" in msg_line
    assert "[" in la_line and "]" in la_line

    # (d) native events listed separately
    assert "Messaging Workshop" in out
    assert "Memorial Day" in out
    # native events appear under their own heading, not inside a phase block
    assert "Native schedule events" in out or "Schedule events" in out


def test_build_where_report_no_start_date():
    """start_date=None: no calendar dates, still lists every item by status."""
    est = _estimate()
    est_no_date = Estimate(
        id=est.id, mc_project_id=est.mc_project_id, name=est.name,
        phases=est.phases, start_date=None,
    )
    out = build_where_report(est_no_date, _schedule(), _substance(), today=date(2026, 6, 15))

    # still lists every work item
    for name in (
        "Narrative Messaging Architecture",
        "Leadership Alignment",
        "Messaging System",
        "Campaign Asset Development",
    ):
        assert name in out
    # no "week N of M" header (no calendar anchor) but project name still shows
    assert est.name in out
    assert "week" not in out.lower() or "of" not in out.lower()


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows
        self._filtered = rows

    def select(self, cols):
        return self

    def eq(self, col, val):
        self._filtered = [r for r in self._rows if r.get(col) == val]
        return self

    def execute(self):
        return type("R", (), {"data": self._filtered})()


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        assert name == "spine_substance"
        return _FakeTable(self._rows)


def test_fetch_substance_status():
    pc = "ibx-5153-ai-campaign"
    rows = [
        {"est_item_id": "d-0", "status": "live", "version_date": "2026-06-12",
         "version_label": "v3", "binding": "live", "project_code": pc},
        {"est_item_id": "d-0", "status": "superseded", "version_date": "2026-05-01",
         "version_label": "v1", "binding": "live", "project_code": pc},
        {"est_item_id": "a-1", "status": "draft", "version_date": "2026-05-10",
         "version_label": "v1", "binding": "live", "project_code": pc},
    ]
    client = _FakeClient(rows)
    out = fetch_substance_status(client, "ibx-5153-ai-campaign")
    assert out["d-0"]["has_live"] is True
    assert "2026-06-12" in out["d-0"]["version_dates"]
    assert "2026-05-01" in out["d-0"]["version_dates"]
    # a-1 has a row but no live version
    assert out["a-1"]["has_live"] is False
    assert out["a-1"]["version_dates"] == ["2026-05-10"]
