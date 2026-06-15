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
    _CLICKUP_MAX_PAGES,
    _CLICKUP_PAGE_SIZE,
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


def test_normalize_falls_back_to_due_date_in_name_when_structured_field_missing():
    """When ClickUp's structured due_date is null but the name contains
    `(due YYYY-MM-DD`, extract the date so Forward Calendar still renders.

    Regression: 2026-06-02 IBX milestone push from fathom-meeting-sync did
    not set the structured field, so the date lived only in the name. Engine
    skipped the Forward Calendar bullet (no anchor) — the task surfaced only
    in the Open Commitments fallback.
    """
    task = {
        "id": "T4",
        "name": (
            "Campaign strategy workshop with Infoblox "
            "(due 2026-06-17, owner: Marcello)"
        ),
        "due_date": None,
        "assignees": [],
        "tags": [{"name": "milestone"}],
        "status": {"status": "open"},
        "custom_fields": [],
    }
    norm = _normalize_clickup_task(task)
    assert norm["date"] == "2026-06-17"


def test_normalize_prefers_structured_due_date_over_name():
    """When both ClickUp's structured due_date and a `(due ...)` in the name
    exist, the structured field wins. Hand-edits to either should not
    silently override a server-side value."""
    task = {
        "id": "T5",
        "name": "Pop-up final (due 2026-06-06, owner: brandon)",
        # 2026-07-01 12:00 UTC in ms-since-epoch
        "due_date": "1782907200000",
        "assignees": [],
        "tags": [{"name": "milestone"}],
        "status": {"status": "open"},
        "custom_fields": [],
    }
    norm = _normalize_clickup_task(task)
    assert norm["date"] == "2026-07-01"


def test_normalize_no_due_anywhere_yields_empty_date():
    """No structured field, no `(due ...)` in name → empty date string."""
    task = {
        "id": "T6",
        "name": "Some milestone without dates",
        "due_date": None,
        "assignees": [],
        "tags": [{"name": "milestone"}],
        "status": {"status": "open"},
        "custom_fields": [],
    }
    norm = _normalize_clickup_task(task)
    assert norm["date"] == ""


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
#  v0.15.1 Fix 4 — pipe-escape Open Commitments table cells
# ──────────────────────────────────────────────────────────────────────


def test_md_table_cell_escapes_pipes_and_collapses_newlines():
    """Helper escapes the two characters that corrupt markdown table rows."""
    assert prep_planning._md_table_cell("Spec | Implementation | Review") == (
        "Spec \\| Implementation \\| Review"
    )
    assert prep_planning._md_table_cell("line1\nline2") == "line1 line2"
    assert prep_planning._md_table_cell("line1\r\nline2") == "line1  line2"
    assert prep_planning._md_table_cell(None) == ""
    assert prep_planning._md_table_cell("  padded  ") == "padded"


def test_commitments_table_escapes_pipes_in_milestone_deliverable():
    """A milestone deliverable with literal `|` must not break the row.

    Before Fix 4: f"| {what} |" → "| Spec | Implementation | Review |"
    rendered as a 5-column row in a 4-column table, corrupting the layout.

    After Fix 4: pipes escape to `\\|`, the row stays 4 columns wide.
    """
    state = make_state("ggl-5168", name="GGL 5168 Activation")
    ms: Milestone = Milestone(
        id="M1", task_type="milestone",
        deliverable="Spec | Implementation | Review",
        date="2026-06-03", owner="brandon", confidence="high",
        depends_on=[], status="open", linked_to=[],
    )
    block = ProjectPlanningBlock(
        project=state,
        quick_resume_line=None,
        milestones=(ms,),
        client_asks=(),
        sprint_open_asks=(),
        urgent=(),
        fetch_error=None,
    )
    out = "\n".join(prep_planning._render_commitments_table(block))
    # Each row has 5 pipe characters (4 separators + leading + trailing).
    # Literal `|` inside `what` would push that to 7 — assert it stays at 5.
    row_lines = [
        ln for ln in out.splitlines()
        if ln.startswith("| ") and "Spec" in ln
    ]
    assert len(row_lines) == 1
    # Count unescaped pipes (split would treat \| as a separator otherwise,
    # so escape them first to count just the cell separators).
    raw_pipes = row_lines[0].count("|") - row_lines[0].count("\\|")
    assert raw_pipes == 5, (
        f"row corrupted: got {raw_pipes} unescaped pipes (expected 5): {row_lines[0]!r}"
    )
    assert "Spec \\| Implementation \\| Review" in out


