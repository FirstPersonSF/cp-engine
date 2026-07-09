"""Tenant clone -> commit -> push lifecycle for the webhook.

Split out of webhook/main.py (arch-phase-4, cp-engine #32).
Behavior-preserving: code moved verbatim; only import paths and
cross-module qualifications changed. Tests monkeypatch THIS module's
names (patching `main.<name>` re-exports has no effect on behavior).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

import observability
from fastapi import HTTPException

import cp_engine

log = logging.getLogger("cp-engine-webhook")


@contextmanager
def _cloned_tenant(sparse_paths: list[str] | None = None):
    """Clone cp tenant into temp dir; yield path; always clean up.

    ``sparse_paths``: when given, the clone is a partial + sparse checkout —
    ``--depth=10 --filter=blob:none --sparse`` followed by ``git
    sparse-checkout set <paths...>`` (cone mode, git's default). Only the
    named top-level directories (recursively) plus all root-level files
    (cone mode includes those automatically — .cp-engine.toml, master-cp.md,
    …) are materialized; blobs outside the cone are never fetched. Commits
    and pushes from such a clone work normally for changes touching
    checked-out paths (partial-clone push has been solid since git ≥2.30;
    the deploy image and dev machines run far newer). ``git add -A`` in a
    sparse checkout only stages visible files — sparse-excluded index
    entries carry the skip-worktree bit and pass through commits untouched.
    """
    repo_url = os.environ.get("CP_TENANT_REPO_URL")
    if not repo_url:
        raise HTTPException(status_code=500, detail="CP_TENANT_REPO_URL not configured")

    tmp = Path(tempfile.mkdtemp(prefix="cp-webhook-"))
    try:
        env = _ssh_env()
        clone_cmd = ["git", "clone", "--depth=10"]
        if sparse_paths:
            clone_cmd += ["--filter=blob:none", "--sparse"]
        clone_cmd += [repo_url, str(tmp / "cp")]
        subprocess.run(
            clone_cmd,
            check=True,
            env=env,
            capture_output=True,
        )
        if sparse_paths:
            subprocess.run(
                ["git", "sparse-checkout", "set", *sparse_paths],
                cwd=tmp / "cp",
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


# Markers that git emits on a non-fast-forward push reject (the case
# we can recover from with a pull --rebase). Anything else (auth, hook
# rejection, network) is raised straight through — we don't want to
# loop on those.
#
# NB: a bare "rejected" marker was previously here too but matched every
# kind of push reject (auth failures, pre-receive hooks, branch
# protection), wasting two pull-rebase round-trips on each before
# bottoming out. The two markers below are what git actually emits for
# the non-ff race condition; auth/hook rejects raise immediately.
_NON_FAST_FORWARD_MARKERS = (
    "non-fast-forward",
    "(non-fast-forward)",
    "fetch first",
)


def _push_with_retry(
    tenant_root: Path,
    *,
    target_branch: str,
    env: dict,
    max_attempts: int = 3,
) -> None:
    """``git push origin <branch>`` with rebase-on-reject recovery.

    Concurrent auto-ingest webhooks each clone independently and race on
    push. The loser of the race gets a non-fast-forward rejection. This
    helper recovers by ``git pull --rebase origin <branch>`` and trying
    again. After ``max_attempts`` consecutive failures, the last error
    is re-raised so the request 500s and Fathom can retry the whole
    pipeline cleanly (rather than wedging mid-rebase).

    Important: if the ``pull --rebase`` itself fails (e.g., true content
    conflict on the same line), we run ``git rebase --abort`` to leave
    the working tree on a clean detached state and then raise. We do
    NOT try to auto-resolve — that would silently overwrite one webhook
    call's bullet with another's.

    Modelled on src/cp_engine/capture_session.py:_push_with_retry but
    parameterized for the webhook's per-request SSH env + named-branch
    push.
    """
    last_err: subprocess.CalledProcessError | None = None
    for attempt in range(1, max_attempts + 1):
        push = subprocess.run(
            ["git", "push", "origin", target_branch],
            cwd=tenant_root,
            env=env,
            capture_output=True,
            text=True,
        )
        if push.returncode == 0:
            if attempt > 1:
                log.info(
                    "push succeeded on attempt %d (after rebase)", attempt
                )
            return

        last_err = subprocess.CalledProcessError(
            push.returncode, push.args, output=push.stdout, stderr=push.stderr
        )

        stderr_lc = (push.stderr or "").lower()
        is_non_ff = any(m in stderr_lc for m in _NON_FAST_FORWARD_MARKERS)
        if not is_non_ff or attempt == max_attempts:
            # Either a non-recoverable class of failure (auth, hook
            # reject, network) or we've exhausted retries. Surface it.
            if not is_non_ff:
                log.warning(
                    "push failed with non-recoverable error: %s",
                    (push.stderr or "")[:240],
                )
            raise last_err

        log.warning(
            "push rejected non-fast-forward (attempt %d/%d); "
            "rebasing and retrying",
            attempt, max_attempts,
        )
        rebase = subprocess.run(
            ["git", "pull", "--rebase", "origin", target_branch],
            cwd=tenant_root,
            env=env,
            capture_output=True,
            text=True,
        )
        if rebase.returncode != 0:
            # Don't leave the tenant in a mid-rebase state — abort so
            # the next clone (whether this same request or a Fathom
            # retry) starts from a clean tree. Then surface the
            # original push failure: that's the operationally
            # actionable signal.
            log.warning(
                "pull --rebase failed (%s); aborting rebase and giving up",
                (rebase.stderr or "")[:240],
            )
            abort = subprocess.run(
                ["git", "rebase", "--abort"],
                cwd=tenant_root,
                env=env,
                capture_output=True,
                text=True,
            )
            if abort.returncode != 0:
                # Don't swallow this — a wedged worktree is the kind of
                # thing the operator needs to see in logs (next clone
                # may inherit a half-rebase state).
                log.warning(
                    "git rebase --abort failed (rc=%d): %s",
                    abort.returncode,
                    (abort.stderr or "")[:200],
                )
            raise last_err


def _commit_with_message_and_push(tenant_root: Path, message: str) -> str:
    """Stage all, commit with `message`, branch-rename, push, return HEAD SHA.

    The shared mechanical tail used by both `_commit_and_push` (auto-ingest)
    and `_commit_and_push_promote` (spine-promote). Each caller builds only its
    own commit message and delegates the `git add -A` / commit / CP_TENANT_BRANCH
    rename / `_push_with_retry` / `git rev-parse HEAD` sequence here.
    """
    env = _ssh_env()

    subprocess.run(["git", "add", "-A"], cwd=tenant_root, check=True)
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
    _push_with_retry(tenant_root, target_branch=target_branch, env=env)

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tenant_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit_and_push(
    *, tenant_root: Path, meeting_id: str, ingested: list[dict]
) -> str:
    """Stage + commit + push. Returns the new HEAD SHA."""
    # Subject attribution: prefer the codes that actually wrote files. If
    # NONE did (a transcript-only commit — persisted a transcript but wrote
    # no bullets), fall back to ALL entries' codes so the project is still
    # named in the subject rather than leaving a blank `[auto-ingest] :`.
    codes = ", ".join(e["code"] for e in ingested if e["files_written"]) or ", ".join(
        e["code"] for e in ingested
    )
    summary_lines = []
    for e in ingested:
        if e["files_written"]:
            verbs = ", ".join(
                f"{k}={v}" for k, v in (e["plan_summary"] or {}).items()
            )
            summary_lines.append(f"- {e['code']}: {verbs}")
        elif e.get("transcript_persisted"):
            # No bullets but a transcript landed. `.get` keeps this safe for
            # the account/sprint-planning/slack callers whose entries never
            # set `transcript_persisted` (missing key → falsy → skipped).
            summary_lines.append(f"- {e['code']}: transcript only")
    body = "\n".join(summary_lines)

    message = (
        f"[auto-ingest] {codes}: meeting {meeting_id[:8]}\n\n"
        f"{body}\n\n"
        f"Generated by cp-engine-webhook v{cp_engine.__version__}.\n"
        f"{_correlation_trailer()}"
    )

    return _commit_with_message_and_push(tenant_root, message)


def _correlation_trailer() -> str:
    """`Correlation-Id: <cid>` trailer line for pushed commit messages.

    Lets `git log --grep 'Correlation-Id: <cid>'` on the tenant repo find
    the commits one webhook delivery produced. Empty string outside a
    request context so non-request callers don't grow a `Correlation-Id: -`.
    """
    cid = observability.current_correlation_id()
    return f"Correlation-Id: {cid}\n" if cid else ""


def _commit_and_push_promote(
    *, tenant_root: Path, project_code: str, version_label: str, rel_path: str
) -> str:
    """Stage + commit + push a spine-promote markdown write. Returns HEAD SHA.

    Sibling of `_commit_and_push` (whose commit message is auto-ingest-shaped).
    A promote writes exactly one substance file; we stage everything (`git add
    -A`, in case promote_card also created a parent dir) and commit with a
    promote-shaped message. Reuses the shared `_commit_with_message_and_push`
    tail so it honors the CP_TENANT_BRANCH override exactly like
    `_commit_and_push` and tests can push to a throwaway branch.
    """
    message = (
        f"[spine-promote] {project_code}: {rel_path} {version_label}\n\n"
        f"Generated by cp-engine-webhook v{cp_engine.__version__}.\n"
        f"{_correlation_trailer()}"
    )
    return _commit_with_message_and_push(tenant_root, message)


def _commit_meeting_artifacts(
    *, tenant_root: Path, meeting_id: str, artifact_paths: list[Path]
) -> str | None:
    """Commit + push the per-meeting artifact files.

    Separate from _commit_and_push because a meeting can produce an
    artifact even when it wrote no sprint-file bullets (so the per-project
    commit loop would never fire). Stages only the artifact paths.

    Best-effort: returns None on failure rather than raising — a failed
    artifact commit must not break the auto-ingest contract.
    """
    if not artifact_paths:
        return None
    try:
        env = _ssh_env()
        rels = [str(p.relative_to(tenant_root)) for p in artifact_paths]
        subprocess.run(["git", "add", *rels], cwd=tenant_root, check=True)

        # If the sprint-file commit already swept these in via `git add
        # -A`, there's nothing staged here — bail quietly.
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=tenant_root, env=env
        )
        if staged.returncode == 0:
            return None

        message = (
            f"[auto-ingest] meeting artifacts: meeting {meeting_id[:8]}\n\n"
            f"Per-meeting synthesis + transcript for {len(rels)} file(s).\n"
            f"Generated by cp-engine-webhook v{cp_engine.__version__}.\n"
            f"{_correlation_trailer()}"
        )
        subprocess.run(
            ["git", "commit", "-m", message], cwd=tenant_root, check=True, env=env
        )
        target_branch = os.environ.get("CP_TENANT_BRANCH", "main")
        _push_with_retry(tenant_root, target_branch=target_branch, env=env)
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tenant_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning(
            "meeting-artifact: commit failed for meeting=%s: %s", meeting_id, exc
        )
        observability.capture(exc, area="meeting_artifact_commit")
        return None


def _commit_clickup_close(
    *, tenant_root: Path, code: str, cp_hash: str
) -> str | None:
    """Commit + push a ClickUp-close round-trip. Returns the new HEAD sha,
    or None if the working tree was clean (e.g., execute_plan already
    flipped the bullet on a previous webhook run)."""
    env = _ssh_env()

    # Short-circuit if execute_plan made no on-disk change. Without this,
    # `git commit` would fail with "nothing to commit" and 500 the request.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tenant_root,
        check=True,
        capture_output=True,
        text=True,
    )
    if not status.stdout.strip():
        log.info(
            "clickup-task-closed: no changes for code=%s hash=%s", code, cp_hash
        )
        return None

    subprocess.run(["git", "add", "-A"], cwd=tenant_root, check=True)
    message = (
        f"[clickup-close] {code}: hash {cp_hash}\n\n"
        f"Generated by cp-engine-webhook v{cp_engine.__version__}.\n"
        f"{_correlation_trailer()}"
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=tenant_root,
        check=True,
        env=env,
    )
    target_branch = os.environ.get("CP_TENANT_BRANCH", "main")
    if target_branch != "main":
        subprocess.run(
            ["git", "branch", "-M", target_branch], cwd=tenant_root, check=True
        )
    _push_with_retry(tenant_root, target_branch=target_branch, env=env)

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tenant_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
