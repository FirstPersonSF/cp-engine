"""Tests for _push_with_retry (v0.15.2 Fix 2 — I4).

Concurrent webhook requests each clone independently and race on `git
push origin main`. The loser gets a non-fast-forward rejection. The
new ``_push_with_retry`` helper recovers with ``git pull --rebase``
and retries up to 3 attempts; if the rebase itself conflicts, it
``git rebase --abort``s and re-raises so the request fails cleanly
rather than wedging the working tree.

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

import main as webhook_main


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
    with patch.object(webhook_main.subprocess, "run") as run:
        run.return_value = _result(returncode=0)
        webhook_main._push_with_retry(tmp_path, target_branch="main", env={})
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

    with patch.object(webhook_main.subprocess, "run") as run:
        run.side_effect = [push_reject, rebase_ok, push_ok]
        webhook_main._push_with_retry(tmp_path, target_branch="main", env={})

    assert run.call_count == 3
    cmds = [c.args[0] for c in run.call_args_list]
    assert cmds[0] == ["git", "push", "origin", "main"]
    assert cmds[1] == ["git", "pull", "--rebase", "origin", "main"]
    assert cmds[2] == ["git", "push", "origin", "main"]


def test_push_gives_up_after_max_attempts(tmp_path: Path) -> None:
    """Three consecutive non-fast-forward rejections -> CalledProcessError."""
    push_reject = _result(
        returncode=1,
        stderr="! [rejected] (non-fast-forward) — fetch first",
    )
    rebase_ok = _result(returncode=0)

    with patch.object(webhook_main.subprocess, "run") as run:
        # attempt 1: push reject -> rebase ok
        # attempt 2: push reject -> rebase ok
        # attempt 3: push reject -> give up
        run.side_effect = [
            push_reject, rebase_ok,
            push_reject, rebase_ok,
            push_reject,
        ]
        with pytest.raises(subprocess.CalledProcessError):
            webhook_main._push_with_retry(tmp_path, target_branch="main", env={})

    assert run.call_count == 5


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

    with patch.object(webhook_main.subprocess, "run") as run:
        run.side_effect = [push_reject, rebase_fail, _result(returncode=0)]
        with pytest.raises(subprocess.CalledProcessError):
            webhook_main._push_with_retry(tmp_path, target_branch="main", env={})

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

    with patch.object(webhook_main.subprocess, "run") as run:
        run.side_effect = [push_auth_fail]
        with pytest.raises(subprocess.CalledProcessError):
            webhook_main._push_with_retry(tmp_path, target_branch="main", env={})

    assert run.call_count == 1  # no rebase attempted