def test_commitments_table_escapes_newlines_in_client_ask():
    """Newlines in a ClickUp task name collapse to a single space row."""
    state = make_state("ggl-5168", name="GGL 5168 Activation")
    ask: Milestone = Milestone(
        id="A1", task_type="client_ask",
        deliverable="Round 3 feedback\nincluding pop-up assets",
        date="2026-06-05", owner="rena", confidence="medium",
        depends_on=[], status="open", linked_to=[],
    )
    block = ProjectPlanningBlock(
        project=state,
        quick_resume_line=None,
        milestones=(),
        client_asks=(ask,),
        sprint_open_asks=(),
        urgent=(),
        fetch_error=None,
    )
    out = "\n".join(prep_planning._render_commitments_table(block))
    # No raw newline inside the rendered row — collapsed to a space.
    assert "Round 3 feedback including pop-up assets" in out
    # Sanity: the row stays single-line.
    matching_lines = [
        ln for ln in out.splitlines() if "Round 3 feedback" in ln
    ]
    assert len(matching_lines) == 1


# ──────────────────────────────────────────────────────────────────────
#  Test 7: empty milestone list renders the "no milestones tracked yet" line
# ──────────────────────────────────────────────────────────────────────


def test_render_planning_doc_handles_empty_clickup_list(tmp_path):
    """Project HAS a list_id but ClickUp returns 0 tasks → ``no_milestones_tagged``.

    Distinct from ``no_clickup_list`` (the project simply hasn't been
    wired to a ClickUp list at all) — see
    ``test_render_planning_doc_distinguishes_list_unset_vs_list_empty``
    for both branches side by side.
    """
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
    # The list-id resolved + the fetch returned 0 tasks → distinct sentinel
    # from the no-list case so the renderer can point users to the right fix.
    assert block.fetch_error == "no_milestones_tagged"
    assert block.milestones == ()
    rendered = "\n".join(prep_planning._render_forward_calendar(block))
    assert "no milestones tagged in ClickUp yet" in rendered
    assert "Task 29" in rendered


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
    assert block.fetch_error == "no_clickup_list"
    rendered = "\n".join(prep_planning._render_forward_calendar(block))
    assert "ClickUp list not set in MC-2" in rendered


# ──────────────────────────────────────────────────────────────────────
#  Test 8b (#36): list-unset vs list-empty render distinct messages
# ──────────────────────────────────────────────────────────────────────


def test_render_planning_doc_distinguishes_list_unset_vs_list_empty(tmp_path):
    """The two empty-states render distinct messages.

    Before #36 both cases emitted "_(ClickUp list not set — milestones not
    tracked)_", which confused users into thinking they needed to set a
    list-id that was already set. After #36:

      - No clickup_list_id at all → "ClickUp list not set in MC-2"
      - List set but ClickUp returned 0 tasks → "no milestones tagged
        in ClickUp yet" + a pointer to Task 29 back-population.
    """
    config = make_config(tmp_path)
    state_unset = make_state("ggl-9999", name="No List Yet")
    state_empty = make_state("ggl-5168", name="GGL 5168 Activation")

    # Branch A — no list_id.
    block_unset = prep_planning.build_project_block(
        state_unset,
        config=config,
        supabase_client=None,
        today=date(2026, 6, 7),
        week_iso="2026-W24",
        clickup_client=None,
        clickup_token=None,
        list_id_override=None,
    )
    assert block_unset.fetch_error == "no_clickup_list"
    rendered_unset = "\n".join(prep_planning._render_forward_calendar(block_unset))
    assert "ClickUp list not set in MC-2" in rendered_unset
    assert "Task 29" not in rendered_unset

    # Branch B — list_id present, fetch returns 0 tasks.
    list_id = "EMPTY"
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    client = FakeClient(
        {
            (url, "milestone"): FakeResp(200, {"tasks": []}),
            (url, "client-ask"): FakeResp(200, {"tasks": []}),
        }
    )
    block_empty = prep_planning.build_project_block(
        state_empty,
        config=config,
        supabase_client=None,
        today=date(2026, 6, 7),
        week_iso="2026-W24",
        clickup_client=client,
        clickup_token="tok_test",
        list_id_override=list_id,
    )
    assert block_empty.fetch_error == "no_milestones_tagged"
    rendered_empty = "\n".join(prep_planning._render_forward_calendar(block_empty))
    assert "no milestones tagged in ClickUp yet" in rendered_empty
    assert "Task 29" in rendered_empty
    # And critically — the two messages do NOT collide.
    assert rendered_unset != rendered_empty


