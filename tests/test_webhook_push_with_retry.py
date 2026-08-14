"""Tests for _push_with_retry (v0.15.2 Fix 2 — I4).

Concurrent webhook requests each clone independently and race on `git
push origin main`. The loser gets a non-fast-forward rejection. The
``_push_with_retry`` helper recovers with ``git pull --rebase`` and
retries up to ``_PUSH_MAX_ATTEMPTS``; if the rebase itself conflicts, it
``git rebase --abort``s and re-raises so the request fails cleanly
rather than wedging the working tree.

#181 added jittered backoff between attempts and took the ceiling 3 -> 5:
retries used to fire back-to-back, which is sized for two webhooks
colliding, not for the eleven-webhook burst that dropped commits on
2026-08-12.

We mock subprocess.run so we can drive the push/rebase exit codes
without spinning up real git repos.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import main as webhook_main  # noqa: F401 — path shim side effect
import git_ops


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Never actually sleep (#181 added jittered backoff).

    Without this the suite pays real wall-clock for every retry path. Patched
    on `git_ops.time.sleep` rather than globally so a genuine sleep elsewhere
    would still be visible.
    """
    monkeypatch.setattr(git_ops.time, "sleep", lambda _s: None)


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    """A subprocess.CompletedProcess look-alike."""
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    r.args = ["git", "push", "origin", "main"]
    return r


def test_push_succeeds_first_try(tmp_path: Path) -> None:
    """No rebase loop: a single successful push returns immediately."""
    with patch.object(git_ops.subprocess, "run") as run:
        run.return_value = _result(returncode=0)
        git_ops._push_with_retry(tmp_path, target_branch="main", env={})
    assert run.call_count == 1
    args = run.call_args_list[0].args[0]
    assert args == ["git", "push", "origin", "main"]


def test_push_retries_after_non_fast_forward(tmp_path: Path) -> None:
    """Non-fast-forward rejection -> pull --rebase -> push succeeds."""
    push_reject = _result(
        returncode=1,
        stderr="! [rejected]    main -> main (non-fast-forward)",
    )
    rebase_ok = _result(returncode=0)
    push_ok = _result(returncode=0)

    with patch.object(git_ops.subprocess, "run") as run:
        run.side_effect = [push_reject, rebase_ok, push_ok]
        git_ops._push_with_retry(tmp_path, target_branch="main", env={})

    assert run.call_count == 3
    cmds = [c.args[0] for c in run.call_args_list]
    assert cmds[0] == ["git", "push", "origin", "main"]
    assert cmds[1] == ["git", "pull", "--rebase", "origin", "main"]
    assert cmds[2] == ["git", "push", "origin", "main"]


def test_push_gives_up_after_max_attempts(tmp_path: Path) -> None:
    """N consecutive non-fast-forward rejections -> CalledProcessError.

    Derived from `_PUSH_MAX_ATTEMPTS` rather than hardcoded, so raising the
    ceiling (#181 took it 3 -> 5) doesn't silently leave a test asserting the
    old shape.
    """
    n = git_ops._PUSH_MAX_ATTEMPTS
    push_reject = _result(
        returncode=1,
        stderr="! [rejected] (non-fast-forward) — fetch first",
    )
    rebase_ok = _result(returncode=0)

    with patch.object(git_ops.subprocess, "run") as run:
        # (push reject -> rebase ok) x (n-1), then a final push reject.
        run.side_effect = [push_reject, rebase_ok] * (n - 1) + [push_reject]
        with pytest.raises(subprocess.CalledProcessError):
            git_ops._push_with_retry(tmp_path, target_branch="main", env={})

    # n pushes + (n-1) rebases
    assert run.call_count == 2 * n - 1


def test_push_aborts_rebase_on_conflict_and_raises(tmp_path: Path) -> None:
    """If pull --rebase itself fails (conflict on same lines), abort and raise."""
    push_reject = _result(
        returncode=1,
        stderr="! [rejected] non-fast-forward",
    )
    rebase_fail = _result(
        returncode=1,
        stderr="CONFLICT (content): Merge conflict in sprints/2026-W22/ggl-5168.md",
    )

    with patch.object(git_ops.subprocess, "run") as run:
        run.side_effect = [push_reject, rebase_fail, _result(returncode=0)]
        with pytest.raises(subprocess.CalledProcessError):
            git_ops._push_with_retry(tmp_path, target_branch="main", env={})

    # 1 push, 1 rebase, 1 abort. NOT a second push attempt.
    cmds = [c.args[0] for c in run.call_args_list]
    assert cmds[0] == ["git", "push", "origin", "main"]
    assert cmds[1] == ["git", "pull", "--rebase", "origin", "main"]
    assert cmds[2] == ["git", "rebase", "--abort"]
    assert run.call_count == 3


def test_push_non_recoverable_error_raises_without_rebase(tmp_path: Path) -> None:
    """Auth errors / network failures / hook rejects are NOT non-fast-forward.
    We don't try to rebase those — surface them immediately so the operator
    sees the real signal in logs."""
    push_auth_fail = _result(
        returncode=128,
        stderr="ERROR: Permission to FirstPersonSF/cp.git denied to deploy key.",
    )

    with patch.object(git_ops.subprocess, "run") as run:
        run.side_effect = [push_auth_fail]
        with pytest.raises(subprocess.CalledProcessError):
            git_ops._push_with_retry(tmp_path, target_branch="main", env={})

    assert run.call_count == 1  # no rebase attempted


