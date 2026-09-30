"""#340 — open questions (#321) are read, carried until settled, and surfaced.

v0.126.0 added `record-open-question` → `### Open questions` beside
`### Decisions`, but nothing read the section: no carry-forward, no agenda or
planning-bundle line, and a sprint-planning meeting's unsettled decisions had
no home at all (dropped, counted). These tests pin the read side.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from tests.test_sprints import _cf_kwargs, _fixture_project, _region

Q1 = (
    "- [open question · 2026-09-08] Thoropass or Vanta for SOC 2? "
    "<!-- cp:hash=0a0a0a01 -->\n"
    "  Pradeep wants a proof gate first;\n"
    "  Krupa owns the security review.\n"
)
Q2 = "- [open question · 2026-09-08] Shoot moves to CAB week? <!-- cp:hash=0a0a0a02 -->\n"


def _week(tmp_path, week_iso, prior, *, questions="", **kw):
    """Render one sprint week, then write `questions` into its hand-written
    `### Open questions` — where `record-open-question` puts them."""
    from cp_engine.sprints import ensure_sprint_file

    path = ensure_sprint_file(**{**_cf_kwargs(tmp_path, week_iso, prior), **kw})
    if questions:
        body = path.read_text()
        assert "### Open questions\n" in body
        path.write_text(body.replace("### Open questions\n", "### Open questions\n" + questions, 1))
    return path


def _carried(path):
    from cp_engine.sprints import parse_sprint_file

    return parse_sprint_file(path).carry_forward.open_questions


# ── parse ─────────────────────────────────────────────────────────────


SECTION = """\
## Meeting notes & decisions

### Decisions

- [decision · 2026-09-08] Deck ships Thursday.

### Open questions

- [open question · 2026-09-08] Still open? <!-- cp:hash=00000001 -->
- [answered · 2026-09-08] Settled by token. <!-- cp:hash=00000002 -->
- [resolved · open question · 2026-09-08] Settled, token leads. <!-- cp:hash=00000003 -->
- ~~[open question · 2026-09-08] Struck whole.~~ <!-- cp:hash=00000004 -->
- [open question · 2026-09-08] ~~Struck text.~~ <!-- cp:hash=00000005 -->
- Hand-written, no bracket
  with a continuation.
<!-- <placeholder guidance> -->