# ──────────────────────────────────────────────────────────────────────
#  Fix 1 (v0.15.2): bridging-period dedupe of sprint-file asks
# ──────────────────────────────────────────────────────────────────────


def test_build_project_block_filters_sprint_asks_already_in_clickup(tmp_path):
    """A sprint-file open ask whose hash exists in the ClickUp task-id map
    drops out of ``block.sprint_open_asks``. Without the dedupe, the Open
    Commitments table renders the same ask twice (once as a ClickUp
    client-ask, once as a sprint-file fallback)."""
    config = make_config(tmp_path)
    state = make_state("ggl-5168", name="GGL 5168 Activation")

    # 3 open asks: hashes aaaa1111 + bbbb2222 already in ClickUp;
    # cccc3333 only in sprint file.
    sprint_path = tmp_path / "sprints" / "2026-W24" / "ggl-5168.md"
    sprint_path.parent.mkdir(parents=True, exist_ok=True)
    sprint_path.write_text(
        "# 2026-W24 · ggl-5168\n\n"
        "## Client communication\n\n"
        "### Open asks\n\n"
        "- [open · 2026-06-01 · rena · by 2026-06-15] Send AI samples "
        "<!-- cp:hash=aaaa1111 -->\n"
        "- [open · 2026-06-02 · janet · by 2026-06-20] Pick a domain "
        "<!-- cp:hash=bbbb2222 -->\n"
        "- [open · 2026-06-03 · ruth] Confirm budget "
        "<!-- cp:hash=cccc3333 -->\n"
    )

    clickup_task_ids = {"aaaa1111": "TASK_A", "bbbb2222": "TASK_B"}

    block = prep_planning.build_project_block(
        state,
        config=config,
        supabase_client=None,
        today=date(2026, 6, 10),
        week_iso="2026-W24",
        clickup_client=None,
        clickup_token=None,
        list_id_override=None,
        clickup_task_ids=clickup_task_ids,
    )

    # Only the un-promoted ask survives.
    assert len(block.sprint_open_asks) == 1
    assert block.sprint_open_asks[0]["hash"] == "cccc3333"
    assert block.sprint_open_asks[0]["text"] == "Confirm budget"


def test_build_project_block_no_dedupe_when_clickup_task_ids_is_none(tmp_path):
    """Without ``clickup_task_ids`` (older callers, tests), all sprint
    open asks pass through — no surprise filtering."""
    config = make_config(tmp_path)
    state = make_state("ggl-5168", name="GGL 5168 Activation")

    sprint_path = tmp_path / "sprints" / "2026-W24" / "ggl-5168.md"
    sprint_path.parent.mkdir(parents=True, exist_ok=True)
    sprint_path.write_text(
        "# 2026-W24 · ggl-5168\n\n"
        "### Open asks\n\n"
        "- [open · 2026-06-01 · rena · by 2026-06-15] Send AI samples "
        "<!-- cp:hash=aaaa1111 -->\n"
        "- [open · 2026-06-02 · janet · by 2026-06-20] Pick a domain "
        "<!-- cp:hash=bbbb2222 -->\n"
    )

    block = prep_planning.build_project_block(
        state,
        config=config,
        supabase_client=None,
        today=date(2026, 6, 10),
        week_iso="2026-W24",
        clickup_client=None,
        clickup_token=None,
        list_id_override=None,
        clickup_task_ids=None,
    )

    assert len(block.sprint_open_asks) == 2


# ──────────────────────────────────────────────────────────────────────
#  Test 9: --summary mode emits valid JSON with the documented keys
# ──────────────────────────────────────────────────────────────────────


def test_summary_mode_emits_json(tmp_path):
    """render_planning_summary returns a JSON string with the contracted keys."""
    config = make_config(tmp_path)
    state = make_state("ggl-5168", name="GGL 5168 Activation")
    # No list_id_lookup, no Supabase → block degrades to "no_clickup_list"
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
        # No list_id_lookup → "no_clickup_list" path; milestones=()
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


