"""Tests for cp_engine.prep_planning — Task 6 of v0.15.0.

Covers:
  - ClickUp REST fetch + normalization (happy path, 4xx, missing fields)
  - account grouping
  - per-project rendering (forward calendar, commitments table)
  - missing list_id placeholder
  - --summary JSON shape
  - Task 7 stub boundary (_detect_urgent returns [])
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from cp_engine import (
    ProjectState,
    SyncConfig,
    TenantConfig,
)
from cp_engine import prep_planning
from cp_engine.prep_planning import (
    Milestone,
    ProjectPlanningBlock,
    _detect_urgent,
    _fetch_clickup_milestones,
    _group_by_account,
    _normalize_clickup_task,
    build_planning_result,
    render_planning_doc,
    render_planning_summary,
)


# ──────────────────────────────────────────────────────────────────────
#  Fixtures
# ──────────────────────────────────────────────────────────────────────


def make_config(tenant_root: Path) -> TenantConfig:
    return TenantConfig(
        name="firstpersonsf",
        display="First Person Internal",
        engine_version_constraint="~= 0.1",
        sync=SyncConfig(
            backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="ref"
        ),
        projects=(),
        root=tenant_root,
    )


def make_state(
    code: str,
    *,
    name: str | None = None,
    company_kind: str = "client",
    company_code: str | None = "GGL",
    company_name: str | None = "Google",
    status: str = "Open",
    source: str = "engagement",
    owner: str = "drew",
    is_internal: bool = False,
) -> ProjectState:
    return ProjectState(
        code=code,
        name=name if name is not None else code,
        source=source,  # type: ignore[arg-type]
        company_kind=company_kind,  # type: ignore[arg-type]
        company_code=company_code,
        company_name=company_name,
        status=status,
        is_internal=is_internal,
        owner=owner,
        last_touched=datetime(2026, 6, 1, tzinfo=timezone.utc),
        deadline=None,
        one_line_summary=None,
    )


# A fake httpx response object — just enough for prep_planning's use.
class FakeResp:
    def __init__(self, status_code: int, json_data: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text or ""

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeClient:
    """Stand-in for httpx.Client. Routes by (url, tag) → FakeResp.

    The "tag" key matches the ``tags[]`` query param so milestone vs
    client-ask fetches can return different payloads from the same list.
    """

    def __init__(self, responses: dict[tuple[str, str], FakeResp]):
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    def get(self, url: str, *, headers=None, params=None):
        tag = ""
        if params:
            for k, v in params:
                if k == "tags[]":
                    tag = v
                    break
        self.calls.append((url, tag))
        return self._responses.get((url, tag), FakeResp(404, text="not mocked"))


# ──────────────────────────────────────────────────────────────────────
#  Test 1: fetch returns normalized shape
# ──────────────────────────────────────────────────────────────────────


def test_fetch_milestones_returns_normalized_shape():
    """A mocked ClickUp JSON response normalizes into the documented Milestone keys."""
    list_id = "L1"
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    payload = {
        "tasks": [
            {
                "id": "T1",
                "name": "Pop-up final",
                "due_date": "1812844800000",  # 2027-06-06 ish — date is asserted approximately
                "assignees": [{"username": "brandon"}],
                "custom_fields": [
                    {"name": "Confidence", "value": "medium"},
                    {"name": "Type", "value": "milestone"},
                    {"name": "Linked To", "value": "ggl-5168, ggl-5151"},
                ],
                "tags": [{"name": "milestone"}],
                "status": {"status": "open"},
                "dependencies": [{"task_id": "dep-1"}, {"task_id": "dep-2"}],
            }
        ]
    }
    client = FakeClient({(url, "milestone"): FakeResp(200, payload)})

    raw = _fetch_clickup_milestones(
        list_id, tag="milestone", token="tok_test", client=client
    )
    assert len(raw) == 1
    normalized = _normalize_clickup_task(raw[0])
    # Verify every key the renderer + Task 7 will rely on.
    assert normalized["id"] == "T1"
    assert normalized["deliverable"] == "Pop-up final"
    assert normalized["owner"] == "brandon"
    assert normalized["confidence"] == "medium"
    assert normalized["task_type"] == "milestone"
    assert normalized["depends_on"] == ["dep-1", "dep-2"]
    assert normalized["status"] == "open"
    assert normalized["linked_to"] == ["ggl-5168", "ggl-5151"]
    # Date is derived from the ms timestamp — we only verify the shape.
    assert isinstance(normalized["date"], str) and len(normalized["date"]) == 10


# ──────────────────────────────────────────────────────────────────────
#  Test 2: 4xx from ClickUp surfaces as RuntimeError, caller catches it
# ──────────────────────────────────────────────────────────────────────


def test_fetch_milestones_handles_clickup_4xx_returns_empty_with_error_logged(tmp_path):
    """ClickUp returning 404 raises RuntimeError; build_project_block converts
    it into a fetch_error on the block so rendering continues."""
    list_id = "MISSING"
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    client = FakeClient(
        {
            (url, "milestone"): FakeResp(404, text="list not found"),
            (url, "client-ask"): FakeResp(404, text="list not found"),
        }
    )

    with pytest.raises(RuntimeError, match="404"):
        _fetch_clickup_milestones(
            list_id, tag="milestone", token="tok_test", client=client
        )

    # And the renderer degrades per-project rather than crashing.
    config = make_config(tmp_path)
    state = make_state("ggl-5168", name="GGL 5168 Activation")
    block = prep_planning.build_project_block(
        state,
        config=config,
        supabase_client=None,
        today=date(2026, 6, 7),
        week_iso="2026-W24",
        clickup_client=client,
        clickup_token="tok_test",
        list_id_override="MISSING",
    )
    assert block.fetch_error is not None
    assert "404" in block.fetch_error
    assert block.milestones == ()


# ──────────────────────────────────────────────────────────────────────
#  Test 3: missing custom fields default to "medium" confidence
# ──────────────────────────────────────────────────────────────────────


def test_normalize_handles_missing_custom_fields():
    """A ClickUp task with NO custom_fields at all gets confidence='medium'."""
    task = {
        "id": "T2",
        "name": "Bare task",
        "due_date": None,
        "assignees": [],
        "tags": [{"name": "milestone"}],
        "status": {"status": "open"},
    }
    norm = _normalize_clickup_task(task)
    assert norm["confidence"] == "medium"
    assert norm["owner"] == "—"
    assert norm["date"] == ""
    assert norm["depends_on"] == []
    assert norm["linked_to"] == []
    # Defaulted task_type — falls back to "milestone" without a Type field
    # and without the client-ask tag.
    assert norm["task_type"] == "milestone"


def test_normalize_routes_client_ask_by_tag_when_no_type_field():
    """When the Type custom field is missing, tags[]=client-ask routes the type."""
    task = {
        "id": "T3",
        "name": "Round 3 feedback",
        "due_date": None,
        "assignees": [{"username": "rena"}],
        "tags": [{"name": "client-ask"}],
        "status": {"status": "open"},
        "custom_fields": [],
    }
    norm = _normalize_clickup_task(task)
    assert norm["task_type"] == "client_ask"
    assert norm["owner"] == "rena"


# ──────────────────────────────────────────────────────────────────────
#  Test 4: render groups by account
# ──────────────────────────────────────────────────────────────────────


def test_render_planning_doc_groups_by_account():
    """Three accounts → three ## headers in alphabetical order."""
    blocks = (
        ProjectPlanningBlock(
            project=make_state("ggl-1", company_name="Google"),
            quick_resume_line=None,
            milestones=(),
            client_asks=(),
            sprint_open_asks=(),
            urgent=(),
            fetch_error=None,
        ),
        ProjectPlanningBlock(
            project=make_state("ibx-1", company_code="IBX", company_name="Infoblox"),
            quick_resume_line=None,
            milestones=(),
            client_asks=(),
            sprint_open_asks=(),
            urgent=(),
            fetch_error=None,
        ),
        ProjectPlanningBlock(
            project=make_state(
                "snt-1", company_code="SNT", company_name="Sentinel One"
            ),
            quick_resume_line=None,
            milestones=(),
            client_asks=(),
            sprint_open_asks=(),
            urgent=(),
            fetch_error=None,
        ),
    )
    by_account = _group_by_account(blocks)
    assert list(by_account.keys()) == ["Google", "Infoblox", "Sentinel One"]
    assert len(by_account["Google"]) == 1


