"""POST /api/improvements/append against a real git tenant (cp-engine #282).

WHY THIS FILE EXISTS, AND WHAT IT CATCHES THAT ITS SIBLINGS DO NOT.

The project-state and sessions webhook tests fake `_cloned_tenant` with a stub
that ignores `sparse_paths` entirely. That is fine for what they assert, and it
is exactly why the first version of this route reached production broken: it
passed `sparse_paths=["improvements.md"]`, and `git sparse-checkout set` takes
DIRECTORIES — a filename fails hard with "not a directory" and 500s the route.
Every sibling caller passes `_SCOPE_DIRS`; I passed a file, and no test could
see the difference because the argument was never used.

So the clone fake here VALIDATES the argument the way git does, and
`test_the_route_passes_no_file_as_a_sparse_path` records the argument directly
— the CONTROL that would have caught `0fc9c6a`.

WHAT IS REAL AND WHAT IS STUBBED. The tenant is a real git repository (bare
origin + seeded working clone) and the route reads and writes the real
`improvements.md` in it, so the entry's format and placement are checked on
disk. What is NOT real: `_cloned_tenant` is a fake that yields that clone (no
network, no sparse checkout), and `_commit_with_message_and_push` is stubbed to
return `"deadbee"` — nothing here commits or pushes. A test that wants the
commit itself has to run git; these do not.
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

## Open

- 2026-09-16 · `sprints parser` — An earlier observation that must survive.

## Resolved

- 2026-07-21 · `mcp` — A resolved observation. [fixed: 2026-08-01]
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
    # `reapply=` is the #290 conflict-recovery callback; the stub takes it
    # and ignores it, because nothing here races.
    monkeypatch.setattr(
        git_ops,
        "_commit_with_message_and_push",
        lambda root, msg, reapply=None: "deadbee",
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


def test_the_entry_lands_under_open_not_resolved(client: TestClient, tenant: Path) -> None:
    """#293. The first webhook commit (`201baa46`) landed at EOF — under
    `## Resolved`. `sweep improvements` reads section membership, so that is
    a lost entry, not a cosmetic one."""
    assert _post(client, GOOD).status_code == 200
    text = (tenant / "improvements.md").read_text()
    assert text.index("`hosted-mcp` — A tenant file") < text.index("## Resolved")
    resolved = text.split("## Resolved", 1)[1]
    assert "hosted-mcp" not in resolved


def test_the_route_passes_no_file_as_a_sparse_path(tenant: Path, monkeypatch) -> None:
    """CONTROL for `0fc9c6a`. `git sparse-checkout set` is cone mode and takes
    DIRECTORIES; the route once passed `sparse_paths=["improvements.md"]` and
    500'd in production because no test looked at the argument.

    This records what the route actually passes and requires it to be either
    nothing or directories only. Restoring the bug fails this test.
    """
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    seen: list[list[str] | None] = []

    @contextmanager
    def _recording_clone(sparse_paths=None):
        seen.append(sparse_paths)
        yield tenant

    monkeypatch.setattr(git_ops, "_cloned_tenant", _recording_clone)
    monkeypatch.setattr(git_ops, "_ssh_env", dict)
    monkeypatch.setattr(
        git_ops, "_commit_with_message_and_push", lambda root, msg, reapply=None: "deadbee"
    )

    r = _post(TestClient(webhook_main.app), GOOD)
    assert r.status_code == 200, r.text
    assert len(seen) == 1
    for p in seen[0] or []:
        assert not (tenant / p).is_file(), f"sparse_paths names a FILE: {p!r}"
        assert "." not in Path(p).name, f"sparse_paths looks like a filename: {p!r}"


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


def test_the_response_names_the_entry_that_was_inserted(client: TestClient, tenant: Path) -> None:
    """Since #293 the entry lands mid-file, under `## Open`. The route used
    to report the file's LAST line as the entry — which is now the newest
    `## Resolved` entry, someone else's, from weeks ago."""
    r = _post(client, {
        "area": "audit",
        "observation": "The response must name the line it inserted, not the last one.",
        "user": "drew",
    })
    assert r.status_code == 200, r.text
    entry = r.json()["entry"]
    assert "`audit`" in entry and "name the line it inserted" in entry
    text = (tenant / "improvements.md").read_text()
    assert entry in text
    assert text.rstrip("\n").splitlines()[-1] != entry, "entry landed at EOF, not under Open"