class _AlwaysFullPageFakeClient:
    """FakeClient that returns ``_CLICKUP_PAGE_SIZE`` tasks for every page.

    Simulates a misconfigured tag matching enough tasks to blow past the
    safety cap, so the pagination loop should raise rather than silently
    truncate.
    """

    def __init__(self):
        self.calls: list[str] = []  # page numbers requested

    def get(self, url: str, *, headers=None, params=None):
        page = "0"
        for k, v in params or []:
            if k == "page":
                page = v
        self.calls.append(page)
        full_page = [
            {"id": f"T{page}-{i}", "name": f"M{i}",
             "tags": [{"name": "milestone"}]}
            for i in range(_CLICKUP_PAGE_SIZE)
        ]
        return FakeResp(200, {"tasks": full_page})


def test_fetch_milestones_safety_cap_raises():
    """When ClickUp keeps returning full pages past the cap, raise rather
    than silently truncating."""
    client = _AlwaysFullPageFakeClient()

    with pytest.raises(RuntimeError, match="pagination exceeded"):
        _fetch_clickup_milestones(
            "L1", tag="milestone", token="tok", client=client
        )

    # Cap is _CLICKUP_MAX_PAGES — we fetched pages 0..MAX-1 before the
    # next iteration tripped the guard.
    assert len(client.calls) == _CLICKUP_MAX_PAGES
    assert client.calls[0] == "0"
    assert client.calls[-1] == str(_CLICKUP_MAX_PAGES - 1)


# ──────────────────────────────────────────────────────────────────────
#  ClickUp auth-failure surfacing (#37)
#
#  Before #37 a 401/403 from ClickUp landed as a generic per-project
#  "ClickUp returned 401 for list X: ..." fetch_error and was lost in
#  the noise. Now:
#    - ``_fetch_clickup_milestones`` raises a distinct shape for 401/403
#      ("ClickUp auth failed (HTTP <code>): check CLICKUP_API_TOKEN").
#    - ``build_planning_result`` dedupes those failures into one
#      tenant-wide entry in ``result.errors`` so ``--summary`` shows
#      one clear "check your token" line rather than N copies.
# ──────────────────────────────────────────────────────────────────────


def test_fetch_milestones_401_raises_auth_failed():
    """A 401 from ClickUp surfaces as a distinct auth-failure RuntimeError."""
    list_id = "L1"
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    client = FakeClient({(url, "milestone"): FakeResp(401, text="invalid token")})

    with pytest.raises(RuntimeError, match="ClickUp auth failed.*HTTP 401"):
        _fetch_clickup_milestones(
            list_id, tag="milestone", token="bad_token", client=client
        )


def test_fetch_milestones_403_raises_auth_failed():
    """403 is treated the same as 401 — both mean "token is no good"."""
    list_id = "L1"
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    client = FakeClient({(url, "milestone"): FakeResp(403, text="forbidden")})

    with pytest.raises(RuntimeError, match="ClickUp auth failed.*HTTP 403"):
        _fetch_clickup_milestones(
            list_id, tag="milestone", token="bad_token", client=client
        )


def test_fetch_milestones_missing_token_raises_with_env_phrasing():
    """Missing token raises with the load-bearing 'in environment' phrasing
    so the per-project loop can dedupe it as an auth failure."""
    with pytest.raises(RuntimeError, match="CLICKUP_API_TOKEN not set in environment"):
        _fetch_clickup_milestones("L1", tag="milestone", token="", client=None)


def test_summary_auth_failure_surfaces_as_tenant_wide_error(tmp_path):
    """A 401 on the first project's fetch surfaces ONE tenant-wide auth-error
    entry in ``result.errors`` so ``--summary`` flags it clearly."""
    config = make_config(tmp_path)
    state = make_state("ggl-5168", name="GGL 5168 Activation")
    list_id = "L1"
    url = f"https://api.clickup.com/api/v2/list/{list_id}/task"
    client = FakeClient(
        {
            (url, "milestone"): FakeResp(401, text="invalid token"),
            (url, "client-ask"): FakeResp(401, text="invalid token"),
        }
    )

    result = build_planning_result(
        config,
        (state,),
        today=date(2026, 6, 7),
        tenant_hours_last_week={},
        supabase_client=None,
        clickup_token="bad_token",
        clickup_client=client,
        list_id_lookup={"ggl-5168": list_id},
    )
    assert any("ClickUp auth failed" in err for err in result.errors)
    assert any("CLICKUP_API_TOKEN" in err for err in result.errors)
    # The single project also surfaces as errored in milestone_counts.
    assert result.milestone_counts["errored"] == 1