# ──────────────────────────────────────────────────────────────────────
#  Test 5: forward calendar renders ascending by date
# ──────────────────────────────────────────────────────────────────────


def test_render_planning_doc_renders_forward_calendar():
    """Feed 2 milestones out of order — output dated bullets in date order ascending."""
    state = make_state("ggl-5168", name="GGL 5168 Activation")
    ms1: Milestone = Milestone(
        id="A", task_type="milestone", deliverable="Later thing",
        date="2026-06-20", owner="drew", confidence="high",
        depends_on=[], status="open", linked_to=[],
    )
    ms2: Milestone = Milestone(
        id="B", task_type="milestone", deliverable="Earlier thing",
        date="2026-06-08", owner="tony", confidence="low",
        depends_on=["dep-1"], status="open", linked_to=[],
    )
    block = ProjectPlanningBlock(
        project=state,
        quick_resume_line="we are here",
        # Build expects pre-sorted; build_project_block sorts inside.
        milestones=tuple(sorted([ms1, ms2], key=lambda m: m["date"])),
        client_asks=(),
        sprint_open_asks=(),
        urgent=(),
        fetch_error=None,
    )
    out = "\n".join(prep_planning._render_forward_calendar(block))
    assert "Earlier thing" in out
    assert "Later thing" in out
    # Verify ordering: "Earlier" appears before "Later" in the string.
    assert out.index("Earlier thing") < out.index("Later thing")
    assert "depends_on: dep-1" in out


