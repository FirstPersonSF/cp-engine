"""#322 — cross-project auto-ingest routed every ask to one project and
duplicated cross-routed risks.

Shapes and wording are the real ones: meeting `485c1bf8` (09-21, tagged
sap-5174, covered SalesLoft too — its Fathom action items all landed on
sap-5174), and the $22K stage-hold item that reached slt-5196 twice from
first-person-operations (`391f31fd` as an ask, `9984b759` as a risk).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from tests.test_ingest import _scaffold_minimal_sprint_file
from tests.test_plan_from_transcript import _make_tenant_config, _stub_claude_response

SAP = "sap-5174-vision-update-2026"
SLT = "slt-5196-brand-campaign-26"

ROSTER = [
    SimpleNamespace(code=SAP, name="Vision Update 2026", company_name="SAP Concur",
                    has_agreement=True, company_kind="client"),
    SimpleNamespace(code=SLT, name="Brand Campaign 26", company_name="Salesloft",
                    has_agreement=True, company_kind="client"),
]

ACTION_ITEMS = [
    {"description": "Draft vision/mission/purpose + business case; deliver draft to Michelle/Fiona by Sep 30",
     "assignee": {"name": "Marcello Grande"}},
    {"description": "Formalize message mapping; research SalesLoft interviewees; draft through-line; compile Sean brief; storyboard 60s hero",
     "assignee": {"name": "Drew Fiero"}},
    {"description": "Email Morgan: request SalesLoft interviewee background/pre-recordings; confirm interview-running lead",
     "assignee": {"name": "Drew Fiero"}},
    {"description": "Book the conference room for Thursday",
     "assignee": {"name": "Drew Fiero"}},
]

PLAN_BOTH_BLOCKS = f"""
transcript:
  source: fathom
  path: /tmp/t.txt
projects:
  {SAP}:
    decisions:
      - text: "Schedule shifts ~3 weeks; final delivery moves after Thanksgiving."
        date: "2026-09-21"
  {SLT}:
    decisions:
      - text: "Drew to take the lead on slt-5196 creative prep this week: message mapping and a brief for Sean."
        date: "2026-09-21"
"""

PLAN_SAP_ONLY = f"""
transcript:
  source: fathom
  path: /tmp/t.txt
projects:
  {SAP}:
    decisions:
      - text: "Schedule shifts ~3 weeks; final delivery moves after Thanksgiving."
        date: "2026-09-21"
