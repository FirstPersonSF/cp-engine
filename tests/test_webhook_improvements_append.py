"""POST /api/improvements/append against a real git tenant (cp-engine #282).

WHY THIS FILE EXISTS, AND WHAT IT CATCHES THAT ITS SIBLINGS DO NOT.

The project-state and sessions webhook tests fake `_cloned_tenant` with a stub
that ignores `sparse_paths` entirely. That is fine for what they assert, and it
is exactly why the first version of this route reached production broken: it
passed `sparse_paths=["improvements.md"]`, and `git sparse-checkout set` takes
DIRECTORIES — a filename fails hard with "not a directory" and 500s the route.
Every sibling caller passes `_SCOPE_DIRS`; I passed a file, and no test could
see the difference because the argument was never used.

So the clone fake here VALIDATES the argument the way git does. The route is
also exercised end to end against a real repository, so the commit and the
entry format are checked rather than asserted.
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
from fastapi.testclient import TestClient

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
sys.path.insert(0, str(_WEBHOOK))

import git_ops  # noqa: E402
import main as webhook_main  # noqa: E402

_LOG = """# Improvements log

Protocol prose.

- 2026-09-16 · `sprints parser` — An earlier observation that must survive.
"""


def _git(*args, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def tenant(tmp_path: Path) -> Path:
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
    (work / "improvements.md").write_text(_LOG)

    _git("add", "-A", cwd=work)
    _git("commit", "-m", "seed", cwd=work)
    _git("push", "-u", "origin", "main", cwd=work)
    return work


@pytest.fixture
def client(tenant: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")

    @contextmanager
    def _fake_clone(sparse_paths=None):
        # THE GUARD THE SIBLING FAKES OMIT. `git sparse-checkout set` is cone
        # mode: every entry must be a DIRECTORY. A filename aborts the command
        # and 500s the route, which is exactly how this shipped broken.
        for p in sparse_paths or []:
            target = tenant / p
            assert not target.is_file(), (
                f"sparse_paths must name directories; {p!r} is a file. Cone "
                "mode already materializes every root-level file, so a "
                "root-level file needs no sparse path at all."
            )
        yield tenant

    monkeypatch.setattr(git_ops, "_cloned_tenant", _fake_clone)
    monkeypatch.setattr(git_ops, "_ssh_env", dict)
    monkeypatch.setattr(
        git_ops, "_commit_with_message_and_push", lambda root, msg: "deadbee"
    )
    return TestClient(webhook_main.app)


def _signed(body: bytes, *, secret: bytes = b"test-secret") -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def _post(client: TestClient, payload: dict):
    body = json.dumps(payload).encode()
    return client.post(
        "/api/improvements/append",
        content=body,
        headers={"x-webhook-signature": _signed(body)},
    )


GOOD = {
    "area": "hosted-mcp",
    "observation": "A tenant file was readable but not writable from a hosted session.",
    "user": "drew",
}


def test_an_entry_lands_in_the_file(client: TestClient, tenant: Path) -> None:
    r = _post(client, GOOD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["changed"] is True
    text = (tenant / "improvements.md").read_text()
    assert "`hosted-mcp` — A tenant file was readable" in text


def test_the_existing_log_is_untouched(client: TestClient, tenant: Path) -> None:
    """NEVER DELETE is the file's own rule."""
    _post(client, GOOD)
    text = (tenant / "improvements.md").read_text()
    assert "- 2026-09-16 · `sprints parser` — An earlier observation that must survive." in text


def test_a_duplicate_does_not_commit_twice(client: TestClient, tenant: Path) -> None:
    """A retry after a timeout must not double-log or manufacture a commit."""
    first = _post(client, GOOD).json()
    second = _post(client, GOOD).json()
    assert first["changed"] is True and first["commit"]
    assert second["changed"] is False and second["commit"] is None
    text = (tenant / "improvements.md").read_text()
    assert text.count("A tenant file was readable") == 1


def test_a_thin_observation_is_a_400(client: TestClient) -> None:
    r = _post(client, dict(GOOD, observation="slow"))
    assert r.status_code == 400
    assert "real prose" in r.json()["detail"]


def test_the_user_is_required(client: TestClient) -> None:
    """The entry names a commit, so it has to be attributable."""
    payload = dict(GOOD)
    payload.pop("user")
    r = _post(client, payload)
    assert r.status_code == 400