# ──────────────────────────────────────────────────────────────────────
#  Test 6: commitments table includes all three categories
# ──────────────────────────────────────────────────────────────────────


def test_render_planning_doc_renders_open_commitments_table():
    """Internal milestone + client-ask + sprint-file ask all appear in the table."""
    state = make_state("ggl-5168", name="GGL 5168 Activation")
    ms: Milestone = Milestone(
        id="M1", task_type="milestone", deliverable="Roadshow plan",
        date="2026-06-03", owner="brandon", confidence="high",
        depends_on=[], status="open", linked_to=[],
    )
    ask: Milestone = Milestone(
        id="A1", task_type="client_ask", deliverable="Round 3 feedback",
        date="2026-06-05", owner="rena", confidence="medium",
        depends_on=[], status="open", linked_to=[],
    )
    sprint_ask = {
        "text": "share new mock",
        "who": "geoff",
        "asked": "2026-05-28",
        "by": "2026-06-10",
        "hash": "abc12345",
    }
    block = ProjectPlanningBlock(
        project=state,
        quick_resume_line=None,
        milestones=(ms,),
        client_asks=(ask,),
        sprint_open_asks=(sprint_ask,),  # type: ignore[arg-type]
        urgent=(),
        fetch_error=None,
    )
    out = "\n".join(prep_planning._render_commitments_table(block))
    assert "| Who | Owes what | To | By |" in out
    assert "Roadshow plan" in out
    assert "brandon" in out
    assert "Round 3 feedback" in out
    assert "rena" in out
    assert "share new mock" in out
    assert "geoff" in out
    assert "(sprint file)" in out


# ──────────────────────────────────────────────────────────────────────
#  Test 7: empty milestone list renders the "no milestones tracked yet" line
# ──────────────────────────────────────────────────────────────────────


def test_render_planning_doc_handles_empty_clickup_list(tmp_path):
    """Project HAS a list_id but ClickUp returns 0 tasks → placeholder."""
    config = make_config(tmp_path)
    state = make_state("ggl-5168", name="GGL 5168 Activation")
    list_id = "EMPTY"
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    client = FakeClient(
        {
            (url, "milestone"): FakeResp(200, {"tasks": []}),
            (url, "client-ask"): FakeResp(200, {"tasks": []}),
        }
    )
    block = prep_planning.build_project_block(
        state,
        config=config,
        supabase_client=None,
        today=date(2026, 6, 7),
        week_iso="2026-W24",
        clickup_client=client,
        clickup_token="tok_test",
        list_id_override=list_id,
    )
    assert block.fetch_error is None
    assert block.milestones == ()
    rendered = "\n".join(prep_planning._render_forward_calendar(block))
    assert "no milestones tracked yet" in rendered


# ──────────────────────────────────────────────────────────────────────
#  Test 8: project without a clickup_list_id renders the "not set" line
# ──────────────────────────────────────────────────────────────────────


