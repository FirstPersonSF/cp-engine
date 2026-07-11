# tests/test_agreement_projection.py — the SOW read-side projection.
from cp_engine.agreement_projection import (
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