def test_summary_auth_failure_dedupes_across_projects(tmp_path):
    """When auth fails on every project, ``result.errors`` carries exactly
    ONE auth-error entry (dedup), not N copies."""
    config = make_config(tmp_path)
    projects = (
        make_state("ggl-5168", name="GGL 5168", company_name="Google"),
        make_state(
            "ibx-5153", name="IBX 5153",
            company_code="IBX", company_name="Infoblox",
        ),
        make_state(
            "snt-5189", name="SNT 5189",
            company_code="SNT", company_name="Sentinel One",
        ),
    )

    class _AlwaysAuthFailClient:
        """Routes every request to a 401, regardless of list_id."""

        def __init__(self):
            self.calls: list[str] = []

        def get(self, url, *, headers=None, params=None):
            self.calls.append(url)
            return FakeResp(401, text="invalid token")

    client = _AlwaysAuthFailClient()
    result = build_planning_result(
        config,
        projects,
        today=date(2026, 6, 7),
        tenant_hours_last_week={},
        supabase_client=None,
        clickup_token="bad_token",
        clickup_client=client,
        list_id_lookup={
            "ggl-5168": "L1",
            "ibx-5153": "L2",
            "snt-5189": "L3",
        },
    )
    auth_errors = [e for e in result.errors if "ClickUp auth failed" in e]
    assert len(auth_errors) == 1
    # Every project still counts as errored individually in milestone_counts.
    assert result.milestone_counts["errored"] == 3


# ──────────────────────────────────────────────────────────────────────
#  Project header dedup when code == name (#38)
#
#  Standalone repos (and some initiatives) carry name == code. The old
#  ``f"### {p.code} {p.name} — {owner}"`` template rendered as e.g.
#  "### cp cp — Drew and Tony", duplicating the slug. Cosmetic only,
#  but distracting in a doc partners read every Monday.
# ──────────────────────────────────────────────────────────────────────


def test_project_header_dedupes_when_code_equals_name():
    """code == name → single slug in the header (no "cp cp" duplication)."""
    state = make_state(
        "cp",
        name="cp",
        company_kind="self-fpsf",
        company_code=None,
        company_name=None,
        source="repo",
        owner="Drew and Tony",
    )
    block = ProjectPlanningBlock(
        project=state,
        quick_resume_line=None,
        milestones=(),
        client_asks=(),
        sprint_open_asks=(),
        urgent=(),
        fetch_error=None,
    )
    rendered = "\n".join(prep_planning._render_project_block(block))
    assert "### cp — Drew and Tony" in rendered
    # The duplicated form must NOT appear.
    assert "### cp cp" not in rendered


def test_project_header_keeps_both_when_code_differs_from_name():
    """code != name → both still surface, em-dash and spacing unchanged."""
    state = make_state(
        "ggl-5168",
        name="GGL 5168 Activation",
        company_name="Google",
        owner="drew",
    )
    block = ProjectPlanningBlock(
        project=state,
        quick_resume_line=None,
        milestones=(),
        client_asks=(),
        sprint_open_asks=(),
        urgent=(),
        fetch_error=None,
    )
    rendered = "\n".join(prep_planning._render_project_block(block))
    assert "### ggl-5168 GGL 5168 Activation — drew" in rendered


def test_summary_non_auth_error_does_not_surface_auth_error(tmp_path):
    """A 500 (or any non-401/403) MUST NOT bubble an auth-error entry —
    those errors are real, project-specific and should appear per-project."""
    config = make_config(tmp_path)
    state = make_state("ggl-5168", name="GGL 5168 Activation")
    list_id = "L1"
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
    assert not any("ClickUp auth failed" in err for err in result.errors)
    # The 500 still lands per-project.
    assert any("ggl-5168" in err for err in result.errors)


# ──────────────────────────────────────────────────────────────────────
#  ClickUp token resolution — env (both names) + mc-2 .env fallback
# ──────────────────────────────────────────────────────────────────────

