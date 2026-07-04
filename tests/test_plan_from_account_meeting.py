"""First tests for `cp_engine.plan_from_account_meeting` (arch-phase-3,
issue #26). The module is imported by the webhook's account-meeting and
sprint-planning endpoints and was previously untested.

The Claude call is stubbed (`_call_claude` is patched in this module's
namespace, mirroring test_plan_from_transcript.py); the backend is
stubbed at `cp_engine.sync._default_backend_factory`, which the listing
helpers import inside their function bodies.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from cp_engine.plan_from_account_meeting import (
    AccountPlanError,
    _build_account_prompt,
    _build_sprint_planning_prompt,
    _company_label,
    _format_active_projects,
    generate_account_plan,
    generate_sprint_planning_plan,
    list_active_for_company,
    list_active_for_scope,
)
from cp_engine.state import ProjectState
from cp_engine.config import ProjectConfig, SyncConfig, TenantConfig


def make_tenant(root: Path, team: tuple[str, ...] = ()) -> TenantConfig:
    return TenantConfig(
        name="1p",
        display="1P Test",
        engine_version_constraint="~= 0.1",
        sync=SyncConfig(backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="ref"),
        projects=(
            ProjectConfig(code="ggl-5168", github="FirstPersonSF/ggl-5168", local_path=None),
        ),
        root=root,
        team=team,
    )


def make_project(
    code: str,
    name: str,
    *,
    source: str = "engagement",
    company_kind: str = "client",
    company_code: str | None = "GGL",
    company_name: str | None = "Google",
    status: str = "Open",
    is_internal: bool = False,
    summary: str | None = None,
) -> ProjectState:
    return ProjectState(
        code=code,
        name=name,
        source=source,  # type: ignore[arg-type]
        company_kind=company_kind,  # type: ignore[arg-type]
        company_code=company_code,
        company_name=company_name,
        status=status,
        is_internal=is_internal,
        owner="Drew",
        last_touched=datetime(2026, 5, 12, tzinfo=timezone.utc),
        deadline=None,
        one_line_summary=summary,
    )


class _FakeBackend:
    def __init__(self, projects: list[ProjectState]) -> None:
        self._projects = projects

    def read_projects(self, config: TenantConfig) -> list[ProjectState]:
        return self._projects


def _stub_backend(monkeypatch: pytest.MonkeyPatch, projects: list[ProjectState]) -> None:
    monkeypatch.setattr(
        "cp_engine.sync._default_backend_factory",
        lambda backend_name: _FakeBackend(projects),
    )


def _stub_claude(monkeypatch: pytest.MonkeyPatch, yaml_body: str) -> None:
    def fake_call_claude(prompt: str, *, model: str, api_key: str | None,
                         timeout: float = 120) -> str:
        return f"```yaml\n{yaml_body}\n```"

    monkeypatch.setattr(
        "cp_engine.plan_from_account_meeting._call_claude", fake_call_claude
    )


_VALID_PLAN_YAML = """\
transcript:
  source: fathom
  path: account-meeting

projects:
  ggl-5168:
    inbound:
      - text: "Maria wants the tier-2 cap revisited"
        date: "2026-05-12"
        who: "Maria"
    decisions:
      - text: "Hold tier-2 cap firm"
        date: "2026-05-12"
        cross_cutting: false

account_summary:
  text: "Weekly Google sync covered activation storyboards and pricing."

account_decisions:
  - text: "All Google invoices route through Brandon."
    date: "2026-05-12"
