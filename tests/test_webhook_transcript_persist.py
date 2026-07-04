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

import main as webhook_main
import git_ops
import pipeline  # the module is `main.py`


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


def test_persist_same_meeting_reingest_overwrites_in_place(tmp_path: Path) -> None:
    """Re-ingesting the SAME meeting (same stem + same body) overwrites
    in place — idempotent backfill, no proliferating duplicates."""
    proj = tmp_path / "ggl-5168"
    proj.mkdir()

    first = webhook_main._persist_transcript(proj, "2026-06-12", "Sync", "same body")
    second = webhook_main._persist_transcript(proj, "2026-06-12", "Sync", "same body")

    assert first == second  # same path, overwritten
    assert second.read_text(encoding="utf-8") == "same body"
    assert list((proj / "meeting-transcripts").iterdir()) == [second]


def test_persist_distinct_same_stem_meetings_do_not_clobber(tmp_path: Path) -> None:
    """Two DIFFERENT meetings that sanitize to the same stem (same day,
    same title — this happens in the tenant) must NOT silently overwrite
    each other. The second lands on a ` (2)` suffix; both survive."""
    proj = tmp_path / "ggl-5168"
    proj.mkdir()

    first = webhook_main._persist_transcript(proj, "2026-06-12", "Sync", "meeting one body")
    second = webhook_main._persist_transcript(proj, "2026-06-12", "Sync", "meeting two body")

    assert first != second
    assert first.name == "2026-06-12 Sync.txt"
    assert second.name == "2026-06-12 Sync (2).txt"
    assert first.read_text(encoding="utf-8") == "meeting one body"
    assert second.read_text(encoding="utf-8") == "meeting two body"


