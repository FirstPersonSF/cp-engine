"""Tests for v0.15.1 Fix 1A: webhook → execute_plan supabase wiring.

REGRESSION GUARD against the v0.15.0 "headline feature is dead code" bug:
the webhook called ``execute_plan(...)`` without ``supabase=`` or
``meeting_id=``, so the new ``set-milestone`` / ``set-client-ask-task``
verbs hit ingest.py's "no Supabase client supplied — skipping" branch
and silently dropped every milestone the LLM produced. Proposal rows
should have started arriving in the dashboard the day v0.15.0 shipped;
zero showed up.

These tests assert that ``_ingest_one_project`` threads the meeting_id
and a real Supabase client through to ``execute_plan`` so the proposal
verbs reach their write path.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# webhook/ is a sibling of src/; not on the import path by default.
_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import main as webhook_main
import pipeline  # the module is `main.py`


def _scaffold_sprint_file(tenant: Path, *, week: str, code: str) -> Path:
    """Minimal engagement sprint file so execute_plan's project loop runs."""
    week_dir = tenant / "sprints" / week
    week_dir.mkdir(parents=True, exist_ok=True)
    sprint_file = week_dir / f"{code}.md"
    sprint_file.write_text(
        "# Sprint\n\n"
        "## Client communication\n\n"
        "### Inbound\n\n- _<inbound>_\n\n"
        "### Open asks\n\n- _<asks>_\n\n"
        "### Stakeholders\n\n- _<stakeholders>_\n\n"
        "### Slack digest\n\n- _<digest>_\n\n"
        "## Meeting notes & decisions\n\n"
        "### Decisions\n\n- _<decisions>_\n\n"
        "## Dependencies & risks\n\n- _<risks>_\n"
    )
    return sprint_file


def _fake_supabase_for_project(
    *,
    project_id: str = "p1",
    clickup_list_id: str | None = "L1",
    code: str = "ggl-5168",
) -> MagicMock:
    """Same shape as tests/test_ingest._fake_supabase_for_project but local.

    Wires the SELECT-then-EQ chain to return one project row, and the
    SELECT-EQ-IN chain (dedupe lookup) to return [] (no existing row).
    """
    number = int(code.rsplit("-", 1)[-1])
    project_row = {
        "id": project_id,
        "number": number,
        "enable_clickup": True,
    }
    client = MagicMock()
    client.table.return_value.select.return_value.eq.return_value.execute.return_value.data = [
        project_row
    ]
    # Bindings lookup (read-flip): .select(...).in_(...).execute().data → the
    # clickup '' binding carrying the list id.
    client.table.return_value.select.return_value.in_.return_value.execute.return_value.data = (
        [{
            "project_id": project_id, "initiative_id": None, "service": "clickup",
            "external_ref": {"id": clickup_list_id, "extra": {"list_id": clickup_list_id}},
            "label": "",
        }]
        if clickup_list_id
        else []
    )
    # Dedupe lookup: .eq("cp_ask_hash", h).in_("status", [...]).execute().data → []
    client.table.return_value.select.return_value.eq.return_value.in_.return_value.execute.return_value.data = []
    return client


def test_ingest_one_project_passes_supabase_and_meeting_id_to_execute_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGRESSION GUARD for the v0.15.0 dead-code bug.

    Before v0.15.1: webhook called execute_plan without supabase or
    meeting_id; the new verbs silently skipped.

    After v0.15.1: _ingest_one_project threads both through; a milestone
    in the generated plan must actually reach the Supabase mock as an
    insert.
    """
    from cp_engine.config import SyncConfig, TenantConfig
    from cp_engine.plan_from_transcript import GeneratedPlan

    # Minimal tenant with a W22 sprint file.
    tenant = tmp_path
    # Use the current ISO week so execute_plan's `_current_week_iso`
    # finds the sprint file. Compute it the same way execute_plan does.
    from cp_engine.sprints import current_sprint_week_iso
    from datetime import datetime
    week = current_sprint_week_iso(datetime.now())
    _scaffold_sprint_file(tenant, week=week, code="ggl-5168")

    config = TenantConfig(
        name="cp",
        display="cp",
        engine_version_constraint="~= 0.15",
        sync=SyncConfig(
            backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="ref"
        ),
        projects=(),
        root=tenant,
    )

    # Stub generate_plan to return a plan that includes set-milestone.
    fake_plan = {
        "transcript": {"source": "fathom", "path": "x.txt"},
        "projects": {
            "ggl-5168": {
                "set-milestone": [
                    {
                        "deliverable": "Pop-up final to Rena",
                        "date": "2026-06-10",
                        "owner": "brandon",
                        "confidence": "high",
                    }
                ]
            }
        },
    }
    monkeypatch.setattr(pipeline, "generate_plan",
        lambda **kw: GeneratedPlan(
            plan=fake_plan,
            raw_response="",
            project_code=kw.get("project_code", "ggl-5168"),
            transcript_path=kw.get("transcript_path", Path("/tmp/x")),
            model="stub",
        ),
    )

    # Stub _create_supabase_client to return our recording mock.
    sb = _fake_supabase_for_project(code="ggl-5168")
    monkeypatch.setattr(pipeline, "_create_supabase_client", lambda: sb)

    transcript_path = tmp_path / "transcript.txt"
    transcript_path.write_text("x\n")

    entry = webhook_main._ingest_one_project(
        config=config,
        code="ggl-5168",
        transcript_path=transcript_path,
        meeting_id="meet-xyz",
    )

    # No errors from the verb (proves the supabase= path was taken, not
    # the "skipped silently" path which would log INFO and continue).
    assert entry["errors"] == [], entry["errors"]

    # The mock received the insert — this is the bug fix proof.
    insert_calls = sb.table.return_value.insert.call_args_list
    assert insert_calls, (
        "execute_plan never inserted the milestone — supabase= wiring "
        "regressed back to the v0.15.0 dead-code state"
    )
    inserted_row = insert_calls[-1].args[0]
    assert inserted_row["task_type"] == "milestone"
    assert inserted_row["is_milestone"] is True
    assert inserted_row["meeting_id"] == "meet-xyz", (
        "meeting_id must be threaded through so the dashboard can link "
        "review actions back to the originating Fathom meeting"
    )
    # cp_ask_hash from Fix 1B is present (round-tripping dedupe).
    assert "cp_ask_hash" in inserted_row