def _config_with_mc2(tenant_root: Path, mc2_clone: Path) -> TenantConfig:
    return TenantConfig(
        name="firstpersonsf",
        display="First Person Internal",
        engine_version_constraint="~= 0.1",
        sync=SyncConfig(
            backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="ref"
        ),
        projects=(),
        root=tenant_root,
        local_repos={"mc-2": mc2_clone},
    )


def test_clickup_token_env_canonical(monkeypatch, tmp_path):
    monkeypatch.setenv("CLICKUP_API_TOKEN", "pk_canonical")
    monkeypatch.delenv("CLICKUP_API_KEY", raising=False)
    cfg = _config_with_mc2(tmp_path, tmp_path / "mc-2")
    assert prep_planning._resolve_clickup_token(cfg) == "pk_canonical"


def test_clickup_token_env_key_alias(monkeypatch, tmp_path):
    # MC-2's name, exported in the spine — should still resolve.
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)
    monkeypatch.setenv("CLICKUP_API_KEY", "pk_alias")
    cfg = _config_with_mc2(tmp_path, tmp_path / "mc-2")
    assert prep_planning._resolve_clickup_token(cfg) == "pk_alias"


def test_clickup_token_env_token_wins_over_key(monkeypatch, tmp_path):
    monkeypatch.setenv("CLICKUP_API_TOKEN", "pk_token")
    monkeypatch.setenv("CLICKUP_API_KEY", "pk_key")
    cfg = _config_with_mc2(tmp_path, tmp_path / "mc-2")
    assert prep_planning._resolve_clickup_token(cfg) == "pk_token"


def test_clickup_token_falls_back_to_mc2_env_key(monkeypatch, tmp_path, capsys):
    # THE bug this fixes: token only in mc-2/backend/.env as CLICKUP_API_KEY.
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)
    monkeypatch.delenv("CLICKUP_API_KEY", raising=False)
    mc2 = tmp_path / "mc-2"
    backend = mc2 / "backend"
    backend.mkdir(parents=True)
    (backend / ".env").write_text(
        'SUPABASE_URL="https://x.supabase.co"\n'
        'CLICKUP_API_KEY="pk_from_dotenv"\n'
    )
    cfg = _config_with_mc2(tmp_path, mc2)
    assert prep_planning._resolve_clickup_token(cfg) == "pk_from_dotenv"
    # one-line stderr note keeps the implicit dependency visible
    assert "CLICKUP_API_KEY" in capsys.readouterr().err


def test_clickup_token_falls_back_to_mc2_env_token_name(monkeypatch, tmp_path):
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)
    monkeypatch.delenv("CLICKUP_API_KEY", raising=False)
    mc2 = tmp_path / "mc-2"
    backend = mc2 / "backend"
    backend.mkdir(parents=True)
    (backend / ".env").write_text('CLICKUP_API_TOKEN="pk_dotenv_canonical"\n')
    cfg = _config_with_mc2(tmp_path, mc2)
    assert prep_planning._resolve_clickup_token(cfg) == "pk_dotenv_canonical"


def test_clickup_token_none_when_nowhere(monkeypatch, tmp_path):
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)
    monkeypatch.delenv("CLICKUP_API_KEY", raising=False)
    # mc-2 clone configured but no .env file present
    cfg = _config_with_mc2(tmp_path, tmp_path / "mc-2")
    assert prep_planning._resolve_clickup_token(cfg) is None


def test_clickup_token_none_when_no_mc2_clone(monkeypatch, tmp_path):
    monkeypatch.delenv("CLICKUP_API_TOKEN", raising=False)
    monkeypatch.delenv("CLICKUP_API_KEY", raising=False)
    cfg = TenantConfig(
        name="firstpersonsf",
        display="First Person Internal",
        engine_version_constraint="~= 0.1",
        sync=SyncConfig(
            backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="ref"
        ),
        projects=(),
        root=tmp_path,
    )
    assert prep_planning._resolve_clickup_token(cfg) is None


# ──────────────────────────────────────────────────────────────────────
#  Project Spine slice 3 — Phase B: opt-in --sweep wiring
# ──────────────────────────────────────────────────────────────────────