"""


def _gen(tmp_path, monkeypatch, body, *, code, co_tagged=None):
    from cp_engine.plan_from_transcript import generate_plan

    _stub_claude_response(monkeypatch, body)
    t = tmp_path / "t.txt"
    t.write_text("00:00:01 - Drew Fiero\n  hi\n")
    extra = {"co_tagged": co_tagged} if co_tagged is not None else {}
    return generate_plan(
        config=_make_tenant_config(tmp_path),
        project_code=code,
        transcript_path=t,
        api_key="stub",
        roster=ROSTER,
        action_items=ACTION_ITEMS,
        today="2026-09-21",
        **extra,
    ).plan["projects"]


def _asks(block: dict) -> list[str]:
    return [a["text"] for a in block.get("record-ask", [])]


def test_action_items_follow_the_project_they_name(tmp_path, monkeypatch):
    from cp_engine.asks import ask_hash as _ask_hash

    projects = _gen(tmp_path, monkeypatch, PLAN_BOTH_BLOCKS, code=SAP)
    sap, slt = _asks(projects[SAP]), _asks(projects[SLT])
    assert any(t.startswith("Draft vision/mission") for t in sap)
    assert not any("SalesLoft" in t for t in sap)
    assert sum("SalesLoft" in t for t in slt) == 2
    # No signal either way: stays with the tagged project.
    assert "Book the conference room for Thursday" in sap
    # Moved items carry the TARGET's hash, so _write_ask's dedupe and the
    # ClickUp round trip still recognise them.
    moved = next(a for a in projects[SLT]["record-ask"] if a["text"].startswith("Email Morgan"))
    assert moved["hash"] == _ask_hash(SLT, moved["text"])


def test_co_tagged_meeting_writes_each_action_item_exactly_once(tmp_path, monkeypatch):
    both = [SAP, SLT]
    sap_run = _gen(tmp_path, monkeypatch, PLAN_SAP_ONLY, code=SAP, co_tagged=both)
    slt_plan = PLAN_SAP_ONLY.replace(SAP, SLT)
    slt_run = _gen(tmp_path, monkeypatch, slt_plan, code=SLT, co_tagged=both)
    written = [
        t for run in (sap_run, slt_run) for block in run.values() for t in _asks(block)
    ]
    for item in ACTION_ITEMS:
        assert written.count(item["description"]) == 1, item["description"]
    assert not any("SalesLoft" in t for t in _asks(sap_run[SAP]))
    assert not any(t.startswith("Draft vision") for t in _asks(slt_run[SLT]))


def test_prompt_names_the_co_tagged_projects():
    from cp_engine.plan_from_transcript import _build_prompt

    prompt = _build_prompt(
        transcript="x", project_context="", project_code=SAP,
        transcript_path=Path("/tmp/t.txt"), co_tagged=[SAP, SLT],
    )
    assert f"This meeting is ALSO tagged to: `{SLT}`" in prompt


# ── execute_plan: cross-target dedupe ─────────────────────────────────


def _two_sprint_files(tmp_path: Path) -> tuple[Path, Path]:
    week = tmp_path / "sprints" / "2026-W39"
    a, b = week / f"{SAP}.md", week / f"{SLT}.md"
    _scaffold_minimal_sprint_file(a, SAP)
    _scaffold_minimal_sprint_file(b, SLT)
    return a, b


RISK = "SalesLoft interviewees have no pre-interview briefs heading into the shoot week."


def test_one_item_under_two_projects_is_written_once(tmp_path):
    from cp_engine.ingest import execute_plan

    a, b = _two_sprint_files(tmp_path)
    plan = {"projects": {
        SLT: {"risks": [{"text": RISK, "date": "2026-09-21"}]},
        SAP: {"risks": [{"text": RISK + " ", "date": "2026-09-21"}]},
    }}
    result = execute_plan(plan, tenant_root=tmp_path, today=date(2026, 9, 21), week_iso="2026-W39")
    assert RISK in b.read_text()
    assert RISK not in a.read_text()
    assert len(result.cross_target_duplicates) == 1


def test_shared_true_writes_both_deliberately(tmp_path):
    from cp_engine.ingest import execute_plan

    a, b = _two_sprint_files(tmp_path)
    plan = {"projects": {
        SLT: {"risks": [{"text": RISK, "date": "2026-09-21", "shared": True}]},
        SAP: {"risks": [{"text": RISK, "date": "2026-09-21", "shared": True}]},
    }}
    execute_plan(plan, tenant_root=tmp_path, today=date(2026, 9, 21), week_iso="2026-W39")
    assert RISK in a.read_text() and RISK in b.read_text()


# ── accepted cross-routed item: update, not sibling ───────────────────

EXISTING_ASK = (
    "- [open · 2026-09-15 · SalesLoft AP (via Drew)] Kelly waiting on SalesLoft "
    "(Erika/AP) response to Drew's request to extend the $22K stage-hold deposit "
    "invoice due date to end of September so it lines up with the ~Sep 30 first "
    "payment on SLT-5196; if paid earlier, may need a short-term transfer from "
    "reserves to cover payroll. [cross-routed from first-person-operations · "
    "meeting 391f31fd] <!-- cp:hash=32ed3b66 -->"
)
ROUTED_RISK = {
    "source_code": "first-person-operations",
    "target_code": SLT,
    "verb": "risks",
    "meeting_id": "9984b759-b3ed-4f99-a9b1-1b621a1d645f",
    "text": (
        "SalesLoft ~$22K stage-hold invoice: production company has not replied "
        "to Drew's request to extend to 2026-09-30; if they demand payment sooner, "
        "may need short-term reserve transfer to cover payroll."
    ),
    "payload": {"severity": "watching", "category": "financial", "date": "2026-09-18"},
}


def _seed_target(tmp_path: Path) -> Path:
    _, b = _two_sprint_files(tmp_path)
    body = b.read_text().replace(
        "### Open asks\n", "### Open asks\n" + EXISTING_ASK + "\n", 1
    )
    b.write_text(body)
    return b


def test_cross_routed_restatement_lands_as_an_update_not_a_sibling(tmp_path):
    from cp_engine.cross_project import build_routed_plan
    from cp_engine.ingest import execute_plan
    from cp_engine.sprints import parse_sprint_file

    path = _seed_target(tmp_path)
    plan = build_routed_plan(ROUTED_RISK)
    r1 = execute_plan(plan, tenant_root=tmp_path, today=date(2026, 9, 19), week_iso="2026-W39")
    assert r1.errors == [] and r1.files_written == [path]
    lines = path.read_text().splitlines()
    at = lines.index(EXISTING_ASK)
    assert lines[at + 1].startswith("  - [update · 2026-09-18 · risk] SalesLoft ~$22K stage-hold")
    # Not a second risk.
    assert not any("stage-hold" in r.text for r in parse_sprint_file(path).risks)
    # Re-accepting is a no-op.
    r2 = execute_plan(build_routed_plan(ROUTED_RISK), tenant_root=tmp_path,
                      today=date(2026, 9, 19), week_iso="2026-W39")
    assert r2.files_written == [] and path.read_text().count("[update ·") == 1


def test_cross_routed_item_with_no_match_is_written_normally(tmp_path):
    from cp_engine.cross_project import build_routed_plan
    from cp_engine.ingest import execute_plan

    path = _seed_target(tmp_path)
    other = dict(ROUTED_RISK, text="Sean's crew availability for the Oct 15 shoot is unconfirmed.")
    execute_plan(build_routed_plan(other), tenant_root=tmp_path,
                 today=date(2026, 9, 19), week_iso="2026-W39")
    body = path.read_text()
    assert "[update ·" not in body
    assert "- [watching · financial · 2026-09-18] Sean's crew availability" in body
