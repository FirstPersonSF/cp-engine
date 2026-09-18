"""Tenant clone -> commit -> push lifecycle for the webhook.

Split out of webhook/main.py (arch-phase-4, cp-engine #32).
Behavior-preserving: code moved verbatim; only import paths and
cross-module qualifications changed. Tests monkeypatch THIS module's
names (patching `main.<name>` re-exports has no effect on behavior).
"""

from __future__ import annotations

import logging
import os
import random
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
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

# Backoff between push attempts (#181). Retries used to fire back-to-back,
# which is sized for two webhooks colliding and not for a burst: tagging a
# batch of meetings in the dashboard fans out N concurrent deliveries, each
# cloning the tenant and racing to push. Observed 2026-08-12 11:21-11:31 —
# eleven webhooks, and two losers exhausted their attempts and dropped
# commits that exist in no branch.
#
# JITTER IS THE LOAD-BEARING PART, not the delay. Simultaneous losers back
# off by the same amount and re-collide in lockstep; a random component is
# what actually de-synchronises them. The delay is drawn from
# [0, base * 2**attempt) — "full jitter", which beats fixed-plus-noise
# because it also spreads the FIRST retry, where a burst's collisions are
# densest.
#
# Base is deliberately small: a push against an already-fetched remote is
# fast, and each attempt also pays a `pull --rebase`. Worst case added
# latency across 5 attempts is ~2.3s of sleep, well inside the sender's
# timeout — and a delivery that retries is one we'd otherwise LOSE.
_PUSH_BACKOFF_BASE_SEC = 0.15
_PUSH_MAX_ATTEMPTS = 5


def _push_backoff_delay(attempt: int) -> float:
    """Full-jitter backoff: a uniform draw from [0, base * 2**attempt).

    `attempt` is 1-based, so the first retry draws from [0, 0.3), the
    second [0, 0.6), and so on. Split out as a named function so tests can
    assert the SHAPE (bounded, spreads with attempt) without asserting an
    exact value, and so the sleep can be patched at one place.
    """
    return random.uniform(0, _PUSH_BACKOFF_BASE_SEC * (2 ** attempt))


