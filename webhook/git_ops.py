"""Tenant clone -> commit -> push lifecycle for the webhook.

Split out of webhook/main.py (arch-phase-4, cp-engine #32).
Behavior-preserving: code moved verbatim; only import paths and
cross-module qualifications changed. Tests monkeypatch THIS module's
names (patching `main.<name>` re-exports has no effect on behavior).
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

import observability
from fastapi import HTTPException

import cp_engine

log = logging.getLogger("cp-engine-webhook")


# ──────────────────────────────────────────────────────────────────────
#  One writer at a time per tenant (architecture plan step 2)
# ──────────────────────────────────────────────────────────────────────
#
# THE RACE. Every route clones the tenant, edits, commits and pushes on its
# own. Two deliveries that overlap (a batch of meetings tagged at once, a
# Slack click during an ingest) each clone the SAME tip, edit the same sprint
# file, and race to push. The loser's `pull --rebase` succeeds only when the
# edits touched different lines; two bullets appended under one heading
# conflict, and the loser's request fails (or, for append routes, re-does its
# write via #290). Git kept it from being corrupt; it did not keep it from
# being lost to a 5xx.
#
# THE LOCK. `_cloned_tenant` holds a per-tenant lock across the whole
# clone → edit → commit → push, so in-process writers run one after another
# and each clones the previous one's pushed tip. Writers OUTSIDE this process
# (CI workflows, people, local cxp) still race, and that race is still
# rebase-or-fail in `_push_with_retry`.
#
# WHY NOT A PLAIN LOCK. Routes run both on the event-loop thread (sync code
# in `async def`) and in `asyncio.to_thread` workers; two routes also hold a
# clone open across an `await`. A coroutine waiting on a lock held by another
# coroutine of the SAME loop thread would block the loop forever (the holder
# can never resume). So: a thread that already holds the lock — which on the
# loop thread means "another coroutine of this loop holds it" — proceeds
# WITHOUT it (logged), falling back to the rebase-or-fail push. Every other
# wait is bounded by `CP_TENANT_LOCK_TIMEOUT_SEC` and ends in a loud 503.
#
# SCOPE. In-process only: correct for the single uvicorn process the
# Dockerfile starts. More than one replica or `--workers N` needs a lock
# outside the process (a Postgres advisory lock is the natural one).

_TENANT_LOCK_TIMEOUT_SEC = float(os.environ.get("CP_TENANT_LOCK_TIMEOUT_SEC", "600"))
_tenant_locks: dict[str, threading.Lock] = {}
_tenant_lock_holder: dict[str, int] = {}
_tenant_locks_guard = threading.Lock()


def _on_event_loop_thread() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@contextmanager
def _tenant_write_lock(repo_url: str):
    """Hold the per-tenant writer lock; yields True if held, False if this
    call had to proceed without it (see the note above)."""
    with _tenant_locks_guard:
        lock = _tenant_locks.setdefault(repo_url, threading.Lock())
    me = threading.get_ident()
    if _tenant_lock_holder.get(repo_url) == me:
        log.warning(
            "tenant write lock already held by this thread (%s); proceeding "
            "unlocked — push stays rebase-or-fail",
            "another coroutine on the event loop" if _on_event_loop_thread() else "re-entrant clone",
        )
        yield False
        return
    if not lock.acquire(timeout=_TENANT_LOCK_TIMEOUT_SEC):
        raise HTTPException(
            status_code=503,
            detail=(
                f"tenant write lock busy for {_TENANT_LOCK_TIMEOUT_SEC:.0f}s — "
                "another write to the tenant is still running; retry"
            ),
        )
    _tenant_lock_holder[repo_url] = me
    try:
        yield True
    finally:
        _tenant_lock_holder.pop(repo_url, None)
        lock.release()


@contextmanager
def _cloned_tenant(sparse_paths: list[str] | None = None):
    """Clone cp tenant into temp dir; yield path; always clean up.

    Holds the per-tenant writer lock for the whole clone → edit → commit →
    push (architecture plan step 2; see `_tenant_write_lock`).

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

    with _tenant_write_lock(repo_url):
        with _clone_into_tmp(repo_url, sparse_paths) as root:
            yield root