def test_render_planning_doc_handles_project_without_clickup_list(tmp_path):
    """No list_id resolvable → block renders the (ClickUp list not set) line."""
    config = make_config(tmp_path)
    state = make_state("ggl-9999", name="No List Yet")
    block = prep_planning.build_project_block(
        state,
        config=config,
        supabase_client=None,  # no Supabase client → can't resolve list_id
        today=date(2026, 6, 7),
        week_iso="2026-W24",
        clickup_client=None,
        clickup_token=None,
        list_id_override=None,
    )
    assert block.fetch_error == "no clickup_list_id"
    rendered = "\n".join(prep_planning._render_forward_calendar(block))
    assert "ClickUp list not set" in rendered


# ──────────────────────────────────────────────────────────────────────
#  Test 9: --summary mode emits valid JSON with the documented keys
# ──────────────────────────────────────────────────────────────────────


def test_summary_mode_emits_json(tmp_path):
    """render_planning_summary returns a JSON string with the contracted keys."""
    config = make_config(tmp_path)
    state = make_state("ggl-5168", name="GGL 5168 Activation")
    # No list_id_lookup, no Supabase → block degrades to "no clickup_list_id"
    # but the summary still renders.
    summary_str = render_planning_summary(
        config,
        (state,),
        today=date(2026, 6, 7),
        tenant_hours_last_week={"Drew": 52, "Tony": 50},
    )
    data = json.loads(summary_str)
    # Documented keys per Task 6 spec.
    expected_keys = {
        "week_iso",
        "week_dates",
        "project_count",
        "estimated_minutes",
        "tenant_hours_last_week",
        "milestone_counts",
        "urgent_counts",
        "errors",
    }
    assert expected_keys.issubset(data.keys())
    assert data["project_count"] == 1
    assert data["tenant_hours_last_week"] == {"Drew": 52, "Tony": 50}
    # Urgent counts shape — all zero today (Task 7 not landed).
    assert data["urgent_counts"] == {
        "slip_risk": 0,
        "decision_due": 0,
        "past_due_ask": 0,
        "escalated_risk": 0,
    }
    # Milestone counts shape — total/fetched/errored present, all integers.
    for k in ("total", "fetched", "errored"):
        assert k in data["milestone_counts"]
        assert isinstance(data["milestone_counts"][k], int)


# ──────────────────────────────────────────────────────────────────────
#  Test 10: cruising project (no urgent signals) returns empty list
# ──────────────────────────────────────────────────────────────────────


# Fixed "today" for every urgent-detection test — pins the 14d slip window.
_TODAY = date(2026, 6, 2)


def _ms(
    *,
    deliverable: str = "Deliverable",
    date_str: str = "",
    confidence: str = "medium",
    depends_on: list[str] | None = None,
    status: str = "open",
) -> Milestone:
    """Compact Milestone factory for urgent tests."""
    return Milestone(
        id="x",
        task_type="milestone",
        deliverable=deliverable,
        date=date_str,
        owner="drew",
        confidence=confidence,
        depends_on=depends_on or [],
        status=status,
        linked_to=[],
    )


def _ask(*, text: str, by: str, who: str = "drew") -> dict:
    """Compact SprintAsk factory (returned as dict for TypedDict use)."""
    return {
        "text": text,
        "who": who,
        "asked": "2026-05-01",
        "by": by,
        "hash": "deadbeef",
    }


def test_cruising_project_returns_empty_list():
    """No milestones, no asks, no sprint file → no urgent flags."""
    state = make_state("ggl-5168")
    assert _detect_urgent(state, (), (), today=_TODAY) == []


# ──────────────────────────────────────────────────────────────────────
#  Rule 1 — slip_risk
# ──────────────────────────────────────────────────────────────────────


def test_slip_risk_low_confidence_milestone_in_next_14d_flags():
    """A low-confidence milestone due in the 14-day window flags as slip_risk."""
    state = make_state("ggl-5168")
    m = _ms(deliverable="Pop-up R3", date_str="2026-06-08", confidence="low")
    flags = _detect_urgent(state, (m,), (), today=_TODAY)
    assert len(flags) == 1
    assert flags[0]["type"] == "slip_risk"
    assert "Pop-up R3" in flags[0]["text"]
    assert "low confidence" in flags[0]["text"]
    assert flags[0]["severity"] == "warn"


def test_slip_risk_stale_depends_on_flags():
    """A milestone whose depends_on matches a past-due sprint ask flags."""
    state = make_state("ggl-5168")
    m = _ms(
        deliverable="Workshop prep",
        date_str="2026-06-10",
        confidence="medium",
        depends_on=["rena-feedback"],
    )
    asks = (
        _ask(text="Awaiting rena-feedback on R3 mocks", by="2026-05-26"),
    )
    flags = _detect_urgent(state, (m,), asks, today=_TODAY)
    # One slip_risk (and Rule 3 also flags the ask as past_due).
    slip = [f for f in flags if f["type"] == "slip_risk"]
    assert len(slip) == 1
    assert "rena-feedback stale 7d" in slip[0]["text"]


