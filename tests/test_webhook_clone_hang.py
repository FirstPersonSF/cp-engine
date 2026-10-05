"""A tenant clone that hangs must not freeze every Slack click (2026-10-05).

Every tenant write runs under one lock. A clone whose SSH connection went
silent had no timeout, so it held the lock for 4m17s and every "Mark closed"
click queued behind it sat on "⏳ Closing…".
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import git_ops  # noqa: E402


def test_ssh_command_kills_a_silent_connection(monkeypatch):
    monkeypatch.setenv("GIT_SSH_KEY", "-----BEGIN KEY-----\nx\n-----END KEY-----")
    cmd = git_ops._ssh_env()["GIT_SSH_COMMAND"]
    assert "ConnectTimeout=" in cmd
    assert "ServerAliveInterval=" in cmd
    assert "ServerAliveCountMax=" in cmd


def test_hung_clone_times_out_and_retries(monkeypatch, tmp_path):
    monkeypatch.setenv("CP_TENANT_REPO_URL", "git@github.com:x/cp.git")
    calls: list[dict] = []
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["git", "clone"]:
            calls.append(kw)
            if kw.get("timeout") is None:
                pytest.fail("clone runs with no timeout — a hang holds the lock forever")
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(cmd, kw["timeout"])
            Path(cmd[-1]).mkdir(parents=True)
            real_run(["git", "init", "-q", cmd[-1]], check=True)
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)
    with git_ops._cloned_tenant() as root:
        assert root.exists()
    assert len(calls) == 2  # first hung → retried once


def test_clone_that_keeps_hanging_raises_and_frees_the_lock(monkeypatch):
    monkeypatch.setenv("CP_TENANT_REPO_URL", "git@github.com:x/cp-hang.git")

    def fake_run(cmd, *a, **kw):
        if cmd[:2] == ["git", "clone"]:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)
    with pytest.raises(subprocess.TimeoutExpired):
        with git_ops._cloned_tenant():
            pass
    # The lock is free: the next write gets in.
    lock = git_ops._tenant_locks["git@github.com:x/cp-hang.git"]
    assert lock.acquire(timeout=0.1)
    lock.release()