@contextmanager
def _clone_into_tmp(repo_url: str, sparse_paths: list[str] | None):
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


@contextmanager
def _read_only_clone(*, since_days: int = 3):
    """Lock-free, blob-less, sparse clone for READ-ONLY jobs (the cron health
    line). Yields the checkout path; always cleans up.

    Differs from `_cloned_tenant` on purpose:

    - no writer lock — nothing here commits, so it must not queue behind (or
      hold up) an auto-ingest push;
    - history reaches back ``since_days`` (`--shallow-since`), not 10 commits:
      the tenant takes ~35 auto-ingest commits a day, so the newest
      ``[cp-sync]`` commit (the Sync line's git fallback) is routinely deeper
      than ``--depth=10``. ``--filter=blob:none`` keeps that cheap — commits
      and trees only;
    - ``--sparse`` with no cone set materializes root files only, which is all
      the readers need (`.cp-engine.toml`, `master-cp.md`).

    A window with no commits at all makes ``--shallow-since`` fail; that falls
    back to ``--depth=1`` so the config still loads (and the Sync line then
    says it found no sync commit, rather than the job failing).
    """
    repo_url = os.environ.get("CP_TENANT_REPO_URL")
    if not repo_url:
        raise HTTPException(status_code=500, detail="CP_TENANT_REPO_URL not configured")
    tmp = Path(tempfile.mkdtemp(prefix="cp-webhook-ro-"))
    dest = tmp / "cp"
    try:
        env = _ssh_env()
        base = ["git", "clone", "--filter=blob:none", "--sparse", "--no-tags"]
        since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - since_days * 86400))
        try:
            subprocess.run([*base, f"--shallow-since={since}", repo_url, str(dest)],
                           check=True, env=env, capture_output=True)
        except subprocess.CalledProcessError as first:
            shutil.rmtree(dest, ignore_errors=True)
            out = subprocess.run([*base, "--depth=1", repo_url, str(dest)],
                                 env=env, capture_output=True, text=True)
            if out.returncode != 0:
                # git's own message (auth, network) — never the key itself.
                raise RuntimeError(
                    f"tenant clone failed: {out.stderr.strip()[-300:]}") from first
        yield dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# The identity the tenant workflows commit as (`git config user.name/email` in
# sync.yml and draft-summaries.yml). Jobs that move off GitHub Actions to the
# Railway cron commit as the same bot, so the tenant's history stays uniform.
BOT_IDENTITY = ("cp-engine-bot", "cp-engine-bot@users.noreply.github.com")


def _identity_env(identity: tuple[str, str] | None) -> dict:
    """Author AND committer env for `identity`. Env, not just `git config`:
    the webhook service sets GIT_AUTHOR_NAME/EMAIL, and git's env beats its
    config — a config-only override would still author as the webhook."""
    if identity is None:
        return {}
    name, email = identity
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
            "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}


@contextmanager
def _writer_lock():
    """The per-tenant writer lock on its own, for a job that clones without it
    (`_full_history_clone`) and takes it only around commit + push."""
    repo_url = os.environ.get("CP_TENANT_REPO_URL")
    if not repo_url:
        raise HTTPException(status_code=500, detail="CP_TENANT_REPO_URL not configured")
    with _tenant_write_lock(repo_url) as held:
        yield held