def _push_with_retry(
    tenant_root: Path,
    *,
    target_branch: str,
    env: dict,
    max_attempts: int = _PUSH_MAX_ATTEMPTS,
    reapply: Callable[[], bool] | None = None,
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

    THE APPEND EXCEPTION (#290). The rebase path only helps when the
    concurrent writers touched DIFFERENT lines. Two appends to the tail of
    `improvements.md` — or two `updates_append` to one cp.md, both inserting
    directly under `**Updates:**` — conflict every time (`UU
    improvements.md`, verified), so for a tenant-wide file the abort-and-raise
    branch was the ONLY branch, and the loser's entry existed nowhere behind a
    502. For those routes the write is idempotent by construction (the append
    functions dedupe on content), so re-doing it on top of the winner's HEAD
    is exactly what the caller asked for and loses nothing.

    ``reapply``, when given, is that re-do: on a rebase conflict this helper
    aborts the rebase, resets the clone to origin's current tip (``fetch`` +
    ``reset --hard FETCH_HEAD``), calls ``reapply()`` — which must re-write
    the file AND re-commit, returning ``True`` if it committed — and pushes
    again, still bounded by ``max_attempts``. A ``False`` return means the
    winner already landed the identical entry (the dedupe fired), so there is
    nothing left to push and the helper returns. Callers that are NOT
    append-only must leave it ``None``: for a field replace, a conflict is two
    people disagreeing about the same prose, and that is not ours to settle.

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

        # Jittered backoff BEFORE the rebase (#181): under a burst the
        # rebase itself contends, so spreading here — not just before the
        # re-push — is what breaks the lockstep.
        delay = _push_backoff_delay(attempt)
        log.warning(
            "push rejected non-fast-forward (attempt %d/%d); "
            "backing off %.2fs, then rebasing and retrying",
            attempt, max_attempts, delay,
        )
        time.sleep(delay)
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
                "pull --rebase failed (%s); aborting rebase and %s",
                (rebase.stderr or "")[:240],
                "re-applying the append" if reapply else "giving up",
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
            if reapply is None:
                raise last_err

            # #290: the append-only recovery. Drop OUR commit entirely — the
            # rebase just proved it cannot be replayed — and rebuild it on
            # the winner's tip. `reset --hard FETCH_HEAD` rather than
            # `origin/<branch>`: after a CP_TENANT_BRANCH `branch -M` the
            # tracking ref may not exist, FETCH_HEAD always does.
            if not _reset_to_origin_tip(tenant_root, target_branch, env):
                raise last_err
            if not reapply():
                # The dedupe fired: the winner's commit already carries this
                # exact entry, so the caller's content IS on origin. Pushing
                # nothing is success here, not a silent drop.
                log.info(
                    "append already present at origin after conflict; "
                    "nothing left to push (attempt %d/%d)",
                    attempt, max_attempts,
                )
                return


def _reset_to_origin_tip(tenant_root: Path, target_branch: str, env: dict) -> bool:
    """``git fetch origin <branch>`` + ``git reset --hard FETCH_HEAD``.

    The re-apply half of #290 needs a tree that is EXACTLY origin's tip, with
    our un-replayable commit gone. Returns False (after logging) rather than
    raising so the caller can surface the ORIGINAL push error — the thing an
    operator can act on — instead of a secondary one from the recovery.
    """
    for cmd in (
        ["git", "fetch", "origin", target_branch],
        ["git", "reset", "--hard", "FETCH_HEAD"],
    ):
        r = subprocess.run(
            cmd, cwd=tenant_root, env=env, capture_output=True, text=True
        )
        if r.returncode != 0:
            log.warning(
                "%s failed during append re-apply (rc=%d): %s",
                " ".join(cmd), r.returncode, (r.stderr or "")[:200],
            )
            return False
    return True


def _commit_with_message_and_push(
    tenant_root: Path,
    message: str,
    *,
    reapply: Callable[[], bool] | None = None,
) -> str | None:
    """Stage all, commit with `message`, branch-rename, push, return HEAD SHA.

    Returns **None when the working tree was already clean** — nothing was
    committed and nothing pushed.

    ``reapply`` (#290) is for APPEND-ONLY callers: a zero-argument callable
    that re-reads the file from disk, re-applies the append and re-writes it,
    returning whether the file changed. When the push loses a race AND the
    rebase conflicts, the helper resets the clone to origin's tip, calls it,
    and — if it changed anything — re-stages and re-commits under the SAME
    ``message`` before pushing again. The route keeps authoring the write;
    this tail only owns the git mechanics, as it does on the happy path. Leave
    it ``None`` for anything that replaces content rather than appending it.

    The shared mechanical tail used by both `_commit_and_push` (auto-ingest)
    and `_commit_and_push_promote` (spine-promote). Each caller builds only its
    own commit message and delegates the `git add -A` / commit / CP_TENANT_BRANCH
    rename / `_push_with_retry` / `git rev-parse HEAD` sequence here.

    THE EMPTY-TREE GUARD (#237). `git commit` fails with "nothing to commit"
    when a handler wrote nothing — a snooze whose bullet was already flipped by
    an earlier delivery, a plan whose every verb was a no-op. `check=True` then
    turns a benign no-op into a 500, after the caller has already reported
    `files_written`. `_commit_clickup_close` has carried this guard since the
    original report; it was added to that ONE path while four other helpers
    kept the bug. Guarding the shared tail fixes three of them at once
    (`_commit_and_push`, `_commit_and_push_promote`, and every direct caller:
    sessions, project-state, email).

    Callers must treat `None` as success-with-nothing-to-do, not as failure.
    """
    env = _ssh_env()

    # Short-circuit BEFORE `git add`, so a clean tree costs one cheap status
    # call rather than a staged-then-failed commit.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tenant_root,
        check=True,
        capture_output=True,
        text=True,
    )
    if not status.stdout.strip():
        log.info("nothing to commit (clean tree); skipping: %s", message.splitlines()[0])
        return None

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
    recommit: Callable[[], bool] | None = None
    if reapply is not None:

        def recommit() -> bool:
            # Same message, same identity: the recovered commit should be
            # indistinguishable from the one that would have landed had the
            # race gone the other way.
            if not reapply():
                return False
            subprocess.run(["git", "add", "-A"], cwd=tenant_root, check=True)
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=tenant_root,
                check=True,
                env=env,
            )
            return True

    _push_with_retry(
        tenant_root, target_branch=target_branch, env=env, reapply=recommit
    )

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
) -> str | None:
    """Stage + commit + push. Returns the new HEAD SHA, or None on a clean
    tree — see `_commit_with_message_and_push` (#237)."""
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
) -> str | None:
    """Stage + commit + push a spine-promote markdown write. Returns HEAD SHA,
    or None on a clean tree — see `_commit_with_message_and_push` (#237).

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