def test_slip_risk_outside_14d_window_no_flag():
    """A low-confidence milestone due 30 days out does NOT flag."""
    state = make_state("ggl-5168")
    m = _ms(deliverable="Future thing", date_str="2026-07-02", confidence="low")
    flags = _detect_urgent(state, (m,), (), today=_TODAY)
    assert flags == []


def test_slip_risk_already_shipped_no_flag():
    """A shipped milestone in the window doesn't flag even with low confidence."""
    state = make_state("ggl-5168")
    m = _ms(
        deliverable="Already done",
        date_str="2026-06-08",
        confidence="low",
        status="shipped",
    )
    flags = _detect_urgent(state, (m,), (), today=_TODAY)
    assert flags == []


# ──────────────────────────────────────────────────────────────────────
#  Rule 2 — decision_due
# ──────────────────────────────────────────────────────────────────────


_SPRINT_BODY_DECISIONS_TEMPLATE = """## Horizon — 4–8 weeks out

### Milestones

### Decisions due
{decisions}

### Opportunities
"""


def _body_with_decisions(*decisions: str) -> str:
    """Build a minimal sprint body whose Decisions due section holds bullets."""
    return _SPRINT_BODY_DECISIONS_TEMPLATE.format(decisions="\n".join(decisions))


def test_decision_due_this_sprint_flags():
    """A decision marked `[this sprint]` surfaces a decision_due flag."""
    state = make_state("ggl-5168")
    body = _body_with_decisions(
        "- [this sprint] Pick a deck template for the workshop"
    )
    flags = _detect_urgent(state, (), (), today=_TODAY, sprint_file_body=body)
    assert len(flags) == 1
    assert flags[0]["type"] == "decision_due"
    assert "deck template" in flags[0]["text"]
    assert flags[0]["severity"] == "alert"


def test_decision_due_next_sprint_flags():
    """A decision marked `[next sprint]` also surfaces."""
    state = make_state("ggl-5168")
    body = _body_with_decisions(
        "- [next sprint] Reconcile Engage/Execute/Extend frame"
    )
    flags = _detect_urgent(state, (), (), today=_TODAY, sprint_file_body=body)
    assert len(flags) == 1
    assert flags[0]["type"] == "decision_due"


def test_decision_due_later_horizon_no_flag():
    """A decision dated 60 days out does NOT flag."""
    state = make_state("ggl-5168")
    body = _body_with_decisions(
        "- [2026-08-15] Q3 portfolio direction"
    )
    flags = _detect_urgent(state, (), (), today=_TODAY, sprint_file_body=body)
    assert flags == []


# ──────────────────────────────────────────────────────────────────────
#  Rule 3 — past_due_ask
# ──────────────────────────────────────────────────────────────────────


def test_past_due_ask_flags_with_age():
    """A sprint ask with `by` < today flags with the age in days."""
    state = make_state("ggl-5168")
    asks = (_ask(text="Share R3 mocks", by="2026-05-26"),)  # 7d past due
    flags = _detect_urgent(state, (), asks, today=_TODAY)
    assert len(flags) == 1
    assert flags[0]["type"] == "past_due_ask"
    assert "7d past due" in flags[0]["text"]
    assert flags[0]["severity"] == "warn"  # <14d


def test_past_due_ask_today_no_flag():
    """`by == today` is not yet past due → no flag."""
    state = make_state("ggl-5168")
    asks = (_ask(text="Send the deck", by="2026-06-02"),)
    flags = _detect_urgent(state, (), asks, today=_TODAY)
    assert flags == []


# ──────────────────────────────────────────────────────────────────────
#  Rule 4 — escalated_risk
# ──────────────────────────────────────────────────────────────────────


_SPRINT_BODY_RISKS_TEMPLATE = """## Dependencies & risks

{risks}

## This sprint
"""


def _body_with_risks(*risks: str) -> str:
    return _SPRINT_BODY_RISKS_TEMPLATE.format(risks="\n".join(risks))


