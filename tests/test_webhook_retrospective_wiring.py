"""Tests for the Retrospective append wiring (spine-inversion Part B).

Every tagged Fathom meeting should leave a dated entry in the project's
`shell/Retrospective/meeting-history.md`, embedding the WHOLE Fathom
summary (anti-compression). The append is best-effort and MUST NOT break
auto-ingest: a missing/empty summary, an unresolvable project dir, or any
other failure degrades to "no retrospective entry written" rather than a
failed ingest.

These tests exercise the extracted helper `_append_retrospective` directly
against a tmp tenant with a resolvable project dir.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# webhook/ is a sibling of src/; not on the import path by default.
_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import main as webhook_main  # the module is `main.py`


def _config_for(tenant: Path):
    from cp_engine.config import SyncConfig, TenantConfig

    return TenantConfig(
        name="cp",
        display="cp",
        engine_version_constraint="~= 0.21",
        sync=SyncConfig(
            backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="ref"
        ),
        projects=(),
        root=tenant,
    )


def _make_project_dir(tenant: Path, code: str) -> Path:
    """Create a resolvable working dir under firstpersonsf/<code>/."""
    proj = tenant / "firstpersonsf" / code
    proj.mkdir(parents=True, exist_ok=True)
    return proj


def test_append_retrospective_writes_whole_summary(tmp_path: Path) -> None:
    code = "ggl-5168"
    proj = _make_project_dir(tmp_path, code)
    config = _config_for(tmp_path)

    summary = (
        "Discussed the activation pop-up. Brandon will ship the final. "
        "Rena flagged the legal copy as a blocker."
    )
    meeting = {
        "id": "meet-abc",
        "title": "Activation sync",
        "meeting_date": "2026-06-10",
        "summary": summary,
        "participants": [{"name": "Brandon"}, "Rena"],
        "fathom_url": "https://fathom.video/meet-abc",
    }
    action_items = [{"description": "Ship pop-up final", "assignee": {"name": "Brandon"}}]

    status = webhook_main._append_retrospective(
        config=config,
        code=code,
        meeting=meeting,
        action_items=action_items,
        plan={"projects": {code: {}}},
    )

    history = proj / "shell" / "Retrospective" / "meeting-history.md"
    assert history.exists(), "retrospective history file was not created"
    text = history.read_text()
    assert summary in text, "the WHOLE Fathom summary must be embedded"
    assert "Activation sync" in text
    assert "Brandon" in text and "Rena" in text
    assert "meet-abc" in text  # idempotency marker
    assert status == "appended"


def test_append_retrospective_skips_when_summary_empty(tmp_path: Path) -> None:
    code = "ggl-5168"
    proj = _make_project_dir(tmp_path, code)
    config = _config_for(tmp_path)

    meeting = {
        "id": "meet-abc",
        "title": "Activation sync",
        "meeting_date": "2026-06-10",
        "summary": "",  # empty → nothing to preserve
        "participants": [],
        "fathom_url": None,
    }

    status = webhook_main._append_retrospective(
        config=config,
        code=code,
        meeting=meeting,
        action_items=[],
        plan=None,
    )

    history = proj / "shell" / "Retrospective" / "meeting-history.md"
    assert not history.exists(), "no file should be written when summary is empty"
    assert status == "skipped"


def test_append_retrospective_skips_when_meeting_none(tmp_path: Path) -> None:
    code = "ggl-5168"
    _make_project_dir(tmp_path, code)
    config = _config_for(tmp_path)

    status = webhook_main._append_retrospective(
        config=config, code=code, meeting=None, action_items=[], plan=None,
    )
    assert status == "skipped"


def test_append_retrospective_is_idempotent_on_meeting_id(tmp_path: Path) -> None:
    code = "ggl-5168"
    proj = _make_project_dir(tmp_path, code)
    config = _config_for(tmp_path)

    meeting = {
        "id": "meet-abc",
        "title": "Activation sync",
        "meeting_date": "2026-06-10",
        "summary": "A real summary.",
        "participants": [],
        "fathom_url": None,
    }

    first = webhook_main._append_retrospective(
        config=config, code=code, meeting=meeting, action_items=[], plan=None,
    )
    second = webhook_main._append_retrospective(
        config=config, code=code, meeting=meeting, action_items=[], plan=None,
    )
    assert first == "appended"
    assert second == "duplicate"

    history = proj / "shell" / "Retrospective" / "meeting-history.md"
    assert history.read_text().count("A real summary.") == 1


def test_append_retrospective_never_raises_on_unresolvable_dir(tmp_path: Path) -> None:
    """An unresolvable project code must degrade to a status, never raise."""
    config = _config_for(tmp_path)
    meeting = {
        "id": "meet-abc",
        "title": "X",
        "meeting_date": "2026-06-10",
        "summary": "A summary.",
        "participants": [],
        "fathom_url": None,
    }
    # No project dir created → find_shell_dir raises internally; helper swallows.
    status = webhook_main._append_retrospective(
        config=config, code="nope-9999", meeting=meeting, action_items=[], plan=None,
    )
    assert status == "error"
