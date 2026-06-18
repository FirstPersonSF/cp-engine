"""Tests for full-transcript persistence (workshop-synthesis Task 1 / Piece A).

Every project an auto-ingested meeting maps to should get the FULL verbatim
Fathom transcript written into its `meeting-transcripts/<date> <title>.txt`,
matching the readable filename style of the existing hand-added transcripts.

The persist is best-effort: a failure logs a warning and degrades to "no
transcript file written" rather than aborting the ingest or breaking the commit.

These tests exercise the helper `_persist_transcript` directly and assert the
best-effort swallow on the ingest path.
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


# ─────────────────────────────────────────────────────────────
#  _persist_transcript — the helper itself
# ─────────────────────────────────────────────────────────────


def test_persist_writes_file_and_creates_dir(tmp_path: Path) -> None:
    proj = tmp_path / "ggl-5168"
    proj.mkdir()
    text = "Drew: hello.\nMarcello: hi there.\n"

    path = webhook_main._persist_transcript(
        proj, "2026-06-12", "Sync on AI workshop Agenda and Plan", text
    )

    expected = proj / "meeting-transcripts" / "2026-06-12 Sync on AI workshop Agenda and Plan.txt"
    assert path == expected
    assert path.exists()
    assert (proj / "meeting-transcripts").is_dir()
    assert path.read_text(encoding="utf-8") == text


def test_persist_sanitizes_slashes_and_control_chars(tmp_path: Path) -> None:
    proj = tmp_path / "ggl-5168"
    proj.mkdir()

    # `/` is a path separator; \t/\n are control chars. They must become
    # spaces and collapse — readable, not slugified.
    dirty = "Q3/Q4  planning\tsession\nreview"
    path = webhook_main._persist_transcript(proj, "2026-06-12", dirty, "body")

    # No subdirs created from the slash; the title is one readable segment.
    assert path.parent == proj / "meeting-transcripts"
    assert path.name == "2026-06-12 Q3 Q4 planning session review.txt"
    assert path.exists()


def test_persist_overwrites_existing(tmp_path: Path) -> None:
    proj = tmp_path / "ggl-5168"
    proj.mkdir()

    first = webhook_main._persist_transcript(proj, "2026-06-12", "Sync", "old text")
    second = webhook_main._persist_transcript(proj, "2026-06-12", "Sync", "new text")

    assert first == second  # same path
    assert second.read_text(encoding="utf-8") == "new text"


def test_persist_empty_date_falls_back_to_title(tmp_path: Path) -> None:
    proj = tmp_path / "ggl-5168"
    proj.mkdir()

    path = webhook_main._persist_transcript(proj, "", "Kickoff call", "body")
    assert path.name == "Kickoff call.txt"


def test_persist_empty_title_falls_back(tmp_path: Path) -> None:
    proj = tmp_path / "ggl-5168"
    proj.mkdir()

    # Date present, title empty → date-only stem.
    dated = webhook_main._persist_transcript(proj, "2026-06-12", "", "body")
    assert dated.name == "2026-06-12.txt"

    # Both empty → "Untitled meeting".
    neither = webhook_main._persist_transcript(proj, "", "", "body")
    assert neither.name == "Untitled meeting.txt"


# ─────────────────────────────────────────────────────────────
#  Best-effort wiring — a persist failure must not abort the ingest
# ─────────────────────────────────────────────────────────────


def _config_for(tenant: Path):
    from cp_engine.config import SyncConfig, TenantConfig

    return TenantConfig(
        name="cp",
        display="cp",
        engine_version_constraint="~= 0.26",
        sync=SyncConfig(
            backend="mc-2", cron="0 * * * *", mc_2_supabase_project_ref="ref"
        ),
        projects=(),
        root=tenant,
    )


def test_persist_failure_is_swallowed_and_ingest_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `_persist_transcript` raises, `_perform_auto_ingest` must swallow it
    and complete the per-project ingest + commit anyway.

    The full pipeline is faked at its seams (transcript fetch, clone, stage,
    meeting fetch, per-project ingest, commit, downstream best-effort blocks)
    so we exercise only the loop's transcript-persist try/except.
    """
    code = "ggl-5168"
    tenant = tmp_path / "tenant"
    proj = tenant / "firstpersonsf" / code
    proj.mkdir(parents=True)

    from contextlib import contextmanager

    @contextmanager
    def fake_cloned_tenant():
        yield tenant

    monkeypatch.setattr(webhook_main, "_cloned_tenant", fake_cloned_tenant)
    monkeypatch.setattr(webhook_main, "_load_tenant_config", lambda root: _config_for(tenant))
    monkeypatch.setattr(
        webhook_main, "_stage_transcript",
        lambda root, mid, text: tenant / "staged.txt",
    )
    monkeypatch.setattr(
        webhook_main, "_fetch_meeting",
        lambda mid: {"id": mid, "title": "Workshop", "meeting_date": "2026-06-16", "action_items": []},
    )

    # The per-project ingest reports a real write so the commit path runs.
    def fake_ingest_one(*, config, code, transcript_path, action_items, meeting_id, meeting):
        return {
            "code": code,
            "files_written": ["sprints/2026-W25/ggl-5168.md"],
            "plan_summary": {"outbound": 1},
            "errors": [],
            "status": "ok",
        }

    monkeypatch.setattr(webhook_main, "_ingest_one_project", fake_ingest_one)

    commits: list[str] = []

    def fake_commit(*, tenant_root, meeting_id, ingested):
        commits.append("deadbeef")
        return "deadbeef"

    monkeypatch.setattr(webhook_main, "_commit_and_push", fake_commit)

    # The persist itself blows up — the loop must swallow it.
    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(webhook_main, "_persist_transcript", boom)

    # Downstream best-effort blocks — stub them out so they don't reach
    # real services. They're not under test here.
    monkeypatch.setattr(webhook_main, "propose_clickup_tasks", lambda mid, codes: {})
    monkeypatch.setattr(webhook_main, "_generate_meeting_artifacts", lambda **kw: {})
    monkeypatch.setattr(webhook_main, "_log_run_to_supabase", lambda **kw: None)

    result = webhook_main._perform_auto_ingest(
        meeting_id="meet-xyz",
        project_codes=[code],
        transcript_text="A real transcript body.",
    )

    # Ingest completed despite the persist failure, and the commit still ran.
    assert commits == ["deadbeef"]
    assert result["commit_sha"] == "deadbeef"
    entry = result["ingested"][0]
    assert entry["code"] == code
    assert entry["files_written"]