@contextmanager
def _full_history_clone(*, identity: tuple[str, str] | None = None):
    """Full-history, full-blob clone WITHOUT the writer lock, for the cron's
    long tenant writers (sync, draft-summaries). Yields the checkout path;
    always cleans up.

    - FULL HISTORY: partial-refresh detection dates each Exec Summary field
      with `git blame` (`exec_summary_freshness`), which a `--depth=10` clone
      answers wrongly — the workflows use `fetch-depth: 0` for this reason.
      Blobs too: blame over a blob-less clone faults in every revision one
      fetch at a time. The tenant is ~35 MB packed (2026-10).
    - NO LOCK HERE: a sync takes minutes, and the lock is taken synchronously
      on the event-loop thread by most routes — holding it for the whole run
      would freeze every delivery behind it. The caller takes `_writer_lock()`
      around commit + push only; a commit landed meanwhile is rebased onto,
      and a conflict fails loudly (`_push_with_retry`), exactly as the
      workflows' push-with-rebase.sh does.
    """
    repo_url = os.environ.get("CP_TENANT_REPO_URL")
    if not repo_url:
        raise HTTPException(status_code=500, detail="CP_TENANT_REPO_URL not configured")
    tmp = Path(tempfile.mkdtemp(prefix="cp-webhook-full-"))
    dest = tmp / "cp"
    try:
        env = _ssh_env()
        out = subprocess.run(["git", "clone", "--no-tags", repo_url, str(dest)],
                             env=env, capture_output=True, text=True)
        if out.returncode != 0:
            # git's own message (auth, network) — never the key itself.
            raise RuntimeError(f"tenant clone failed: {out.stderr.strip()[-300:]}")
        name, email = identity or (
            os.environ.get("GIT_AUTHOR_NAME", "cp-engine-webhook"),
            os.environ.get("GIT_AUTHOR_EMAIL", "webhook@firstperson.is"))
        subprocess.run(["git", "config", "user.name", name], cwd=dest, check=True)
        subprocess.run(["git", "config", "user.email", email], cwd=dest, check=True)
        yield dest
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
    # Two pushes landing at the same instant: the server's ref update loses
    # the compare-and-swap ("cannot lock ref ... is at X but expected Y").
    # Same race, same recovery (architecture plan step 2).
    "cannot lock ref",
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


# ──────────────────────────────────────────────────────────────────────
#  Managed-region guard at the webhook (architecture plan step 2)
# ──────────────────────────────────────────────────────────────────────
#
# The webhook commits what its routes wrote. A route that writes INSIDE an
# engine-managed region without rendering it (#263: the account-summary
# append landed inside a marker and sync ate 39 summaries across 18 runs)
# used to be committed as-is and silently destroyed by the next render. Now,
# before every commit, each region the commit would change is checked: the
# engine's own splices carry a matching digest (cp_engine.region_guard); any
# other change is reverted to HEAD's content, the text is preserved under
# exceptions/region-edits/ (committed in the same commit), and a warning +
# Sentry event name it. `exec-summary` is authored and exempt.


def _replace_region_inner(text: str, region: str, inner: str) -> str:
    start = f"<!-- cp-engine:start {region} -->"
    end = f"<!-- cp-engine:end {region} -->"
    s = text.find(start)
    e = text.find(end, s + len(start)) if s >= 0 else -1
    if s < 0 or e < 0:
        return text
    return text[: s + len(start)] + inner + text[e:]


