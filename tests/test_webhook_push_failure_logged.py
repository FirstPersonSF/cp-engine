"""A push failure must land a `failed` row in auto_ingest_runs.

Every `_log_run_to_supabase` call used to sit on the success path, so an
exception escaping the clone block wrote NO row at all — neither success
nor failed. The runs table therefore showed a clean record while commits
sat unpublished in a discarded clone, and Sentry was the only signal.

Observed 2026-08-12 11:21–11:31: eleven auto-ingest webhooks fired in a
burst, each cloning the tenant and racing to push. Losers exhausted
`_push_with_retry` and raised CalledProcessError 128. Two runs
(meetings e6ecea2b, 1559b663) recorded commit shas 4ff80f11 / 489786fe
that exist in no branch of the tenant. The failures table's most recent
entry at the time was from June 7.

The contract under test:
  1. a failure writes a row with status="failed"
  2. the exception still propagates (fathom-meeting-sync must retry)
  3. the row carries the PARTIAL published set — pushes happen inside the
     per-project loop, so the last successful sha is the boundary between
     what landed and what didn't
"""
from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import git_ops
import pipeline


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


def _wire_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    tenant: Path,
    *,
    codes: list[str],
    commit_behavior,
) -> list[dict]:
    """Fake the pipeline at its seams; return the captured run-log rows.

    `commit_behavior` is called as (code) -> sha and may raise to simulate
    a push failure on a chosen project.
    """
    tenant.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def fake_cloned_tenant():
        yield tenant

    monkeypatch.setattr(git_ops, "_cloned_tenant", fake_cloned_tenant)
    monkeypatch.setattr(
        pipeline, "_load_tenant_config", lambda root: _config_for(tenant)
    )
    monkeypatch.setattr(
        pipeline, "_stage_transcript", lambda root, mid, text: tenant / "staged.txt"
    )
    monkeypatch.setattr(
        pipeline,
        "_fetch_meeting",
        lambda mid: {
            "id": mid,
            "title": "Burst",
            "meeting_date": "2026-08-12",
            "action_items": [],
        },
    )
    monkeypatch.setattr(
        pipeline,
        "_ingest_one_project",
        lambda *, config, code, transcript_path, action_items, meeting_id, meeting, roster=None: {
            "code": code,
            "files_written": [f"sprints/2026-W33/{code}.md"],
            "plan_summary": {"outbound": 1},
            "errors": [],
            "status": "ok",
        },
    )

    def fake_commit(*, tenant_root, meeting_id, ingested):
        return commit_behavior(ingested[0]["code"])

    monkeypatch.setattr(git_ops, "_commit_and_push", fake_commit)

    # Downstream best-effort blocks — not under test.
    monkeypatch.setattr(pipeline, "_persist_transcript", lambda *a, **k: None)
    monkeypatch.setattr(
        pipeline, "propose_commitments", lambda mid, c, roster=None: {}
    )
    monkeypatch.setattr(pipeline, "_generate_meeting_artifacts", lambda **kw: {})
    monkeypatch.setattr(pipeline, "_create_supabase_client", lambda: None)

    rows: list[dict] = []
    monkeypatch.setattr(
        pipeline, "_log_run_to_supabase", lambda **kw: rows.append(kw)
    )
    return rows


def test_push_failure_writes_a_failed_row_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real 2026-08-12 shape: push raises CalledProcessError 128."""
    boom = subprocess.CalledProcessError(
        128, ["git", "push", "origin", "main"], stderr="fatal: ...non-fast-forward"
    )

    def commit(code):
        raise boom

    rows = _wire_pipeline(
        monkeypatch, tmp_path / "tenant", codes=["ibx-5192"], commit_behavior=commit
    )

    # Must still raise — the caller 500s so fathom-meeting-sync retries.
    with pytest.raises(subprocess.CalledProcessError):
        pipeline._perform_auto_ingest(
            meeting_id="e6ecea2b", project_codes=["ibx-5192"], transcript_text="body"
        )

    assert len(rows) == 1, "exactly one run row, on the failure path"
    assert rows[0]["status"] == "failed"
    assert rows[0]["meeting_id"] == "e6ecea2b"
    # The error text must name the failure, or the row is undiagnosable.
    assert any("CalledProcessError" in e for e in rows[0]["top_level_errors"])


def test_failed_row_records_the_partial_push_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pushes happen per-project, so a mid-loop failure leaves earlier
    projects PUBLISHED. The row's commit_sha is that boundary — recording
    None here would misreport a partial run as a total loss."""
    pushed: list[str] = []

    def commit(code):
        if code == "ggl-5197":  # the third project fails
            raise subprocess.CalledProcessError(128, ["git", "push"])
        sha = f"sha-{code}"
        pushed.append(sha)
        return sha

    rows = _wire_pipeline(
        monkeypatch,
        tmp_path / "tenant",
        codes=["ibx-5192", "slt-5195", "ggl-5197"],
        commit_behavior=commit,
    )

    with pytest.raises(subprocess.CalledProcessError):
        pipeline._perform_auto_ingest(
            meeting_id="1559b663",
            project_codes=["ibx-5192", "slt-5195", "ggl-5197"],
            transcript_text="body",
        )

    assert rows[0]["status"] == "failed"
    assert rows[0]["commit_sha"] == "sha-slt-5195", "last sha that reached origin"
    assert pushed == ["sha-ibx-5192", "sha-slt-5195"]
    # The partial per-project detail survives into the row.
    assert [e["code"] for e in rows[0]["ingested"]] == [
        "ibx-5192",
        "slt-5195",
        "ggl-5197",
    ]


def test_success_path_still_logs_exactly_one_success_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new handler must not double-log or shadow the success path."""
    rows = _wire_pipeline(
        monkeypatch,
        tmp_path / "tenant",
        codes=["ibx-5192"],
        commit_behavior=lambda code: f"sha-{code}",
    )

    result = pipeline._perform_auto_ingest(
        meeting_id="da7128ed", project_codes=["ibx-5192"], transcript_text="body"
    )

    assert result["commit_sha"] == "sha-ibx-5192"
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