def test_persist_slices_iso_timestamp_to_date(tmp_path: Path) -> None:
    """`meeting_date` is a full ISO timestamp in prod; only the date
    prefix is used (matching the sibling meetings/ writer), so no colon
    leaks into the filename."""
    proj = tmp_path / "ggl-5168"
    proj.mkdir()

    path = webhook_main._persist_transcript(
        proj, "2026-06-12T14:30:00+00:00", "Workshop", "body"
    )
    assert path.name == "2026-06-12 Workshop.txt"
    assert ":" not in path.name


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

    monkeypatch.setattr(git_ops, "_cloned_tenant", fake_cloned_tenant)
    monkeypatch.setattr(pipeline, "_load_tenant_config", lambda root: _config_for(tenant))
    monkeypatch.setattr(pipeline, "_stage_transcript",
        lambda root, mid, text: tenant / "staged.txt",
    )
    monkeypatch.setattr(pipeline, "_fetch_meeting",
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

    monkeypatch.setattr(pipeline, "_ingest_one_project", fake_ingest_one)

    commits: list[str] = []

    def fake_commit(*, tenant_root, meeting_id, ingested):
        commits.append("deadbeef")
        return "deadbeef"

    monkeypatch.setattr(git_ops, "_commit_and_push", fake_commit)

    # The persist itself blows up — the loop must swallow it.
    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(pipeline, "_persist_transcript", boom)

    # Downstream best-effort blocks — stub them out so they don't reach
    # real services. They're not under test here.
    monkeypatch.setattr(pipeline, "propose_clickup_tasks", lambda mid, codes: {})
    monkeypatch.setattr(pipeline, "_generate_meeting_artifacts", lambda **kw: {})
    monkeypatch.setattr(pipeline, "_log_run_to_supabase", lambda **kw: None)

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


def test_transcript_only_commit_fires_with_no_bullets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A meeting that writes NO bullets (empty `files_written`) but DOES
    persist a transcript must still produce a commit — the trigger is
    `files_written OR transcript_persisted`. This exercises the new
    transcript-only commit path that the original change added.
    """
    code = "ggl-5168"
    tenant = tmp_path / "tenant"
    proj = tenant / "firstpersonsf" / code
    proj.mkdir(parents=True)

    from contextlib import contextmanager

    @contextmanager
    def fake_cloned_tenant():
        yield tenant

    monkeypatch.setattr(git_ops, "_cloned_tenant", fake_cloned_tenant)
    monkeypatch.setattr(pipeline, "_load_tenant_config", lambda root: _config_for(tenant))
    monkeypatch.setattr(pipeline, "_stage_transcript",
        lambda root, mid, text: tenant / "staged.txt",
    )
    monkeypatch.setattr(pipeline, "_fetch_meeting",
        lambda mid: {"id": mid, "title": "Workshop", "meeting_date": "2026-06-16", "action_items": []},
    )
    # find_spine_dir must resolve to the real on-disk project dir so the
    # (non-faked) _persist_transcript can write into it.
    monkeypatch.setattr(pipeline, "find_spine_dir", lambda root, c: proj)

    # The per-project ingest reports NO files written — the only thing
    # that should trigger the commit is the persisted transcript.
    def fake_ingest_one(*, config, code, transcript_path, action_items, meeting_id, meeting):
        return {
            "code": code,
            "files_written": [],
            "plan_summary": {},
            "errors": [],
            "status": "ok",
        }

    monkeypatch.setattr(pipeline, "_ingest_one_project", fake_ingest_one)

    commits: list[str] = []

    def fake_commit(*, tenant_root, meeting_id, ingested):
        commits.append("cafef00d")
        return "cafef00d"

    monkeypatch.setattr(git_ops, "_commit_and_push", fake_commit)

    monkeypatch.setattr(pipeline, "propose_clickup_tasks", lambda mid, codes: {})
    monkeypatch.setattr(pipeline, "_generate_meeting_artifacts", lambda **kw: {})
    monkeypatch.setattr(pipeline, "_log_run_to_supabase", lambda **kw: None)

    result = webhook_main._perform_auto_ingest(
        meeting_id="meet-xyz",
        project_codes=[code],
        transcript_text="A real transcript body.",
    )

    # No bullets, but the transcript landed → a commit still fired.
    assert commits == ["cafef00d"]
    assert result["commit_sha"] == "cafef00d"
    transcript = proj / "meeting-transcripts" / "2026-06-16 Workshop.txt"
    assert transcript.exists()

    # Observability: the entry is honest about transcript-only vs bullets.
    entry = result["ingested"][0]
    assert entry["files_written"] == []
    assert entry["transcript_persisted"] is True


# ─────────────────────────────────────────────────────────────
#  _commit_and_push — message attribution
# ─────────────────────────────────────────────────────────────


def _capture_commit_message(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch the shared push tail to capture the commit message instead of
    touching git, and return the (single-element) capture list."""
    captured: list[str] = []

    def fake_push(tenant_root, message):
        captured.append(message)
        return "feedface"

    monkeypatch.setattr(git_ops, "_commit_with_message_and_push", fake_push)
    return captured


def test_commit_message_transcript_only_attributes_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transcript-only commit (no `files_written`, `transcript_persisted`
    True) must name the project in the subject and carry a
    `- <code>: transcript only` body line — not a blank `[auto-ingest] :`."""
    captured = _capture_commit_message(monkeypatch)

    entry = {
        "code": "ggl-5168",
        "files_written": [],
        "plan_summary": {},
        "errors": [],
        "transcript_persisted": True,
    }
    sha = webhook_main._commit_and_push(
        tenant_root=tmp_path, meeting_id="meet-abcdef12", ingested=[entry]
    )

    assert sha == "feedface"
    msg = captured[0]
    subject = msg.splitlines()[0]
    assert subject == "[auto-ingest] ggl-5168: meeting meet-abc"
    assert "- ggl-5168: transcript only" in msg


def test_commit_message_files_written_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: a normal files-written commit message is unchanged
    by the transcript-only attribution work."""
    captured = _capture_commit_message(monkeypatch)

    entry = {
        "code": "ggl-5168",
        "files_written": ["sprints/2026-W25/ggl-5168.md"],
        "plan_summary": {"outbound": 1},
        "errors": [],
        "transcript_persisted": True,  # present but irrelevant when files wrote
    }
    webhook_main._commit_and_push(
        tenant_root=tmp_path, meeting_id="meet-abcdef12", ingested=[entry]
    )

    msg = captured[0]
    assert msg.splitlines()[0] == "[auto-ingest] ggl-5168: meeting meet-abc"
    assert "- ggl-5168: outbound=1" in msg
    # Not mislabeled "transcript only" when bullets were written.
    assert "transcript only" not in msg


def test_commit_message_account_caller_without_transcript_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Account/sprint-planning callers pass entries that never set
    `transcript_persisted`. The `.get` falsy path must still build a valid
    message (these callers only commit when files were written)."""
    captured = _capture_commit_message(monkeypatch)

    entry = {
        "code": "account:ggl",
        "files_written": ["weekly-cp.md"],
        "plan_summary": {"account_summary": 1},
        "errors": [],
        # no `transcript_persisted` key at all
    }
    webhook_main._commit_and_push(
        tenant_root=tmp_path, meeting_id="meet-abcdef12", ingested=[entry]
    )

    msg = captured[0]
    assert msg.splitlines()[0] == "[auto-ingest] account:ggl: meeting meet-abc"
    assert "- account:ggl: account_summary=1" in msg
    assert "transcript only" not in msg