"""


# ──────────────────────────────────────────────────────────────────────
#  _company_label / _format_active_projects
# ──────────────────────────────────────────────────────────────────────


def test_company_label_prefers_company_name() -> None:
    projects = [make_project("ggl-5168", "Playbooks")]
    assert _company_label(projects) == "Google"


def test_company_label_falls_back_to_code_then_unknown() -> None:
    no_name = [make_project("x-1", "X", company_name=None, company_code="GGL")]
    assert _company_label(no_name) == "GGL"
    neither = [make_project("x-1", "X", company_name=None, company_code=None)]
    assert _company_label(neither) == "(unknown)"


def test_format_active_projects_includes_context_and_truncates(tmp_path: Path) -> None:
    config = make_tenant(tmp_path)
    # Project dir with a cp.md long enough to trip the 2,500-char cap.
    proj_dir = tmp_path / "1p" / "ggl-5168"
    proj_dir.mkdir(parents=True)
    (proj_dir / "cp.md").write_text("# GGL 5168\n" + ("context line\n" * 400))

    projects = [
        make_project("ggl-5168", "Playbooks (Activation)", summary="Storyboards in flight."),
        make_project(
            "mission-control", "Mission Control", source="initiative",
            company_kind="self-fpsf", company_code="1PI", company_name="First Person",
        ),
    ]
    block = _format_active_projects(config, projects)

    assert "### `ggl-5168` — Playbooks (Activation)" in block
    assert "_Storyboards in flight._" in block
    assert "# GGL 5168" in block  # cp.md content pulled in
    assert "[... cp.md truncated ...]" in block  # capped
    # Initiative gets the marker and, with no dir on disk, no cp.md body.
    assert "### `mission-control` — Mission Control (initiative)" in block


# ──────────────────────────────────────────────────────────────────────
#  prompt builders
# ──────────────────────────────────────────────────────────────────────


def test_build_account_prompt_carries_key_fields() -> None:
    prompt = _build_account_prompt(
        company_code="GGL",
        company_label="Google",
        week="2026-W20",
        active_projects_block="### `ggl-5168` — Playbooks",
        account_decisions_context="1. **Old decision** (2026-05-01)",
        transcript="TRANSCRIPT BODY",
        team=("Drew", "Tony"),
    )
    assert "Google (canonical code: `ggl`)" in prompt  # code lowercased
    assert "2026-W20" in prompt
    assert "### `ggl-5168` — Playbooks" in prompt
    assert "TRANSCRIPT BODY" in prompt
    assert "Drew, Tony" in prompt  # team roster block
    assert "INTERNAL TEAM MEMBERS" in prompt
    assert "Old decision" in prompt  # known-decisions context
    assert "Recent account-level decisions" in prompt


def test_build_account_prompt_without_team_or_decisions() -> None:
    prompt = _build_account_prompt(
        company_code="GGL",
        company_label="Google",
        week="2026-W20",
        active_projects_block="",
        account_decisions_context="",
        transcript="T",
        team=(),
    )
    assert "(No team roster declared in tenant config.)" in prompt
    assert "Recent account-level decisions" not in prompt


def test_build_sprint_planning_prompt_carries_scope() -> None:
    prompt = _build_sprint_planning_prompt(
        scope="1p",
        scope_label="1P (all active client engagements)",
        week="2026-W20",
        active_projects_block="### `ggl-5168` — Playbooks",
        account_decisions_context="",
        transcript="T",
        team=("Drew",),
    )
    assert "SPRINT" in prompt and "PLANNING" in prompt
    assert "1P (all active client engagements)" in prompt
    assert "2026-W20" in prompt


# ──────────────────────────────────────────────────────────────────────
#  list_active_for_company / list_active_for_scope
# ──────────────────────────────────────────────────────────────────────


def _population() -> list[ProjectState]:
    return [
        make_project("ggl-5168", "Playbooks", status="Open"),
        make_project("ggl-5200", "Ads Refresh", status="Deal"),
        make_project("ggl-5136", "Go Safety", status="Holding"),          # inactive
        make_project("ggl-9998", "Internal", status="Open", is_internal=True),
        make_project("ibx-5153", "AI Campaign", company_code="IBX",
                     company_name="Infoblox", status="Open"),
        make_project(
            "mission-control", "Mission Control", source="initiative",
            company_kind="self-fpsf", company_code="1PI",
            company_name="First Person", status="Active",
        ),
        make_project(
            "market-scorecard", "Market Scorecard", source="initiative",
            company_kind="self-fpsf", company_code="1PI",
            company_name="First Person", status="On hold",               # inactive
        ),
        make_project(
            "storyos", "StoryOS", source="initiative",
            company_kind="self-canonic", company_code="CNC",
            company_name="Canonic", status="Active",
        ),
        make_project(
            "cp-engine", "cp-engine", source="repo",
            company_kind="self-fpsf", company_code="1PI",
            company_name="First Person", status="Active",                # repos skipped
        ),
    ]


def test_list_active_for_company_filters_and_sorts(tmp_path: Path, monkeypatch) -> None:
    _stub_backend(monkeypatch, _population())
    out = list_active_for_company(make_tenant(tmp_path), "GGL")
    assert [p.code for p in out] == ["ggl-5168", "ggl-5200"]


def test_list_active_for_company_is_case_insensitive(tmp_path: Path, monkeypatch) -> None:
    _stub_backend(monkeypatch, _population())
    out = list_active_for_company(make_tenant(tmp_path), "ggl")
    assert [p.code for p in out] == ["ggl-5168", "ggl-5200"]


def test_list_active_for_company_initiatives_for_internal_kind(
    tmp_path: Path, monkeypatch
) -> None:
    """self-fpsf companies route to active initiatives; On hold and
    standalone repos are excluded."""
    _stub_backend(monkeypatch, _population())
    out = list_active_for_company(make_tenant(tmp_path), "1PI")
    assert [p.code for p in out] == ["mission-control"]


def test_list_active_for_scope_1p(tmp_path: Path, monkeypatch) -> None:
    _stub_backend(monkeypatch, _population())
    out = list_active_for_scope(make_tenant(tmp_path), "1p")
    assert [p.code for p in out] == ["ggl-5168", "ggl-5200", "ibx-5153"]


def test_list_active_for_scope_fpsf_and_canonic(tmp_path: Path, monkeypatch) -> None:
    _stub_backend(monkeypatch, _population())
    assert [p.code for p in list_active_for_scope(make_tenant(tmp_path), "fpsf")] == [
        "mission-control"
    ]
    assert [p.code for p in list_active_for_scope(make_tenant(tmp_path), "canonic")] == [
        "storyos"
    ]


def test_list_active_for_scope_explicit_codes(tmp_path: Path, monkeypatch) -> None:
    """storyos-mc is an explicit-code scope: fixed pair, listed order."""
    _stub_backend(monkeypatch, _population())
    out = list_active_for_scope(make_tenant(tmp_path), "storyos-mc")
    assert [p.code for p in out] == ["storyos", "mission-control"]


def test_list_active_for_scope_unknown_raises(tmp_path: Path, monkeypatch) -> None:
    _stub_backend(monkeypatch, _population())
    with pytest.raises(AccountPlanError, match="unknown sprint-planning scope"):
        list_active_for_scope(make_tenant(tmp_path), "nope")


# ──────────────────────────────────────────────────────────────────────
#  generate_account_plan
# ──────────────────────────────────────────────────────────────────────


def test_generate_account_plan_happy_path(tmp_path: Path, monkeypatch) -> None:
    _stub_claude(monkeypatch, _VALID_PLAN_YAML)
    result = generate_account_plan(
        config=make_tenant(tmp_path),
        company_code="GGL",
        meeting_id="mtg-1",
        transcript_text="the transcript",
        active_projects=[make_project("ggl-5168", "Playbooks")],
        week_iso="2026-W20",
    )
    assert result.company_code == "GGL"
    assert result.meeting_id == "mtg-1"
    assert result.project_codes == ("ggl-5168",)
    assert "ggl-5168" in result.plan["projects"]
    # company + week injected server-side into account_summary…
    assert result.plan["account_summary"]["company"] == "ggl"
    assert result.plan["account_summary"]["week"] == "2026-W20"
    # …and company into each account_decision.
    assert result.plan["account_decisions"][0]["company"] == "ggl"


def test_generate_account_plan_no_active_projects_raises(tmp_path: Path) -> None:
    with pytest.raises(AccountPlanError, match="no active projects"):
        generate_account_plan(
            config=make_tenant(tmp_path),
            company_code="GGL",
            meeting_id="mtg-1",
            transcript_text="t",
            active_projects=[],
        )


def test_generate_account_plan_non_yaml_response_raises(
    tmp_path: Path, monkeypatch
) -> None:
    _stub_claude(monkeypatch, "not: valid: yaml: [")
    with pytest.raises(AccountPlanError, match="non-YAML"):
        generate_account_plan(
            config=make_tenant(tmp_path),
            company_code="GGL",
            meeting_id="mtg-1",
            transcript_text="t",
            active_projects=[make_project("ggl-5168", "Playbooks")],
            week_iso="2026-W20",
        )


def test_generate_account_plan_non_mapping_plan_raises(
    tmp_path: Path, monkeypatch
) -> None:
    _stub_claude(monkeypatch, "- just\n- a\n- list")
    with pytest.raises(AccountPlanError, match="non-mapping"):
        generate_account_plan(
            config=make_tenant(tmp_path),
            company_code="GGL",
            meeting_id="mtg-1",
            transcript_text="t",
            active_projects=[make_project("ggl-5168", "Playbooks")],
            week_iso="2026-W20",
        )


def test_generate_account_plan_invalid_verb_fails_validation(
    tmp_path: Path, monkeypatch
) -> None:
    bad = (
        "projects:\n"
        "  ggl-5168:\n"
        "    made_up_verb:\n"
        "      - text: x\n"
        "account_summary:\n"
        "  text: summary\n"
    )
    _stub_claude(monkeypatch, bad)
    with pytest.raises(AccountPlanError, match="plan failed validation"):
        generate_account_plan(
            config=make_tenant(tmp_path),
            company_code="GGL",
            meeting_id="mtg-1",
            transcript_text="t",
            active_projects=[make_project("ggl-5168", "Playbooks")],
            week_iso="2026-W20",
        )


def test_generate_account_plan_truncates_long_transcript(
    tmp_path: Path, monkeypatch
) -> None:
    seen: dict[str, str] = {}

    def fake_call_claude(prompt: str, *, model: str, api_key: str | None,
                         timeout: float = 120) -> str:
        seen["prompt"] = prompt
        return f"```yaml\n{_VALID_PLAN_YAML}\n```"

    monkeypatch.setattr(
        "cp_engine.plan_from_account_meeting._call_claude", fake_call_claude
    )
    generate_account_plan(
        config=make_tenant(tmp_path),
        company_code="GGL",
        meeting_id="mtg-1",
        # Cap is 400k chars now (a one-hour meeting is ~60k — the old 60k
        # cap silently cut real sprint-planning meetings).
        transcript_text="x" * 450_000,
        active_projects=[make_project("ggl-5168", "Playbooks")],
        week_iso="2026-W20",
    )
    assert "[... transcript truncated ...]" in seen["prompt"]


# ──────────────────────────────────────────────────────────────────────
#  generate_sprint_planning_plan
# ──────────────────────────────────────────────────────────────────────


def test_generate_sprint_planning_plan_stamps_pseudo_company(
    tmp_path: Path, monkeypatch
) -> None:
    _stub_claude(monkeypatch, _VALID_PLAN_YAML)
    result = generate_sprint_planning_plan(
        config=make_tenant(tmp_path),
        scope="fpsf",
        meeting_id="mtg-2",
        transcript_text="t",
        active_projects=[
            make_project(
                "mission-control", "Mission Control", source="initiative",
                company_kind="self-fpsf", company_code="1PI",
                company_name="First Person", status="Active",
            )
        ],
        week_iso="2026-W20",
    )
    assert result.company_code == "fpsf-internal"
    assert result.plan["account_summary"]["company"] == "fpsf-internal"
    assert result.plan["account_summary"]["week"] == "2026-W20"
    assert result.plan["account_decisions"][0]["company"] == "fpsf-internal"


def test_generate_sprint_planning_plan_unknown_scope_raises(tmp_path: Path) -> None:
    with pytest.raises(AccountPlanError, match="unknown sprint-planning scope"):
        generate_sprint_planning_plan(
            config=make_tenant(tmp_path),
            scope="bogus",
            meeting_id="mtg-2",
            transcript_text="t",
            active_projects=[make_project("ggl-5168", "Playbooks")],
        )


def test_generate_sprint_planning_plan_no_projects_raises(tmp_path: Path) -> None:
    with pytest.raises(AccountPlanError, match="no active projects"):
        generate_sprint_planning_plan(
            config=make_tenant(tmp_path),
            scope="1p",
            meeting_id="mtg-2",
            transcript_text="t",
            active_projects=[],
        )
