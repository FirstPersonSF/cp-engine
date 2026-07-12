# tests/test_agreement_projection.py — the SOW read-side projection.
from datetime import date

from cp_engine.agreement_projection import (
    drift_warnings,
    render_engagement_block,
    sow_attach_nudge,
)
from cp_engine.estimate import Estimate, EstimateItem, EstimatePhase, ScheduleItem


def _estimate(start_date="2026-06-15"):
    p1 = EstimatePhase(id="ph1", name="Discovery & Alignment", overview=None, position=0, items=(
        EstimateItem(id="d1", phase_id="ph1", kind="deliverable",
                     name="Perspectives & Possibilities Report",
                     short_description=None, position=0, library_item_id=None),
        EstimateItem(id="a1", phase_id="ph1", kind="activity",
                     name="1:1 Stakeholder Interviews",
                     short_description=None, position=1, library_item_id=None),
    ))
    return Estimate(id="est1", mc_project_id="pid", name="E", phases=(p1,),
                    start_date=start_date)


def _bar(label, wid=None, *, week=2, done=False):
    return ScheduleItem(id=f"b-{label}", label=label, phase_id="ph1",
                        start_week=week, duration=2, item_type="activity",
                        emphasis=None, work_item_id=wid, work_item_kind=None,
                        done=done)


def test_block_renders_phases_deliverables_and_dates():
    bars = [_bar("Perspectives & Possibilities Report", "d1", week=10),
            _bar("1:1 Stakeholder Interviews", "a1", week=2, done=True)]
    out = render_engagement_block(_estimate(), bars)
    assert "Discovery & Alignment" in out
    assert "Deliverable: Perspectives & Possibilities Report · due ~2026-08-24" in out
    assert "1:1 Stakeholder Interviews · due ~2026-06-29 · done ✓" in out
    assert "Kickoff: 2026-06-15" in out
    assert "edit deliverables" in out            # the footer disclaims authorship


def test_label_fallback_when_bar_unlinked():
    bars = [_bar("1:1 stakeholder interviews", None, week=2, done=True)]  # case-insensitive
    out = render_engagement_block(_estimate(), bars)
    assert "1:1 Stakeholder Interviews · due ~2026-06-29 · done ✓" in out


def test_no_start_date_renders_undated_but_complete():
    out = render_engagement_block(_estimate(start_date=None), [])
    assert "Perspectives & Possibilities Report" in out
    assert "due ~" not in out and "Kickoff" not in out


def test_any_done_wins_across_duplicate_bars():
    bars = [_bar("x", "d1", done=False), _bar("x", "d1", done=True)]
    out = render_engagement_block(_estimate(), bars)
    assert "done ✓" in out


# --- drift warnings (#65) ----------------------------------------------------
# _estimate() dates: a1 bar at week 2 → due 2026-06-29; d1 at week 10 →
# due 2026-08-24.

def test_drift_flags_past_due_without_done_mark():
    bars = [_bar("i", "a1", week=2, done=False)]
    out = drift_warnings(_estimate(), bars, [], today=date(2026, 7, 11))
    assert out == ["⚠ 1:1 Stakeholder Interviews — past due ~2026-06-29, "
                   "no done-mark"]


def test_drift_silent_when_done_or_future():
    bars = [_bar("i", "a1", week=2, done=True),      # done → settled
            _bar("r", "d1", week=10, done=False)]    # future → fine
    assert drift_warnings(_estimate(), bars, [], today=date(2026, 7, 11)) == []


def test_drift_flags_diverging_linked_meeting():
    bars = [_bar("i", "a1", week=2, done=False)]
    meetings = [{"work_item_id": "a1", "meeting_date": "2026-07-30"}]
    out = drift_warnings(_estimate(), bars, meetings, today=date(2026, 6, 1))
    assert out == ["⚠ 1:1 Stakeholder Interviews — linked meeting 2026-07-30 "
                   "vs estimate ~2026-06-29 (+31d)"]


def test_drift_meeting_rule_wins_over_past_due():
    bars = [_bar("i", "a1", week=2, done=False)]
    meetings = [{"work_item_id": "a1", "meeting_date": "2026-07-30"}]
    out = drift_warnings(_estimate(), bars, meetings, today=date(2026, 7, 11))
    assert len(out) == 1 and "linked meeting" in out[0]


def test_drift_closest_meeting_within_threshold_is_quiet():
    # Two linked meetings; the closest is 3d off — no drift even though the
    # other is far away (a prep call vs the real session).
    bars = [_bar("i", "a1", week=2, done=False)]
    meetings = [{"work_item_id": "a1", "meeting_date": "2026-07-02"},
                {"work_item_id": "a1", "meeting_date": "2026-08-15"}]
    assert drift_warnings(_estimate(), bars, meetings,
                          today=date(2026, 6, 1)) == []