def test_escalated_risk_flags():
    """A risk bullet with severity=escalated surfaces an alert flag."""
    state = make_state("ggl-5168")
    body = _body_with_risks(
        "- [escalated · staffing · 2026-05-30] Brandon out next week"
    )
    flags = _detect_urgent(state, (), (), today=_TODAY, sprint_file_body=body)
    assert len(flags) == 1
    assert flags[0]["type"] == "escalated_risk"
    assert "Brandon out next week" in flags[0]["text"]
    assert flags[0]["severity"] == "alert"


def test_non_escalated_risk_no_flag():
    """A `watching`-severity risk does NOT flag."""
    state = make_state("ggl-5168")
    body = _body_with_risks(
        "- [watching · tooling · 2026-05-21] Brandon ramping on new tools"
    )
    flags = _detect_urgent(state, (), (), today=_TODAY, sprint_file_body=body)
    assert flags == []


# ──────────────────────────────────────────────────────────────────────
#  Template-placeholder filtering (decisions + risks)
#
#  The sprint scaffold seeds every new file with italicized
#  angle-bracketed placeholder bullets — ``- _<choice — `[by W##]`
#  prefix>_`` under Decisions due, ``- _<risk — `[severity · category ·
#  date]` prefix>_`` under Dependencies & risks. Before this fix every
#  unfilled scaffold flagged ``decision_due`` urgent (the literal text
#  ``by W##`` matched ``_is_decision_horizon_urgent``'s ISO-week
#  substring logic) — 26 false positives across 26 active projects.
# ──────────────────────────────────────────────────────────────────────


_DECISIONS_DUE_PLACEHOLDER = "- _<choice — `[by W##]` prefix>_"
_RISK_PLACEHOLDER = "- _<risk — `[severity · category · date]` prefix>_"


def test_decision_due_template_placeholder_not_flagged():
    """Unfilled `### Decisions due` scaffold placeholder MUST NOT flag urgent."""
    state = make_state("ggl-5168")
    body = _body_with_decisions(_DECISIONS_DUE_PLACEHOLDER)
    flags = _detect_urgent(state, (), (), today=_TODAY, sprint_file_body=body)
    assert flags == []


def test_decision_due_real_decision_still_flagged():
    """Filtering placeholders MUST NOT regress real-decision detection."""
    state = make_state("ggl-5168")
    body = _body_with_decisions(
        "- [this sprint] Confirm the workshop deck template"
    )
    flags = _detect_urgent(state, (), (), today=_TODAY, sprint_file_body=body)
    assert len(flags) == 1
    assert flags[0]["type"] == "decision_due"
    assert "workshop deck template" in flags[0]["text"]


def test_decision_due_mixed_placeholder_and_real_only_real_flagged():
    """A section with both shapes flags only the real bullet."""
    state = make_state("ggl-5168")
    body = _body_with_decisions(
        _DECISIONS_DUE_PLACEHOLDER,
        "- [this sprint] Confirm the workshop deck template",
    )
    flags = _detect_urgent(state, (), (), today=_TODAY, sprint_file_body=body)
    assert len(flags) == 1
    assert flags[0]["type"] == "decision_due"
    assert "workshop deck template" in flags[0]["text"]


def test_escalated_risk_template_placeholder_not_flagged():
    """Unfilled `## Dependencies & risks` placeholder MUST NOT flag urgent."""
    state = make_state("ggl-5168")
    body = _body_with_risks(_RISK_PLACEHOLDER)
    flags = _detect_urgent(state, (), (), today=_TODAY, sprint_file_body=body)
    assert flags == []


def test_escalated_risk_real_risk_still_flagged():
    """Filtering placeholders MUST NOT regress real escalated-risk detection."""
    state = make_state("ggl-5168")
    body = _body_with_risks(
        _RISK_PLACEHOLDER,
        "- [escalated · staffing · 2026-05-30] Brandon out next week",
    )
    flags = _detect_urgent(state, (), (), today=_TODAY, sprint_file_body=body)
    assert len(flags) == 1
    assert flags[0]["type"] == "escalated_risk"
    assert "Brandon out next week" in flags[0]["text"]


# ──────────────────────────────────────────────────────────────────────
#  Combined + summary aggregation
# ──────────────────────────────────────────────────────────────────────


