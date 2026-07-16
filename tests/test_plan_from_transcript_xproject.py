"""Tests for cross-project detection in the single-project classifier (#88):
roster block rendering, annotation validation/application, and the
generate_plan round trip with a stubbed Claude response.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from cp_engine.plan_from_transcript import (
    _apply_cross_project_annotations,
    _build_prompt,
    _build_roster_block,
)

from tests.test_plan_from_transcript import _make_tenant_config


def _roster() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            code="ibx-5192", name="IBX 5192 SRS", company_name="Infoblox",
            source="engagement",
        ),
        SimpleNamespace(
            code="sap-5174", name="SAP 5174 Concur", company_name="SAP",
            source="engagement",
        ),
        SimpleNamespace(
            code="storyos", name="StoryOS", company_name="Canonic",
            source="initiative",
        ),
    ]


# ── roster block ──────────────────────────────────────────────────────


def test_roster_block_lists_other_projects_and_skips_self() -> None:
    block = _build_roster_block(_roster(), "ibx-5192")
    assert "`sap-5174` — SAP 5174 Concur (SAP)" in block
    assert "`storyos` — StoryOS (Canonic) [initiative]" in block
    assert "ibx-5192" not in block


def test_roster_block_empty_turns_detection_off() -> None:
    off = _build_roster_block(None, "ibx-5192")
    assert "detection is OFF" in off
    # Roster containing only the tagged project → also OFF.
    only_self = [_roster()[0]]
    assert "detection is OFF" in _build_roster_block(only_self, "ibx-5192")


def test_prompt_includes_roster_in_both_templates() -> None:
    for code in ("ibx-5192", "storyos"):
        prompt = _build_prompt(
            transcript="t", project_context="(none)", project_code=code,
            transcript_path=Path("/tmp/t.txt"), team=("drew",),
            roster=_roster(),
        )
        assert "cross-project detection roster" in prompt
        assert "cross_project_confidence" in prompt


# ── annotation validation + application ───────────────────────────────


def _plan_with(items_by_verb: dict) -> dict:
    return {"projects": {"ibx-5192": items_by_verb}}


def test_valid_annotation_markers_item_and_collects_proposal() -> None:
    plan = _plan_with({
        "decisions": [{
            "text": "Customer sessions on the 28th",
            "date": "2026-07-15",
            "cross_project": "sap-5174",
            "cross_project_confidence": "high",
        }],
    })
    proposals = _apply_cross_project_annotations(
        plan, project_code="ibx-5192", roster=_roster()
    )
    item = plan["projects"]["ibx-5192"]["decisions"][0]
    # Annotation fields are popped; the text carries the review marker.
    assert "cross_project" not in item
    assert "cross_project_confidence" not in item
    assert item["text"] == (
        "Customer sessions on the 28th [cross-project? → sap-5174]"
    )
    assert len(proposals) == 1
    p = proposals[0]
    assert p["target_code"] == "sap-5174"
    assert p["verb"] == "decisions"
    assert p["confidence"] == "high"
    # The proposal payload is the CLEAN item — marker-free.
    assert p["text"] == "Customer sessions on the 28th"
    assert p["item"]["text"] == "Customer sessions on the 28th"
    assert p["item"]["date"] == "2026-07-15"


def test_invented_code_self_code_and_low_confidence_drop() -> None:
    plan = _plan_with({
        "asks": [
            {"text": "a", "cross_project": "zzz-9999"},           # not in roster
            {"text": "b", "cross_project": "ibx-5192"},           # self
            {"text": "c", "cross_project": "sap-5174",
             "cross_project_confidence": "low"},                  # low confidence
        ],
    })
    proposals = _apply_cross_project_annotations(
        plan, project_code="ibx-5192", roster=_roster()
    )
    assert proposals == []
    for item in plan["projects"]["ibx-5192"]["asks"]:
        # Annotations always popped; texts unmodified.
        assert "cross_project" not in item
        assert "[cross-project?" not in item["text"]


def test_no_roster_drops_all_annotations() -> None:
    plan = _plan_with({
        "decisions": [{"text": "x", "cross_project": "sap-5174"}],
    })
    assert (
        _apply_cross_project_annotations(
            plan, project_code="ibx-5192", roster=None
        )
        == []
    )
    assert plan["projects"]["ibx-5192"]["decisions"][0]["text"] == "x"


def test_non_sprint_verbs_are_ignored() -> None:
    plan = _plan_with({
        "set-milestone": [{
            "deliverable": "ship", "text": "ship",
            "cross_project": "sap-5174",
            "cross_project_confidence": "high",
        }],
    })
    assert (
        _apply_cross_project_annotations(
            plan, project_code="ibx-5192", roster=_roster()
        )
        == []
    )


def test_confidence_defaults_to_medium() -> None:
    plan = _plan_with({
        "risks": [{"text": "r", "severity": "watching",
                   "cross_project": "storyos"}],
    })
    proposals = _apply_cross_project_annotations(
        plan, project_code="ibx-5192", roster=_roster()
    )
    assert proposals[0]["confidence"] == "medium"
    assert proposals[0]["verb"] == "risks"
    assert proposals[0]["target_code"] == "storyos"


# ── generate_plan round trip ──────────────────────────────────────────


def test_generate_plan_collects_cross_project(tmp_path, monkeypatch) -> None:
    from tests.test_plan_from_transcript import _stub_claude_response
    from cp_engine.plan_from_transcript import generate_plan

    _stub_claude_response(
        monkeypatch,
        """
        transcript:
          source: fathom
          path: /tmp/t.txt
        projects:
          ibx-5192:
            decisions:
              - text: "SRS date locked for Sept 10"
                date: "2026-07-15"
              - text: "Customer sessions on the 28th"
                date: "2026-07-15"
                cross_project: "sap-5174"
                cross_project_confidence: "high"
        """,
    )
    config = _make_tenant_config(tmp_path)
    transcript_path = tmp_path / "transcript.txt"
    transcript_path.write_text("2026-07-15 - ibx-5192 sync\nDrew: hi\n")

    result = generate_plan(
        config=config,
        project_code="ibx-5192",
        transcript_path=transcript_path,
        api_key="stub-key",
        roster=_roster(),
    )
    assert len(result.cross_project) == 1
    assert result.cross_project[0]["target_code"] == "sap-5174"
    decisions = result.plan["projects"]["ibx-5192"]["decisions"]
    assert decisions[0]["text"] == "SRS date locked for Sept 10"
    assert decisions[1]["text"].endswith("[cross-project? → sap-5174]")
