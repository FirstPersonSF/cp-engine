"""Tests for POST /api/sessions/capture (cp-engine #247).

THE GAP THIS CLOSES: a hosted-MCP user cannot leave a session record.
`/cp-wrapup` and `cxp capture-session` are LOCAL — they need a checkout — and
the hosted server holds a READ-ONLY deploy key by design. Measured on the live
tenant 2026-09-14: Tony had 134 hosted writes across 11 engagements, 1 git
commit ever, and 0 session files; 83 of the tenant's 84 session files were
Drew's.

WHY THE FILE MUST REACH THE REPO (the fact that decided the design):
`sync._refresh_all_last_session_lines` derives every `**Last session:**` line
by globbing `**/sessions` ON DISK. A capture persisted only as a DB row would
never advance that line — which is the entire point of capturing. So these
tests assert against a REAL git repo on disk, not a mock: the thing under test
is that a file lands, the line moves, and a commit happens.

Git is real here (a temp repo with a bare origin); only the HMAC secret and
the clone helper are wired to the fixture.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

from fastapi.testclient import TestClient

import git_ops
import main as webhook_main
from routers import sessions as sessions_router


def _signed(body: bytes, *, secret: bytes = b"test-secret") -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


CP_MD = """---
Project: GGL 5151 GRC Narrative
Filename: ggl-5151.md
---

## Exec Summary

**Objective:** land the deck.
**Last session:** 2026-01-01 — placeholder

## Notes
"""


@pytest.fixture
def tenant(tmp_path: Path) -> Path:
    """A real tenant clone with a bare origin, so commit AND push both run."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=origin)

    work = tmp_path / "cp"
    work.mkdir()
    _git("init", "--initial-branch=main", cwd=work)
    _git("config", "user.email", "test@example.com", cwd=work)
    _git("config", "user.name", "Test", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)

    (work / ".cp-engine.toml").write_text("[tenant]\nname = 'test'\n")
    proj = work / "1p" / "google" / "ggl-5151-grc-narrative"
    proj.mkdir(parents=True)
    (proj / "cp.md").write_text(CP_MD)
    # An initiative at depth 1, to prove both nesting depths resolve.
    init = work / "firstpersonsf" / "mission-control"
    init.mkdir(parents=True)
    (init / "cp.md").write_text(CP_MD)

    _git("add", "-A", cwd=work)
    _git("commit", "-m", "seed", cwd=work)
    _git("push", "-u", "origin", "main", cwd=work)
    return work


@pytest.fixture
def client(tenant: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")

    @contextmanager
    def _fake_clone(sparse_paths=None):
        yield tenant

    monkeypatch.setattr(git_ops, "_cloned_tenant", _fake_clone)
    # The route calls git_ops._commit_with_message_and_push, which needs the
    # ssh env only for a real remote; a local path remote works as-is.
    monkeypatch.setattr(git_ops, "_ssh_env", dict)
    return TestClient(webhook_main.app)


def _post(client: TestClient, payload: dict):
    body = json.dumps(payload).encode()
    return client.post(
        "/api/sessions/capture",
        content=body,
        headers={"x-webhook-signature": _signed(body)},
    )


GOOD = {
    "project_code": "ggl-5151-grc-narrative",
    "summary": "Worked through the deck architecture and landed ADR-001.",
    "user": "Tony",
    "when": "2026-09-14T16:39:00Z",
}


class TestTheCaptureLands:
    def test_writes_the_session_file_and_commits(self, client, tenant):
        r = _post(client, GOOD)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True

        landed = tenant / body["session_path"]
        assert landed.is_file()
        assert landed.read_text() == GOOD["summary"]
        # Filename convention is the engine's, not reimplemented here.
        assert landed.name == "2026-09-14-1639-tony.md"
        assert landed.parent.name == "sessions"

    def test_the_commit_is_real_and_pushed(self, client, tenant):
        r = _post(client, GOOD)
        sha = r.json()["commit"]
        assert sha

        # The file is IN the commit, not merely on disk.
        tracked = subprocess.run(
            ["git", "show", "--name-only", "--format=", sha],
            cwd=tenant, check=True, capture_output=True, text=True,
        ).stdout
        assert "sessions/2026-09-14-1639-tony.md" in tracked
        # And it reached the remote — a capture that commits but never pushes
        # is invisible to everyone else, which is the failure being fixed.
        remote = subprocess.run(
            ["git", "ls-remote", "origin", "main"],
            cwd=tenant, check=True, capture_output=True, text=True,
        ).stdout
        assert sha[:8] in remote

    def test_last_session_line_advances(self, client, tenant):
        """The whole point: the derived line must move. It is a projection of
        the sessions/ directory, so a capture that does not reach disk would
        leave it frozen."""
        cp_md = tenant / "1p/google/ggl-5151-grc-narrative/cp.md"
        assert "2026-01-01" in cp_md.read_text()

        r = _post(client, GOOD)
        assert r.json()["cp_md_updated"] is True
        assert "2026-09-14" in cp_md.read_text()

    def test_resolves_a_depth_one_initiative(self, client, tenant):
        """Engagements are company-nested (`1p/<co>/<slug>`); initiatives and
        standalone repos sit one level up. Both must resolve."""
        r = _post(client, {**GOOD, "project_code": "mission-control"})
        assert r.status_code == 200, r.text
        assert r.json()["session_path"].startswith("firstpersonsf/mission-control/")

    def test_two_captures_same_minute_do_not_collide(self, client, tenant):
        first = _post(client, GOOD).json()["session_path"]
        second = _post(client, GOOD).json()["session_path"]
        assert first != second
        assert (tenant / first).is_file() and (tenant / second).is_file()


class TestItRefusesBadInput:
    def test_unsigned_is_rejected(self, client):
        body = json.dumps(GOOD).encode()
        r = client.post("/api/sessions/capture", content=body)
        assert r.status_code in (401, 403)

    def test_unknown_project_code_is_404_not_a_silent_write(self, client):
        r = _post(client, {**GOOD, "project_code": "nope-9999"})
        assert r.status_code == 404
        assert "no working dir" in r.json()["detail"]

    @pytest.mark.parametrize("field", ["project_code", "user"])
    def test_missing_required_field(self, client, field):
        r = _post(client, {**GOOD, field: "  "})
        assert r.status_code == 400
        assert field in r.json()["detail"]

    def test_an_empty_summary_is_refused(self, client):
        """An empty capture would advance the Last-session line while saying
        nothing — worse than no capture, because it looks like one."""
        r = _post(client, {**GOOD, "summary": "   "})
        assert r.status_code == 400
        assert "real prose" in r.json()["detail"]

    def test_a_runaway_summary_is_refused(self, client):
        r = _post(client, {**GOOD, "summary": "x" * 200_000})
        assert r.status_code == 400
        assert "exceeds" in r.json()["detail"]

    def test_a_bad_timestamp_is_reported_not_swallowed(self, client):
        r = _post(client, {**GOOD, "when": "the fourteenth"})
        assert r.status_code == 400
        assert "invalid `when`" in r.json()["detail"]

    def test_nothing_is_committed_when_the_code_does_not_resolve(
        self, client, tenant
    ):
        before = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tenant,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        _post(client, {**GOOD, "project_code": "nope-9999"})
        after = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=tenant,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        assert before == after