def _guard_managed_regions(tenant_root: Path) -> list[str]:
    """Revert + preserve foreign edits inside managed regions in the clone's
    working tree. Returns ``["<path>#<region> -> <quarantine>", ...]``.
    Never raises: a guard failure must not cost the route its write."""
    from cp_engine import region_guard

    findings: list[str] = []
    try:
        diff = subprocess.run(
            ["git", "diff", "--name-only", "HEAD", "--", "*.md"],
            cwd=tenant_root, capture_output=True, text=True,
        )
        for rel in [ln for ln in diff.stdout.splitlines() if ln.strip()]:
            path = tenant_root / rel
            if not path.is_file():
                continue
            head = subprocess.run(
                ["git", "show", f"HEAD:{rel}"],
                cwd=tenant_root, capture_output=True, text=True,
            )
            if head.returncode != 0:
                continue
            before = region_guard.regions(head.stdout)
            if not before:
                continue
            text = path.read_text(encoding="utf-8")
            new_text = text
            for name, inner in region_guard.regions(text).items():
                if not region_guard.is_guarded(name) or name not in before:
                    continue
                if inner == before[name] or region_guard.provenance(inner) == "engine":
                    continue
                edit = region_guard.report(
                    source=path, region=name, discarded=inner,
                    replacement=before[name], writer="cp-engine-webhook",
                    hint="Reverted to the committed region before this commit.",
                )
                new_text = _replace_region_inner(new_text, name, before[name])
                findings.append(f"{rel}#{name} -> {edit.quarantined or 'NOT PRESERVED'}")
            if new_text != text:
                path.write_text(new_text, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — guard must never break the write
        log.warning("region guard failed open: %s", exc, exc_info=True)
        observability.capture(exc, area="region_guard")
        return findings
    if findings:
        log.warning("region guard reverted %d foreign region edit(s): %s",
                    len(findings), "; ".join(findings))
        observability.capture(
            RuntimeError(f"foreign managed-region edit(s) reverted: {'; '.join(findings)}"),
            area="region_guard",
        )
    return findings


def _stage_all(tenant_root: Path) -> None:
    """``git add -A`` plus the quarantine dir, which a sparse clone may not
    have checked out (``add -A`` skips paths outside the sparse cone)."""
    from cp_engine.region_guard import QUARANTINE_DIR

    # In a sparse clone a new file outside the cone makes plain `add -A`
    # exit 1 ("paths outside of your sparse-checkout definition"); `--sparse`
    # lets it stage the quarantine. Only used when one was written, so an
    # ordinary commit stages exactly what it always did.
    cmd = ["git", "add", "-A"]
    if (tenant_root / QUARANTINE_DIR).is_dir():
        cmd.append("--sparse")
    subprocess.run(cmd, cwd=tenant_root, check=True)


def _region_guard_trailer(findings: list[str]) -> str:
    return "".join(f"Region-Guard-Reverted: {f}\n" for f in findings)


def _commit_with_message_and_push(
    tenant_root: Path,
    message: str,
    *,
    reapply: Callable[[], bool] | None = None,
    identity: tuple[str, str] | None = None,
    guard_regions: bool = True,
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

    ``identity`` (name, email) commits — and re-commits on a rebase — as that
    author and committer (`BOT_IDENTITY` for the cron's tenant writers).

    ``guard_regions=False`` is for the RENDERER only (the cron sync): the
    guard exists to stop a non-renderer writing inside a managed region, and
    sync is the writer the regions belong to. Sync quarantines foreign edits
    itself before it splices (`region_guard`), as `cxp sync` does in CI.
    """
    env = {**_ssh_env(), **_identity_env(identity)}

    # Architecture plan step 2: never commit a foreign edit inside a managed
    # region (reverted + preserved; may leave only the quarantine file).
    findings = _guard_managed_regions(tenant_root) if guard_regions else []
    if findings:
        message = message.rstrip("\n") + "\n" + _region_guard_trailer(findings)

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

    _stage_all(tenant_root)
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
            if guard_regions:
                _guard_managed_regions(tenant_root)
            _stage_all(tenant_root)
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


#: `_commit_meeting_artifacts`' "nothing to commit" — distinct from None (failed).
ARTIFACTS_UNCHANGED = ""


def _commit_meeting_artifacts(
    *, tenant_root: Path, meeting_id: str, artifact_paths: list[Path]
) -> str | None:
    """Commit + push the per-meeting artifact files.

    Separate from _commit_and_push because a meeting can produce an
    artifact even when it wrote no sprint-file bullets (so the per-project
    commit loop would never fire). Stages only the artifact paths.

    Best-effort: returns None on failure rather than raising — a failed
    artifact commit must not break the auto-ingest contract.

    Returns ``ARTIFACTS_UNCHANGED`` (``""``) when there is nothing to commit —
    already swept in, or a re-ingest that rewrote the pair byte for byte. That
    is NOT a failure, and the caller must be able to tell the two apart: both
    used to be None, so a clean re-run was reported as "commit/push failed".
    """
    if not artifact_paths:
        return None
    try:
        env = _ssh_env()
        rels = [str(p.relative_to(tenant_root)) for p in artifact_paths]
        subprocess.run(["git", "add", *rels], cwd=tenant_root, check=True)

        # If the sprint-file commit already swept these in via `git add
        # -A`, or a re-ingest wrote identical files, nothing is staged.
        staged = subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=tenant_root, env=env
        )
        if staged.returncode == 0:
            return ARTIFACTS_UNCHANGED

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
    findings = _guard_managed_regions(tenant_root)

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

    _stage_all(tenant_root)
    message = (
        f"[clickup-close] {code}: hash {cp_hash}\n\n"
        f"Generated by cp-engine-webhook v{cp_engine.__version__}.\n"
        f"{_correlation_trailer()}"
        f"{_region_guard_trailer(findings)}"
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