def _write_spine_element(
    tenant_root: Path,
    project: ProjectState,
    *,
    layer: str = "Deliverables",
    name: str = "thing",
    title: str = "The Thing",
    status: str = "active",
    stage: str | None = "first",
    last_touched: str = "2026-06-05",
    body: str = "Some element body content.",
) -> Path:
    """Lay down one spine element under the project's working dir on disk.

    Mirrors the layout build_project_block reads from
    (``<root>/<account_scope>/<dir_slug>/spine/<Layer>/<name>.md``), computed
    via the same state helpers so the test stays in lockstep with the code.
    """
    from cp_engine.state import account_scope_for, dir_slug

    scope = account_scope_for(project)
    slug = dir_slug(project.code, project.name)
    layer_dir = tenant_root / scope / slug / "spine" / layer
    layer_dir.mkdir(parents=True, exist_ok=True)
    el_path = layer_dir / f"{name}.md"
    fm_lines = [
        "---",
        f"id: {project.code}/{layer.lower()}/{name}",
        f"project: {project.code}",
        f"layer: {layer}",
        f"title: {title}",
        f"status: {status}",
        f"last_touched: {last_touched}",
    ]
    if stage is not None:
        fm_lines.append(f"stage: {stage}")
    fm_lines.append("---")
    el_path.write_text("\n".join(fm_lines) + f"\n\n{body}\n", encoding="utf-8")
    return el_path


class _CountingLLM:
    """A fake sweep LLM that counts calls and returns canned synthesis."""

    def __init__(self, text: str = "CANNED SWEEP SYNTHESIS", raises: bool = False):
        self.text = text
        self.raises = raises
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        if self.raises:
            raise RuntimeError("simulated LLM failure")
        return self.text


def test_sweep_on_attaches_synthesis_to_block(tmp_path):
    """With sweep_llm provided + a backfilled spine, the project's block
    carries the synthesis and it renders in the doc."""
    config = make_config(tmp_path)
    state = make_state("ggl-5168", name="GGL 5168 Activation", company_name="Google")
    _write_spine_element(tmp_path, state)
    llm = _CountingLLM("WHOLE PROJECT READOUT")

    result = build_planning_result(
        config,
        (state,),
        today=_TODAY,
        supabase_client=None,
        sweep_llm=llm,
    )

    assert llm.calls == 1
    block = next(
        b for blocks in result.blocks_by_account.values() for b in blocks
    )
    assert block.sweep_synthesis == "WHOLE PROJECT READOUT"

    doc = render_planning_doc(
        config,
        (state,),
        today=_TODAY,
        supabase_client=None,
        sweep_llm=llm,
    )
    assert "**Sweep:**" in doc
    assert "WHOLE PROJECT READOUT" in doc


def test_sweep_off_default_makes_no_llm_call(tmp_path):
    """Default path (no sweep_llm): the fake LLM is never called and no
    synthesis is attached — fast path completely unchanged."""
    config = make_config(tmp_path)
    state = make_state("ggl-5168", name="GGL 5168 Activation", company_name="Google")
    _write_spine_element(tmp_path, state)
    llm = _CountingLLM()

    result = build_planning_result(
        config,
        (state,),
        today=_TODAY,
        supabase_client=None,
        # sweep_llm omitted → default None
    )

    assert llm.calls == 0
    block = next(
        b for blocks in result.blocks_by_account.values() for b in blocks
    )
    assert block.sweep_synthesis is None

    doc = render_planning_doc(config, (state,), today=_TODAY, supabase_client=None)
    assert "**Sweep:**" not in doc


def test_sweep_no_spine_attaches_nothing(tmp_path):
    """A project with no backfilled spine skips the LLM and attaches no
    synthesis (empty elements → run_sweep sentinel, dropped)."""
    config = make_config(tmp_path)
    state = make_state("ggl-5168", name="GGL 5168 Activation", company_name="Google")
    # No _write_spine_element → no spine/ dir on disk.
    llm = _CountingLLM()

    result = build_planning_result(
        config,
        (state,),
        today=_TODAY,
        supabase_client=None,
        sweep_llm=llm,
    )

    assert llm.calls == 0  # empty spine short-circuits before the LLM
    block = next(
        b for blocks in result.blocks_by_account.values() for b in blocks
    )
    assert block.sweep_synthesis is None