def test_push_auth_failure_with_rejected_word_does_not_retry(tmp_path: Path) -> None:
    """Regression: a real auth-failure stderr containing only the word
    'rejected' (but NOT 'non-fast-forward' / 'fetch first') must raise
    immediately, not trigger two wasted rebase round-trips.

    Previously the marker tuple included a bare 'rejected' substring,
    which matched every push reject (auth, pre-receive hooks, branch
    protection) and burned ~10s of retry latency on each before bottoming
    out at attempt == max_attempts."""
    push_auth_fail = _result(
        returncode=1,
        stderr=(
            "remote: Permission to FirstPersonSF/cp.git denied to deploy key.\n"
            "fatal: unable to access 'https://github.com/FirstPersonSF/cp.git/': "
            "The requested URL returned error: 403\n"
            "error: failed to push some refs to 'origin'\n"
            "remote: error: GH006: Protected branch update failed for refs/heads/main.\n"
            "! [remote rejected] main -> main (protected branch hook declined)"
        ),
    )

    with patch.object(git_ops.subprocess, "run") as run:
        run.side_effect = [push_auth_fail]
        with pytest.raises(subprocess.CalledProcessError):
            git_ops._push_with_retry(tmp_path, target_branch="main", env={})

    # ONE push attempt, NO rebase, NO retry. The bug would have produced
    # 5 calls (push, rebase, push, rebase, push).
    assert run.call_count == 1
    cmds = [c.args[0] for c in run.call_args_list]
    assert cmds[0] == ["git", "push", "origin", "main"]


def test_push_warns_when_rebase_abort_itself_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression: if git rebase --abort fails (worktree already clean,
    no rebase in progress, transient git error), surface it as a
    warning log line — a silently-wedged worktree would be invisible
    to the operator otherwise."""
    push_reject = _result(
        returncode=1,
        stderr="! [rejected] non-fast-forward",
    )
    rebase_fail = _result(
        returncode=1,
        stderr="CONFLICT (content): Merge conflict in sprints/2026-W22/ggl-5168.md",
    )
    abort_fail = _result(
        returncode=128,
        stderr="fatal: No rebase in progress?",
    )

    import logging

    with patch.object(git_ops.subprocess, "run") as run:
        run.side_effect = [push_reject, rebase_fail, abort_fail]
        with caplog.at_level(logging.WARNING, logger="cp-engine-webhook"):
            with pytest.raises(subprocess.CalledProcessError):
                git_ops._push_with_retry(
                    tmp_path, target_branch="main", env={}
                )

    # All three subprocess calls happened.
    cmds = [c.args[0] for c in run.call_args_list]
    assert cmds == [
        ["git", "push", "origin", "main"],
        ["git", "pull", "--rebase", "origin", "main"],
        ["git", "rebase", "--abort"],
    ]
    # And the abort failure surfaced in logs.
    assert any(
        "rebase --abort failed" in r.message.lower()
        for r in caplog.records
    )


# --- #181: jittered backoff between attempts --------------------------------


def test_backoff_is_bounded_and_widens_with_attempt() -> None:
    """Full jitter: a draw from [0, base * 2**attempt).

    Asserts the SHAPE, not a value — the whole point is that it's random.
    """
    base = git_ops._PUSH_BACKOFF_BASE_SEC
    for attempt in range(1, 6):
        ceiling = base * (2 ** attempt)
        draws = [git_ops._push_backoff_delay(attempt) for _ in range(200)]
        assert all(0 <= d < ceiling for d in draws)
        # Not a constant — a fixed delay would leave losers in lockstep,
        # which is the actual defect #181 is about.
        assert len(set(draws)) > 1


def test_backoff_starts_from_zero_not_a_fixed_floor() -> None:
    """The first retry must be able to fire almost immediately.

    Fixed-delay-plus-noise would tax the common case (a lone webhook that
    lost one race) to fix the rare one. Full jitter doesn't.
    """
    draws = [git_ops._push_backoff_delay(1) for _ in range(500)]
    assert min(draws) < git_ops._PUSH_BACKOFF_BASE_SEC * 0.5


def test_push_sleeps_between_attempts(tmp_path: Path, monkeypatch) -> None:
    """The retry loop actually calls the backoff — once per retry, not per push."""
    slept: list[float] = []
    monkeypatch.setattr(git_ops.time, "sleep", lambda s: slept.append(s))

    push_reject = _result(
        returncode=1, stderr="! [rejected] main -> main (non-fast-forward)"
    )
    with patch.object(git_ops.subprocess, "run") as run:
        run.side_effect = [push_reject, _result(0), _result(0)]
        git_ops._push_with_retry(tmp_path, target_branch="main", env={})

    # One rejection -> exactly one backoff, before the rebase.
    assert len(slept) == 1
    assert 0 <= slept[0] < git_ops._PUSH_BACKOFF_BASE_SEC * 2


def test_no_backoff_when_the_first_push_succeeds(tmp_path: Path, monkeypatch) -> None:
    """The uncontended path — by far the common one — pays nothing."""
    slept: list[float] = []
    monkeypatch.setattr(git_ops.time, "sleep", lambda s: slept.append(s))

    with patch.object(git_ops.subprocess, "run") as run:
        run.return_value = _result(returncode=0)
        git_ops._push_with_retry(tmp_path, target_branch="main", env={})

    assert slept == []


def test_no_backoff_on_a_non_recoverable_failure(tmp_path: Path, monkeypatch) -> None:
    """An auth/hook reject raises immediately — sleeping would just delay a 500."""
    slept: list[float] = []
    monkeypatch.setattr(git_ops.time, "sleep", lambda s: slept.append(s))

    with patch.object(git_ops.subprocess, "run") as run:
        run.return_value = _result(
            returncode=128, stderr="fatal: Authentication failed"
        )
        with pytest.raises(subprocess.CalledProcessError):
            git_ops._push_with_retry(tmp_path, target_branch="main", env={})

    assert slept == []