def test_multiple_rules_combine_in_order():
    """A project triggering all 4 rules returns one flag of each, in rule order."""
    state = make_state("ggl-5168")
    m = _ms(deliverable="Workshop deck", date_str="2026-06-08", confidence="low")
    asks = (_ask(text="Share R3 mocks", by="2026-05-26"),)
    body = (
        _body_with_risks(
            "- [escalated · staffing · 2026-05-30] Brandon out next week"
        )
        + _body_with_decisions(
            "- [this sprint] Pick a deck template"
        )
    )
    flags = _detect_urgent(state, (m,), asks, today=_TODAY, sprint_file_body=body)
    # All four rule types present, in declared rule order:
    types = [f["type"] for f in flags]
    assert types == [
        "slip_risk",
        "decision_due",
        "past_due_ask",
        "escalated_risk",
    ]


def test_summary_counts_per_type(tmp_path):
    """The build_planning_result urgent_counts tallies flags per rule type."""
    config = make_config(tmp_path)
    state = make_state("ggl-5168", name="GGL 5168 Activation")
    # Lay down a sprint file with both a past-due ask AND an escalated risk
    # so two rules fire end-to-end (no ClickUp/Supabase plumbing needed).
    week_iso = "2026-W23"  # the sprint that contains 2026-06-02
    sprint_dir = tmp_path / "sprints" / week_iso
    sprint_dir.mkdir(parents=True)
    sprint_body = (
        "## Client communication\n\n"
        "### Open asks\n\n"
        "- [open · 2026-05-01 · drew · by 2026-05-26] Share R3 mocks "
        "<!-- cp:hash=abc12345 -->\n\n"
        "## Dependencies & risks\n\n"
        "- [escalated · staffing · 2026-05-30] Brandon out next week\n\n"
        "## This sprint\n"
    )
    (sprint_dir / "ggl-5168.md").write_text(sprint_body, encoding="utf-8")

    result = build_planning_result(
        config,
        (state,),
        today=_TODAY,
        supabase_client=None,
        # No list_id_lookup → "no clickup_list_id" path; milestones=()
    )
    # past_due_ask and escalated_risk fired; the other two are zero.
    assert result.urgent_counts == {
        "slip_risk": 0,
        "decision_due": 0,
        "past_due_ask": 1,
        "escalated_risk": 1,
    }


# ──────────────────────────────────────────────────────────────────────
#  Bonus integration: full doc render walks every section
# ──────────────────────────────────────────────────────────────────────


def test_render_planning_doc_full_walk(tmp_path):
    """Two active projects across two accounts → full markdown doc walks both."""
    config = make_config(tmp_path)
    projects = (
        make_state("ggl-5168", name="GGL 5168 Activation", company_name="Google"),
        make_state(
            "ibx-5153", name="IBX 5153 AI Campaign",
            company_code="IBX", company_name="Infoblox",
        ),
    )

    doc = render_planning_doc(
        config,
        projects,
        today=date(2026, 6, 7),
        tenant_hours_last_week={"Drew": 52, "Tony": 50},
        supabase_client=None,
    )
    # Top-of-doc strip + cross-cutting stub + per-account walks.
    assert "Sprint" in doc
    assert "Planning" in doc
    assert "## Tenant strip" in doc
    assert "Active:" in doc
    assert "Drew 52h" in doc
    assert "## Cross-cutting" in doc
    assert "## Google (1 projects)" in doc
    assert "## Infoblox (1 projects)" in doc
    assert "ggl-5168" in doc
    assert "ibx-5153" in doc


def test_render_quick_resume_when_present(tmp_path):
    """When cp.md has a real **Current work:** line, the Where block reflects it."""
    config = make_config(tmp_path)
    state = make_state("ggl-5168", name="GGL 5168 Activation", company_name="Google")
    # Lay down a real cp.md at the account-scoped path.
    from cp_engine.state import account_scope_for, dir_slug
    scope = account_scope_for(state)
    slug = dir_slug(state.code, state.name)
    cp_md = tmp_path / scope / slug / "cp.md"
    cp_md.parent.mkdir(parents=True, exist_ok=True)
    cp_md.write_text(
        "## Quick Resume\n\n"
        "**Last session:** _<date>_\n"
        "**Current work:** Pop-up R3 with Rena since 5/22.\n"
        "**Next up:** Wait for Rena feedback.\n"
        "**Blockers:** Awaiting Rena.\n\n"
        "## Next\n"
    )
    block = prep_planning.build_project_block(
        state,
        config=config,
        supabase_client=None,
        today=date(2026, 6, 7),
        week_iso="2026-W24",
        clickup_client=None,
        clickup_token=None,
        list_id_override=None,
    )
    assert block.quick_resume_line is not None
    assert "Pop-up R3 with Rena" in block.quick_resume_line