def test_sweep_best_effort_per_project(tmp_path):
    """A project whose sweep raises gets no synthesis but the doc still
    builds — other projects are unaffected and no exception propagates."""
    config = make_config(tmp_path)
    raising = make_state(
        "ggl-5168", name="GGL 5168 Activation", company_name="Google"
    )
    healthy = make_state(
        "ibx-5153", name="IBX 5153 AI Campaign",
        company_code="IBX", company_name="Infoblox",
    )
    _write_spine_element(tmp_path, raising)
    _write_spine_element(tmp_path, healthy)

    # An LLM that raises on the first project but the test asserts the doc
    # still builds. Since both call the same llm, use one that always raises
    # and assert neither synthesis lands but the doc renders both projects.
    llm = _CountingLLM(raises=True)

    doc = render_planning_doc(
        config,
        (raising, healthy),
        today=_TODAY,
        supabase_client=None,
        sweep_llm=llm,
    )

    # Both projects rendered, no synthesis attached, no exception escaped.
    assert "ggl-5168" in doc
    assert "ibx-5153" in doc
    assert "**Sweep:**" not in doc
    assert llm.calls == 2  # attempted both, both caught


def test_sweep_resolves_drifted_dir_via_find_spine_dir(tmp_path):
    """Fix 1: the sweep resolves the working dir via find_spine_dir (prefix-
    tolerant), not a hand-built exact-slug path. A name-drifted dir — where
    the on-disk dir name differs from the current dir_slug because the project
    was renamed in MC-2 — still gets swept. An exact-slug match would miss it.
    """
    from cp_engine.state import account_scope_for, dir_slug

    config = make_config(tmp_path)
    # State whose current slug is ggl-5168-new-name…
    state = make_state(
        "ggl-5168", name="GGL 5168 New Name", company_name="Google"
    )
    scope = account_scope_for(state)
    current_slug = dir_slug(state.code, state.name)

    # …but the spine lives under a DRIFTED dir name (old slug, same code
    # prefix). find_spine_dir prefix-matches on "ggl-5168-"; the old exact
    # path (config.root / scope / current_slug) would not exist.
    drifted_slug = f"{state.code}-stale-old-name"
    assert drifted_slug != current_slug
    layer_dir = tmp_path / scope / drifted_slug / "spine" / "Deliverables"
    layer_dir.mkdir(parents=True, exist_ok=True)
    (layer_dir / "thing.md").write_text(
        "---\n"
        f"id: {state.code}/deliverables/thing\n"
        f"project: {state.code}\n"
        "layer: Deliverables\n"
        "title: The Thing\n"
        "status: active\n"
        "last_touched: 2026-06-05\n"
        "stage: first\n"
        "---\n\nSome element body content.\n",
        encoding="utf-8",
    )
    # The current-slug dir must NOT exist, proving resolution isn't the
    # hand-built exact path.
    assert not (tmp_path / scope / current_slug).exists()

    llm = _CountingLLM("DRIFTED READOUT")
    result = build_planning_result(
        config,
        (state,),
        today=_TODAY,
        supabase_client=None,
        sweep_llm=llm,
    )

    assert llm.calls == 1  # spine found via find_spine_dir → swept
    block = next(
        b for blocks in result.blocks_by_account.values() for b in blocks
    )
    assert block.sweep_synthesis == "DRIFTED READOUT"


def test_sweep_best_effort_one_fails_one_succeeds(tmp_path):
    """Per-project isolation: a failing sweep on one project doesn't block a
    successful sweep on another."""
    config = make_config(tmp_path)
    bad = make_state(
        "ggl-5168", name="GGL 5168 Activation", company_name="Google"
    )
    good = make_state(
        "ibx-5153", name="IBX 5153 AI Campaign",
        company_code="IBX", company_name="Infoblox",
    )
    _write_spine_element(tmp_path, bad)
    _write_spine_element(tmp_path, good)

    # Fail only when the prompt is for ggl-5168 (the sweep prompt header
    # includes the project code), succeed otherwise.
    class _SelectiveLLM:
        def __init__(self):
            self.calls = 0

        def __call__(self, prompt: str) -> str:
            self.calls += 1
            if "ggl-5168" in prompt:
                raise RuntimeError("boom")
            return "GOOD SYNTHESIS"

    llm = _SelectiveLLM()
    result = build_planning_result(
        config,
        (bad, good),
        today=_TODAY,
        supabase_client=None,
        sweep_llm=llm,
    )
    by_code = {
        b.project.code: b
        for blocks in result.blocks_by_account.values()
        for b in blocks
    }
    assert by_code["ggl-5168"].sweep_synthesis is None
    assert by_code["ibx-5153"].sweep_synthesis == "GOOD SYNTHESIS"