def test_drift_needs_inputs():
    bars = [_bar("i", "a1", week=2, done=False)]
    # no today + no meetings → nothing can fire
    assert drift_warnings(_estimate(), bars, []) == []
    # malformed meeting rows are skipped, not fatal
    meetings = [{"work_item_id": "a1", "meeting_date": "not-a-date"},
                {"work_item_id": None, "meeting_date": "2026-07-30"}]
    assert drift_warnings(_estimate(), bars, meetings) == []


def test_block_renders_drift_section():
    bars = [_bar("i", "a1", week=2, done=False)]
    warnings = ["⚠ x — past due ~2026-06-29, no done-mark"]
    out = render_engagement_block(_estimate(), bars, drift=warnings)
    assert "**⚠ Drift (estimate vs linked reality)**" in out
    assert "- ⚠ x — past due ~2026-06-29, no done-mark" in out
    # drift renders above the kickoff/footer tail
    assert out.index("Drift") < out.index("Kickoff")


def test_block_without_drift_is_unchanged():
    out = render_engagement_block(_estimate(), [])
    assert "Drift" not in out


def test_pull_tool_surfaces_drift_warnings(monkeypatch):
    import cp_engine.mcp_server as m
    monkeypatch.setattr(m, "_resolve", lambda code: (object(), "pid", "cid"))
    monkeypatch.setattr("cp_engine.project_sources.pull_spine",
                        lambda c, pid, key, cid=None: {
                            "est_item_id": "_authored/sow", "layer": "Agreement",
                            "body": "human terms", "sources": ["s1"]})
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate",
                        lambda c, pid: _estimate())
    monkeypatch.setattr("cp_engine.estimate.fetch_schedule",
                        lambda c, est_id: [_bar("i", "a1", week=2)])
    monkeypatch.setattr("cp_engine.project_sources.list_project_meetings",
                        lambda c, pid: [{"work_item_id": "a1",
                                         "meeting_date": "2026-07-30"}])
    res = m.pull_spine_element("p", "sow")
    assert res["derived_block"] is True
    assert res["drift_warnings"] == [
        "⚠ 1:1 Stakeholder Interviews — linked meeting 2026-07-30 "
        "vs estimate ~2026-06-29 (+31d)"]
    assert "⚠ Drift" in res["body"]


def test_attach_nudge_names_sow_looking_doc():
    nudge = sow_attach_nudge([{"title": "Kickoff deck"},
                              {"title": "SAP 5171 Display Ads SOW v02.docx"}])
    assert nudge and "SOW v02.docx" in nudge


def test_attach_nudge_none_when_no_sow_doc():
    assert sow_attach_nudge([{"title": "Kickoff deck"}]) is None


def test_pull_tool_composes_block_for_agreement(monkeypatch):
    import cp_engine.mcp_server as m
    monkeypatch.setattr(m, "_resolve", lambda code: (object(), "pid", "cid"))
    monkeypatch.setattr("cp_engine.project_sources.pull_spine",
                        lambda c, pid, key, cid=None: {
                            "est_item_id": "_authored/sow", "layer": "Agreement",
                            "body": "human terms", "sources": ["s1"]})
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate",
                        lambda c, pid: _estimate())
    monkeypatch.setattr("cp_engine.estimate.fetch_schedule",
                        lambda c, est_id: [])
    res = m.pull_spine_element("p", "sow")
    assert res["derived_block"] is True
    assert res["body"].startswith("human terms")
    assert "Engagement shape" in res["body"]
    assert "attach_nudge" not in res            # has a source attached


def test_pull_tool_fail_soft_without_estimate(monkeypatch):
    import cp_engine.mcp_server as m
    monkeypatch.setattr(m, "_resolve", lambda code: (object(), "pid", None))
    monkeypatch.setattr("cp_engine.project_sources.pull_spine",
                        lambda c, pid, key, cid=None: {
                            "est_item_id": "_authored/sow", "layer": "Agreement",
                            "body": "human terms", "sources": []})
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate", lambda c, pid: None)
    res = m.pull_spine_element("initiative", "sow")
    assert res["body"] == "human terms" and "derived_block" not in res


def test_pull_tool_untouched_for_non_agreement(monkeypatch):
    import cp_engine.mcp_server as m
    monkeypatch.setattr(m, "_resolve", lambda code: (object(), "pid", "cid"))
    monkeypatch.setattr("cp_engine.project_sources.pull_spine",
                        lambda c, pid, key, cid=None: {
                            "est_item_id": "_authored/x", "layer": "Note",
                            "body": "b", "sources": []})
    res = m.pull_spine_element("p", "x")
    assert res["body"] == "b" and "derived_block" not in res