def test_summary_counts_errors_for_failed_fetch(tmp_path):
    """When ClickUp returns 4xx, the project counts as errored in milestone_counts."""
    config = make_config(tmp_path)
    state = make_state("ggl-5168", name="GGL 5168 Activation")
    list_id = "BAD"
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    client = FakeClient(
        {
            (url, "milestone"): FakeResp(500, text="server error"),
            (url, "client-ask"): FakeResp(500, text="server error"),
        }
    )
    result = build_planning_result(
        config,
        (state,),
        today=date(2026, 6, 7),
        tenant_hours_last_week={},
        supabase_client=None,
        clickup_token="tok_test",
        clickup_client=client,
        list_id_lookup={"ggl-5168": list_id},
    )
    assert result.milestone_counts["errored"] == 1
    assert result.milestone_counts["fetched"] == 0
    assert any("ggl-5168" in err for err in result.errors)


# ──────────────────────────────────────────────────────────────────────
#  ClickUp pagination tests
# ──────────────────────────────────────────────────────────────────────
#
# ``_fetch_clickup_milestones`` paginates over /list/{id}/task because
# ClickUp caps each page at 100 tasks. The fetcher stops as soon as a
# page returns fewer than 100 tasks.


class _PaginatedFakeClient:
    """FakeClient variant that routes by (url, tag, page) so a single
    list-id can return distinct pages without colliding."""

    def __init__(self, pages_by_tag: dict[str, list[list[dict]]]):
        # pages_by_tag[tag][page_num] = list of task dicts for that page
        self._pages_by_tag = pages_by_tag
        self.calls: list[tuple[str, str, str]] = []  # (url, tag, page)

    def get(self, url: str, *, headers=None, params=None):
        tag = ""
        page = "0"
        for k, v in params or []:
            if k == "tags[]":
                tag = v
            elif k == "page":
                page = v
        self.calls.append((url, tag, page))
        pages = self._pages_by_tag.get(tag, [])
        page_idx = int(page)
        if page_idx < len(pages):
            return FakeResp(200, {"tasks": pages[page_idx]})
        return FakeResp(200, {"tasks": []})


def test_fetch_milestones_single_page_no_extra_request():
    """Fewer than 100 tasks on page 0 → fetch stops after one call."""
    single_page = [{"id": f"T{i}", "name": f"M{i}", "tags": [{"name": "milestone"}]}
                   for i in range(5)]
    client = _PaginatedFakeClient({"milestone": [single_page]})

    out = _fetch_clickup_milestones(
        "L1", tag="milestone", token="tok", client=client
    )

    assert len(out) == 5
    # Exactly one page fetched — no page=1 follow-up.
    assert client.calls == [
        ("https://api.clickup.com/api/v2/list/L1/task", "milestone", "0"),
    ]


def test_fetch_milestones_paginates_when_first_page_full():
    """100 tasks on page 0 + 50 on page 1 → 150 total, both pages fetched."""
    page0 = [{"id": f"T{i}", "name": f"M{i}", "tags": [{"name": "milestone"}]}
             for i in range(100)]
    page1 = [{"id": f"T{i + 100}", "name": f"M{i + 100}",
              "tags": [{"name": "milestone"}]} for i in range(50)]
    client = _PaginatedFakeClient({"milestone": [page0, page1]})

    out = _fetch_clickup_milestones(
        "L1", tag="milestone", token="tok", client=client
    )

    assert len(out) == 150
    # Page 0 + page 1 fetched; page 2 NOT fetched (page1 was < 100).
    page_args = [c[2] for c in client.calls]
    assert page_args == ["0", "1"]


def test_fetch_milestones_stops_at_exactly_100():
    """Page that is exactly empty stops the loop."""
    page0 = [{"id": f"T{i}", "name": f"M{i}", "tags": [{"name": "milestone"}]}
             for i in range(100)]
    # page1 empty
    client = _PaginatedFakeClient({"milestone": [page0, []]})

    out = _fetch_clickup_milestones(
        "L1", tag="milestone", token="tok", client=client
    )

    assert len(out) == 100
    # Page 0 fetched, page 1 fetched (returned 0 tasks), stop.
    page_args = [c[2] for c in client.calls]
    assert page_args == ["0", "1"]
