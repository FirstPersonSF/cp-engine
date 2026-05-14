"""cp-engine-webhook — Phase C.2

FastAPI service that fathom-meeting-sync calls when a high-confidence
project-status meeting arrives. Generates an ingest plan via Claude,
applies it to a fresh clone of the cp tenant, commits + pushes.

Deployment model: Railway service co-located with fathom-meeting-sync
under the same Railway project (shares env vars + metrics). SSH-based
git push using a deploy key on the cp tenant repo with write access.

Endpoints:
  POST /api/auto-ingest   — main entry; HMAC-signed by caller
  GET  /health            — liveness check; reports cp-engine version

Required env vars:
  WEBHOOK_HMAC_SECRET     — shared secret with fathom-meeting-sync
  ANTHROPIC_API_KEY       — for plan generation
  SUPABASE_URL            — for fetching transcripts
  SUPABASE_SERVICE_KEY    — service-role key (server-side only)
  CP_TENANT_REPO_URL      — git@github.com:FirstPersonSF/cp.git
  GIT_SSH_KEY             — private SSH key matching the deploy key
  GIT_AUTHOR_NAME         — "cp-engine-webhook"
  GIT_AUTHOR_EMAIL        — "webhook@firstperson.is" or similar
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

import cp_engine
from cp_engine.config import TenantConfig
from cp_engine.ingest import IngestPlanError, execute_plan
from cp_engine.plan_from_transcript import (
    PlanGenerationError,
    generate_plan,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("cp-engine-webhook")

app = FastAPI(title="cp-engine-webhook", version=cp_engine.__version__)


@app.get("/health")
def health() -> dict:
    """Liveness probe. Reports the cp-engine version we're running against."""
    return {
        "status": "healthy",
        "cp_engine_version": cp_engine.__version__,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


@app.post("/api/auto-ingest")
async def auto_ingest(request: Request) -> dict:
    """Generate + apply an ingest plan for one meeting against one or more projects.

    Request body (JSON):
        {
          "meeting_id": "<uuid from fathom_meetings.id>",
          "project_codes": ["ggl-5168", "ggl-5136"],
          "transcript_text": "<full transcript>"  # optional; fetched from
                                                    # Supabase if absent
        }

    Headers:
        X-Webhook-Signature: hex(hmac_sha256(body, WEBHOOK_HMAC_SECRET))

    Response:
        { "ingested": [{"code": ..., "files_written": [...], "errors": [...]}],
          "commit_sha": "...", "skipped_no_op": false }
    """
    raw_body = await request.body()
    _verify_signature(raw_body, request.headers.get("x-webhook-signature", ""))

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc

    meeting_id = payload.get("meeting_id")
    project_codes = payload.get("project_codes") or []
    transcript_text = payload.get("transcript_text")
    if not meeting_id or not project_codes:
        raise HTTPException(
            status_code=400, detail="meeting_id and project_codes are required"
        )

    if transcript_text is None:
        transcript_text = _fetch_transcript(meeting_id)

    log.info(
        "auto-ingest start: meeting=%s projects=%s", meeting_id, project_codes
    )

    with _cloned_tenant() as tenant_root:
        config = _load_tenant_config(tenant_root)
        transcript_path = _stage_transcript(tenant_root, meeting_id, transcript_text)

        # Per-project ingest + per-project commit. A multi-project
        # auto-ingest call produces N commits (one per project that
        # actually wrote files), not one combined commit. This makes
        # the git log readable (each commit's diff scopes to one
        # project's sprint file) and lets revert target one project
        # without disturbing the others.
        ingested: list[dict] = []
        commits: list[str] = []
        for code in project_codes:
            entry = _ingest_one_project(
                config=config,
                code=code,
                transcript_path=transcript_path,
            )
            ingested.append(entry)
            if entry["files_written"]:
                commit_sha = _commit_and_push(
                    tenant_root=tenant_root,
                    meeting_id=meeting_id,
                    ingested=[entry],
                )
                entry["commit_sha"] = commit_sha
                commits.append(commit_sha)
                log.info(
                    "auto-ingest commit: meeting=%s project=%s commit=%s",
                    meeting_id, code, commit_sha,
                )

        if not commits:
            log.info("auto-ingest no-op: no files changed for meeting=%s", meeting_id)
            response = {"ingested": ingested, "commit_sha": None, "skipped_no_op": True}
            _log_run_to_supabase(
                meeting_id=meeting_id,
                project_codes=project_codes,
                status=_status_from_ingested(ingested, anything_wrote=False),
                ingested=ingested,
                commit_sha=None,
            )
            return response

        # observability: log one row per multi-project auto-ingest run
        # against the last commit SHA. Per-project commit shas are
        # surfaced in the `ingested` payload field.
        last_commit = commits[-1]
        log.info(
            "auto-ingest done: meeting=%s commits=%d last=%s",
            meeting_id, len(commits), last_commit,
        )
        response = {
            "ingested": ingested,
            "commit_sha": last_commit,
            "commit_shas": commits,
            "skipped_no_op": False,
        }
        _log_run_to_supabase(
            meeting_id=meeting_id,
            project_codes=project_codes,
            status="success",
            ingested=ingested,
            commit_sha=last_commit,
        )
        return response


# ─────────────────────────────────────────────────────────────
#  Signature verification
# ─────────────────────────────────────────────────────────────


def _verify_signature(raw_body: bytes, provided: str) -> None:
    secret = os.environ.get("WEBHOOK_HMAC_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="WEBHOOK_HMAC_SECRET not configured")
    if not provided:
        raise HTTPException(status_code=401, detail="missing X-Webhook-Signature header")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="invalid signature")


# ─────────────────────────────────────────────────────────────
#  Tenant repo lifecycle (clone-on-each-request)
# ─────────────────────────────────────────────────────────────


@contextmanager
def _cloned_tenant():
    """Clone cp tenant into temp dir; yield path; always clean up."""
    repo_url = os.environ.get("CP_TENANT_REPO_URL")
    if not repo_url:
        raise HTTPException(status_code=500, detail="CP_TENANT_REPO_URL not configured")

    tmp = Path(tempfile.mkdtemp(prefix="cp-webhook-"))
    try:
        env = _ssh_env()
        subprocess.run(
            ["git", "clone", "--depth=10", repo_url, str(tmp / "cp")],
            check=True,
            env=env,
            capture_output=True,
        )
        # Configure committer once per clone so every commit picks it up.
        subprocess.run(
            ["git", "config", "user.name", os.environ.get("GIT_AUTHOR_NAME", "cp-engine-webhook")],
            cwd=tmp / "cp",
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", os.environ.get("GIT_AUTHOR_EMAIL", "webhook@firstperson.is")],
            cwd=tmp / "cp",
            check=True,
        )
        yield tmp / "cp"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _ssh_env() -> dict:
    """Build a subprocess env that uses GIT_SSH_KEY for the clone/push."""
    env = os.environ.copy()
    key_material = os.environ.get("GIT_SSH_KEY")
    if not key_material:
        return env

    # Materialize the key once per request to a tempfile that we'll point
    # GIT_SSH_COMMAND at. Container has tmpfs at /tmp, fine for ephemeral keys.
    key_path = Path(tempfile.mkdtemp(prefix="cp-webhook-key-")) / "id_ed25519"
    key_path.write_text(key_material if key_material.endswith("\n") else key_material + "\n")
    key_path.chmod(0o600)
    env["GIT_SSH_COMMAND"] = (
        f"ssh -i {key_path} -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"
    )
    return env


# ─────────────────────────────────────────────────────────────
#  Per-project ingest
# ─────────────────────────────────────────────────────────────


def _ingest_one_project(
    *, config: TenantConfig, code: str, transcript_path: Path
) -> dict:
    """Generate plan + execute for a single project. Returns a summary dict."""
    entry = {
        "code": code,
        "plan_summary": None,
        "files_written": [],
        "skipped_duplicate": 0,
        "errors": [],
    }
    try:
        gen = generate_plan(
            config=config,
            project_code=code,
            transcript_path=transcript_path,
        )
    except PlanGenerationError as exc:
        entry["errors"].append(f"plan generation failed: {exc}")
        log.warning("plan generation failed for %s: %s", code, exc)
        return entry

    # Summarize what's in the plan for logging + observability.
    projects = gen.plan.get("projects") or {}
    project_block = projects.get(code) or {}
    entry["plan_summary"] = {verb: len(items) for verb, items in project_block.items()}

    try:
        exec_result = execute_plan(
            gen.plan, tenant_root=config.root, today=datetime.now().date()
        )
    except IngestPlanError as exc:
        entry["errors"].append(f"plan execution failed: {exc}")
        log.warning("plan execution failed for %s: %s", code, exc)
        return entry

    entry["files_written"] = [str(p) for p in exec_result.files_written]
    entry["skipped_duplicate"] = exec_result.skipped_duplicate
    entry["errors"].extend(exec_result.errors)
    return entry


# ─────────────────────────────────────────────────────────────
#  Transcript handling
# ─────────────────────────────────────────────────────────────


def _normalize_transcript(transcript_field) -> str:
    """Normalize Fathom's transcript field into a single readable string.

    Four shapes show up in production:
      1. None / empty → ""
      2. plain string → returned as-is (older webhook payload format)
      3. JSONB object with "text"/"plain_text" key → that value
      4. JSONB list of segments `[{text, speaker:{display_name}, timestamp}, ...]`
         → rendered as `<ts> - <speaker>\\n  <text>` blocks matching the
         staged-transcript file format that plan_from_transcript was
         tuned against.

    Shape 4 is the standard across all current Fathom-delivered meetings;
    the earlier `str(...)` fallback turned a Python list into its repr
    (which silently passed validation but produced garbage prompts).
    """
    if transcript_field is None:
        return ""
    if isinstance(transcript_field, str):
        return transcript_field
    if isinstance(transcript_field, list):
        return _segments_to_text(transcript_field)
    if isinstance(transcript_field, dict):
        if transcript_field.get("text"):
            return transcript_field["text"]
        if transcript_field.get("plain_text"):
            return transcript_field["plain_text"]
        segments = transcript_field.get("segments")
        if isinstance(segments, list):
            return _segments_to_text(segments)
    return ""


def _segments_to_text(segments: list) -> str:
    """Render Fathom segments as `<ts> - <speaker>\\n  <text>` blocks."""
    lines: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        speaker_obj = seg.get("speaker") or {}
        speaker = (
            (speaker_obj.get("display_name") if isinstance(speaker_obj, dict) else None)
            or seg.get("display_name")
            or seg.get("speaker_name")
            or "Unknown"
        )
        ts = seg.get("timestamp") or seg.get("ts") or ""
        header = f"{ts} - {speaker}" if ts else speaker
        lines.append(f"{header}\n  {text}")
    return "\n\n".join(lines)


def _fetch_transcript(meeting_id: str) -> str:
    """Pull a transcript from Supabase fathom_meetings table."""
    try:
        from supabase import create_client
    except ImportError as exc:
        raise HTTPException(
            status_code=500, detail="supabase package not installed"
        ) from exc

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise HTTPException(
            status_code=500, detail="SUPABASE_URL or SUPABASE_SERVICE_KEY not set"
        )

    client = create_client(url, key)
    resp = (
        client.table("fathom_meetings")
        .select("id, title, meeting_date, transcript, participants")
        .eq("id", meeting_id)
        .single()
        .execute()
    )
    if not resp.data:
        raise HTTPException(status_code=404, detail=f"meeting {meeting_id} not found")

    text = _normalize_transcript(resp.data.get("transcript"))
    if not text:
        raise HTTPException(
            status_code=422, detail=f"meeting {meeting_id} has no transcript text"
        )

    header = (
        f"# {resp.data.get('title', 'Meeting')}\n"
        f"# Date: {resp.data.get('meeting_date', 'unknown')}\n"
        f"# Meeting ID: {meeting_id}\n\n"
    )
    return header + text


def _stage_transcript(tenant_root: Path, meeting_id: str, text: str) -> Path:
    """Write transcript to tenant's transcripts/incoming/ for the prompt + audit."""
    incoming = tenant_root / "transcripts" / "incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    filename = f"auto-ingest-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{meeting_id[:8]}.txt"
    path = incoming / filename
    path.write_text(text, encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────
#  Commit + push
# ─────────────────────────────────────────────────────────────


def _commit_and_push(
    *, tenant_root: Path, meeting_id: str, ingested: list[dict]
) -> str:
    """Stage + commit + push. Returns the new HEAD SHA."""
    env = _ssh_env()

    subprocess.run(["git", "add", "-A"], cwd=tenant_root, check=True)
    codes = ", ".join(e["code"] for e in ingested if e["files_written"])
    summary_lines = []
    for e in ingested:
        if not e["files_written"]:
            continue
        verbs = ", ".join(f"{k}={v}" for k, v in (e["plan_summary"] or {}).items())
        summary_lines.append(f"- {e['code']}: {verbs}")
    body = "\n".join(summary_lines)

    message = (
        f"[auto-ingest] {codes}: meeting {meeting_id[:8]}\n\n"
        f"{body}\n\n"
        f"Generated by cp-engine-webhook v{cp_engine.__version__}.\n"
    )

    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=tenant_root,
        check=True,
        env=env,
    )
    # CP_TENANT_BRANCH lets local tests push to a throwaway branch rather
    # than main. Production deploys leave it unset so the default applies.
    target_branch = os.environ.get("CP_TENANT_BRANCH", "main")
    # The clone lands the remote default (main) into local main; if we're
    # targeting a different branch, rename HEAD first.
    if target_branch != "main":
        subprocess.run(
            ["git", "branch", "-M", target_branch],
            cwd=tenant_root,
            check=True,
        )
    subprocess.run(
        ["git", "push", "origin", target_branch],
        cwd=tenant_root,
        check=True,
        env=env,
    )

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tenant_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


# ─────────────────────────────────────────────────────────────
#  Config bootstrap
# ─────────────────────────────────────────────────────────────


def _load_tenant_config(tenant_root: Path) -> TenantConfig:
    """Load .cp-engine.toml from the freshly-cloned tenant root.

    The webhook deployment doesn't get .cp-engine.local.toml — that's
    a per-machine file with absolute paths to project clones. cp_engine.config.load()
    already treats local as optional when the committed config declares
    no [[projects]] (the CI-runner case), which is true for the cp tenant
    (MC-2-backed, no committed [[projects]]). So plain load() just works.
    """
    from cp_engine import config as cfg_mod

    return cfg_mod.load(tenant_root=tenant_root)


# ─────────────────────────────────────────────────────────────
#  Observability (Phase C.4)
# ─────────────────────────────────────────────────────────────


def _status_from_ingested(ingested: list[dict], *, anything_wrote: bool) -> str:
    """Derive the auto_ingest_runs.status enum value.

    - 'failed': at least one project errored out
    - 'skipped_no_op': nothing wrote AND nothing errored (clean no-op)
    - 'success': we wrote files (used in the commit-and-push branch)
    """
    if any(entry.get("errors") for entry in ingested):
        return "failed"
    if not anything_wrote:
        return "skipped_no_op"
    return "success"


def _log_run_to_supabase(
    *,
    meeting_id: str,
    project_codes: list[str],
    status: str,
    ingested: list[dict],
    commit_sha: str | None,
) -> None:
    """Insert one row into auto_ingest_runs. Never raises.

    Observability is best-effort: a failure to log must not break the
    primary auto-ingest contract with fathom-meeting-sync. We log + swallow.
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        log.warning("auto_ingest_runs insert skipped: Supabase env not set")
        return

    plan_summary = {
        entry["code"]: entry.get("plan_summary") or {}
        for entry in ingested
    }
    errors_flat: list[str] = []
    for entry in ingested:
        for err in entry.get("errors") or []:
            errors_flat.append(f"{entry['code']}: {err}")

    try:
        from supabase import create_client

        client = create_client(url, key)
        client.table("auto_ingest_runs").insert({
            "meeting_id": meeting_id,
            "project_codes": project_codes,
            "status": status,
            "plan_summary": plan_summary,
            "commit_sha": commit_sha,
            "errors": errors_flat or None,
        }).execute()
    except Exception as exc:  # noqa: BLE001 — observability must never throw
        log.warning("auto_ingest_runs insert failed for %s: %s", meeting_id, exc)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