### Discussion notes
"""


def test_parse_reads_open_questions_and_their_settled_state():
    from cp_engine.sprints import _parse_open_questions

    qs = _parse_open_questions(SECTION)
    assert [q.is_open for q in qs] == [True, False, False, False, False, True]
    assert qs[0].raised_date == "2026-09-08"
    assert qs[1].status == "answered" and qs[2].status == "resolved"
    assert qs[2].raised_date == "2026-09-08"
    assert qs[5].raised_date == "" and qs[5].note == "with a continuation."


def test_sprint_file_carries_open_questions_field(tmp_path):
    from cp_engine.sprints import parse_sprint_file

    path = _week(tmp_path, "2026-W37", None, questions=Q2)
    sf = parse_sprint_file(path)
    assert [q.text for q in sf.open_questions] == [Q2[len("- [open question · 2026-09-08] "):].strip()]
    # The decision parser still never sees it (#321).
    assert not any("CAB week" in d.text for d in sf.decisions)


# ── carry forward ─────────────────────────────────────────────────────


def test_open_question_carries_until_answered_with_its_continuation(tmp_path):
    """Raised in W37, carried into W38 AND W39 (the walk reads owning files,
    not the prior week's frozen region), continuation and all (#337's lesson)."""
    _week(tmp_path, "2026-W37", None, questions=Q1)
    w38 = _week(tmp_path, "2026-W38", "2026-W37")
    w39 = _week(tmp_path, "2026-W39", "2026-W38")
    for path in (w38, w39):
        carried = _carried(path)
        assert len(carried) == 1
        assert "Thoropass or Vanta" in carried[0].text
        assert carried[0].raised_date == "2026-09-08"
        assert "Krupa owns the security review." in (carried[0].note or "")
    region = _region(w39)
    assert "- [open question · 2026-09-08] Thoropass or Vanta for SOC 2?" in region
    assert "  Krupa owns the security review." in region


def test_open_question_answered_at_origin_stops_carrying(tmp_path, caplog):
    w37 = _week(tmp_path, "2026-W37", None, questions=Q1 + Q2)
    w38 = _week(tmp_path, "2026-W38", "2026-W37")
    assert len(_carried(w38)) == 2  # premise
    body = w37.read_text()
    body = body.replace("[open question · 2026-09-08] Thoropass", "[answered · 2026-09-08] Thoropass", 1)
    body = body.replace("- [open question · 2026-09-08] Shoot moves to CAB week?",
                        "- [open question · 2026-09-08] ~~Shoot moves to CAB week?~~", 1)
    w37.write_text(body)
    with caplog.at_level(logging.WARNING, logger="cp_engine"):
        w39 = _week(tmp_path, "2026-W39", "2026-W38")
    assert _carried(w39) == ()
    assert "_Nothing carried over._" in _region(w39)
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_rerender_with_carried_question_does_not_warn_of_hand_edits(tmp_path, caplog):
    """The carried rows (bullet + indented continuation) are derived: #320's
    discard check must find them in the owning file, not call them hand edits."""
    _week(tmp_path, "2026-W37", None, questions=Q1)
    _week(tmp_path, "2026-W38", "2026-W37")
    with caplog.at_level(logging.WARNING, logger="cp_engine"):
        _week(tmp_path, "2026-W38", "2026-W37")
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_old_open_question_rolls_up_as_stale(tmp_path):
    from cp_engine.sprints import CARRY_FORWARD_MAX_AGE_WEEKS, parse_sprint_file

    monday = date.fromisocalendar(2026, 39, 1)
    past = (monday - timedelta(weeks=CARRY_FORWARD_MAX_AGE_WEEKS, days=1)).isoformat()
    _week(tmp_path, "2026-W30", None,
          questions=f"- [open question · {past}] Old question <!-- cp:hash=0b0b0b01 -->\n")
    w38 = _week(tmp_path, "2026-W38", None)
    w39 = _week(tmp_path, "2026-W39", "2026-W38")
    assert _carried(w39) == ()
    assert (f"- 1 stale open question (oldest {past}) — triage in "
            f"[2026-W30](../2026-W30/{w38.name})") in _region(w39)
    assert parse_sprint_file(w39).carry_forward.stale_count("open_questions") == 1


def test_parent_node_carries_its_own_open_questions(tmp_path):
    """An account meeting's unsettled decisions land on the NODE's sprint
    file (#321). A parent's carry-forward region is its subtree rollup, so
    without this they were never carried."""
    from tests.test_golden_sprints import _program_rollup

    kw = {"subtree_rollup": _program_rollup(), "children_summaries": ()}
    _week(tmp_path, "2026-W37", None, questions=Q2, **kw)
    w38 = _week(tmp_path, "2026-W38", "2026-W37", **kw)
    region = _region(w38)
    assert "subtree rollup" in region  # premise: the parent branch rendered
    assert "- [open question · 2026-09-08] Shoot moves to CAB week?" in region
    assert [q.raised_date for q in _carried(w38)] == ["2026-09-08"]


def test_own_answer_this_week_suppresses_carried_copy(tmp_path):
    from cp_engine.aggregators import open_questions
    from cp_engine.sprints import parse_sprint_file

    _week(tmp_path, "2026-W37", None, questions=Q2)
    w38 = _week(tmp_path, "2026-W38", "2026-W37",
                questions=Q2.replace("[open question ·", "[answered ·"))
    assert len(parse_sprint_file(w38).carry_forward.open_questions) == 1  # premise
    assert open_questions(parse_sprint_file(w38)) == []


# ── surfaces: agenda + planning bundle ────────────────────────────────


def test_agenda_lists_open_questions_beside_decisions_due(tmp_path):
    from cp_engine import agenda
    from cp_engine.sprints import parse_sprint_file

    _week(tmp_path, "2026-W37", None, questions=Q1)
    w38 = _week(tmp_path, "2026-W38", "2026-W37")
    sf = parse_sprint_file(w38)
    qs = agenda._extract_open_questions_for_project(sf.project_code, (sf,))
    assert len(qs) == 1
    text = qs[0]["text"]
    assert "cp:hash" not in text and "Krupa owns the security review." in text
    block = agenda.ProjectAgendaBlock(
        project=_fixture_project(), quick_resume_excerpt=None, last_touched=None,
        last_sprint_hours=None, recent_inbound=(), open_asks_aged=(),
        decisions_due=({"text": "Renew for Q3", "target_date": "by W40"},),
        relevant_weekly_decisions=(), stakeholders=(), discussion_prompt=None,
        open_questions=qs,
    )
    out = "\n".join(agenda._render_project_block(block))
    assert out.index("**Decisions due") < out.index("**Open questions (1):**")
    assert "- [2026-09-08] Thoropass or Vanta for SOC 2?" in out


def test_planning_bundle_lists_open_questions(tmp_path):
    from cp_engine import prep_planning

    _week(tmp_path, "2026-W37", None, questions=Q1)
    w38 = _week(tmp_path, "2026-W38", "2026-W37", questions=Q2)
    qs = prep_planning._sprint_open_questions(w38)
    assert [q["text"][:10] for q in qs] == ["Shoot move", "Thoropass "]
    block = prep_planning.ProjectPlanningBlock(
        project=_fixture_project(), exec_summary=None, milestones=(),
        client_asks=(), sprint_open_asks=(),
        urgent=({"type": "decision_due", "text": "Renew", "severity": "alert"},),
        fetch_error=None, open_questions=qs,
    )
    for render in (prep_planning._render_bundle_project_block,
                   prep_planning._render_project_block):
        out = "\n".join(render(block))
        assert out.index("**Urgent:**") < out.index("**Open questions (2):**")
        assert "- [2026-09-08] Shoot moves to CAB week?" in out


# ── sprint-planning meetings → _week.md ───────────────────────────────


def test_unsettled_sprint_planning_decision_lands_in_week_file(tmp_path):
    """They were dropped (counted). Now: `_week.md` `## Open questions`, read
    back by agenda + bundle, carried across weeks until answered."""
    from cp_engine.ingest import execute_plan
    from cp_engine.ingest_fidelity import apply_decision_fidelity
    from cp_engine.prep_planning import PlanningResult, _render_cross_cutting
    from cp_engine.sprints import week_open_questions

    plan = {
        "projects": {},
        "account_decisions": [
            {"text": "Leaning toward Thoropass for SOC 2.", "scope": "1p",
             "date": "2026-09-14", "week": "2026-W38"},
            {"text": "Invoices route through Brandon.", "scope": "1p",
             "date": "2026-09-14", "week": "2026-W38"},
        ],
    }
    (tmp_path / "master-cp.md").write_text(
        "# Master\n\n## Decisions (cross-cutting, hand-written)\n\n"
    )
    apply_decision_fidelity(plan)
    assert [d["text"] for d in plan["account_decisions"]] == ["Invoices route through Brandon."]
    assert plan["week_open_questions"][0]["scope"] == "1p"

    result = execute_plan(plan, tenant_root=tmp_path, today=date(2026, 9, 14))
    assert result.errors == []
    week_md = tmp_path / "sprints" / "2026-W38" / "_week.md"
    body = week_md.read_text()
    line = next(ln for ln in body.splitlines() if "Thoropass" in ln)
    assert line.startswith("- [open question · 2026-09-14 · 1P] Leaning toward Thoropass")
    assert body.index("## Sprint planning summaries") < body.index("## Open questions")
    # Idempotent.
    again = execute_plan(plan, tenant_root=tmp_path, today=date(2026, 9, 14))
    assert again.skipped_duplicate >= 1 and week_md.read_text() == body

    # Carried: still open two weeks on (no `_week.md` in W39 at all).
    live, stale = week_open_questions(tmp_path / "sprints", "2026-W40")
    assert [(q.scope, q.raised_date) for q in live] == [("1P", "2026-09-14")]
    assert stale is None
    out = "\n".join(_render_cross_cutting(PlanningResult(
        week_iso="2026-W40", week_dates="", project_count=0, estimated_minutes=0,
        tenant_hours_last_week={}, blocks_by_account={}, milestone_counts={},
        urgent_counts={},
        week_open_questions=({"text": "Leaning toward Thoropass for SOC 2.",
                              "raised_date": "2026-09-14", "scope": "1P"},),
    )))
    assert "**Open questions from sprint planning (1):**" in out
    assert "_(no cross-cutting signals this sprint)_" not in out

    # Answered where it was written → gone.
    week_md.write_text(body.replace("[open question · 2026-09-14 · 1P]", "[answered · 2026-09-14 · 1P]"))
    assert week_open_questions(tmp_path / "sprints", "2026-W40")[0] == ()


def test_webhook_forwards_week_open_questions():
    """The sprint-planning endpoint builds a separate summary plan; the new
    key has to ride along or execute_plan never sees it."""
    from pathlib import Path

    src = (Path(__file__).parents[1] / "webhook" / "routers" / "ingest.py").read_text()
    assert 'summary_plan["week_open_questions"] = plan["week_open_questions"]' in src


def test_claude_md_says_how_to_close_an_open_question():
    from tests.test_render import make_tenant
    from cp_engine.render import render_claude_md

    out = render_claude_md(make_tenant(name="1p"))
    assert "an open question with `[answered · …]`" in out
