"""POST /api/word-count/rotate against a real git tenant (cp-engine #280, tier 3).

The route rolls aged Exec Summary Updates from `cp.md` into
`cp-archive-<YYYY-MM>.md`, in ONE commit naming the caller, and refuses to
push when what landed on disk lost a line. Git is real (a working clone with
a bare origin): the thing under test is what reaches ORIGIN — one commit,
both files, the caller's name — or, on a refusal, that nothing does.

`_cloned_tenant` is faked to yield that clone (no network, no sparse
checkout); the commit and push are the real `_commit_with_message_and_push`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

from fastapi.testclient import TestClient  # noqa: E402

import git_ops  # noqa: E402
import main as webhook_main  # noqa: E402
from routers import rotation as rotation_router  # noqa: E402

from cp_engine import cp_rotation  # noqa: E402

TODAY = date(2026, 9, 30)
PROJ = "firstpersonsf/1pi-9005-mission-control"

CP_MD = """---
Project: 1pi-9005 Mission Control
---

<!-- cp-engine:start exec-summary -->
## Exec Summary  ·  updated 2026-09-28

**Objective:** Run the tenant.
**Status:** Live.

**Updates:**
- 2026-09-28 — Fresh entry that stays.
- 2026-09-01 — Old entry one, moves.
  - its nested detail
- 2026-08-20 — Old entry two, moves.
<!-- cp-engine:end exec-summary -->

## Project Notes

Hand-written prose that never moves.
"""


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _signed(body: bytes, *, secret: bytes = b"test-secret") -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


@pytest.fixture
def tenant(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=origin)

    work = tmp_path / "cp"
    work.mkdir()
    _git("init", "--initial-branch=main", cwd=work)
    _git("config", "user.email", "test@example.com", cwd=work)
    _git("config", "user.name", "cp-engine-webhook", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)

    (work / ".cp-engine.toml").write_text("[tenant]\nname = 'test'\n")
    proj = work / PROJ
    proj.mkdir(parents=True)
    (proj / "cp.md").write_text(CP_MD)

    _git("add", "-A", cwd=work)
    _git("commit", "-m", "seed", cwd=work)
    _git("push", "-u", "origin", "main", cwd=work)
    return work, origin


@pytest.fixture
def client(tenant, monkeypatch) -> TestClient:
    work, _ = tenant
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")

    @contextmanager
    def _fake_clone(sparse_paths=None):
        for p in sparse_paths or []:
            assert not (work / p).is_file(), f"sparse path {p!r} is a file"
        yield work

    monkeypatch.setattr(git_ops, "_cloned_tenant", _fake_clone)
    monkeypatch.setattr(git_ops, "_ssh_env", dict)
    monkeypatch.setattr(rotation_router, "tenant_today", lambda: TODAY)
    return TestClient(webhook_main.app)


def _post(client: TestClient, payload: dict):
    body = json.dumps(payload).encode()
    return client.post(
        "/api/word-count/rotate",
        content=body,
        headers={"x-webhook-signature": _signed(body)},
    )


GOOD = {"project_code": "1pi-9005-mission-control", "user": "Tony"}


def _origin_log(origin: Path) -> list[str]:
    return _git("log", "--format=%s", "main", cwd=origin).splitlines()


def test_rotation_lands_as_one_commit_covering_both_files(client, tenant):
    work, origin = tenant
    r = _post(client, GOOD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["changed"] is True and body["commit"]
    assert [m["date"] for m in body["moved"]] == ["2026-09-01", "2026-08-20"]
    assert body["archive_path"] == f"{PROJ}/cp-archive-2026-09.md"

    # Exactly one new commit on ORIGIN, touching exactly the two files.
    log = _origin_log(origin)
    assert len(log) == 2, log
    files = _git("show", "--name-only", "--format=", "main", cwd=origin).split()
    assert sorted(files) == sorted([f"{PROJ}/cp.md", f"{PROJ}/cp-archive-2026-09.md"])

    # The commit names the caller — subject and trailer.
    full = _git("log", "-1", "--format=%B", "main", cwd=origin)
    assert log[0].endswith("(Tony)") and log[0].startswith("[rotate] 1pi-9005-mission-control")
    assert "Requested-by: Tony" in full

    cp_md = _git("show", f"main:{PROJ}/cp.md", cwd=origin)
    archive = _git("show", f"main:{PROJ}/cp-archive-2026-09.md", cwd=origin)
    assert "Old entry one" not in cp_md and "Fresh entry that stays" in cp_md
    assert "- 2026-09-01 — Old entry one, moves.\n  - its nested detail\n" in archive
    assert "Hand-written prose that never moves." in cp_md


def test_nothing_past_age_is_an_honest_no_op(client, tenant):
    work, origin = tenant
    assert _post(client, GOOD).status_code == 200  # rotates
    r = _post(client, GOOD)  # nothing left to move
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {**body, "ok": True, "changed": False, "commit": None, "moved": []}
    assert len(_origin_log(origin)) == 2  # no second commit


def test_an_on_disk_loss_is_refused_and_nothing_is_pushed(client, tenant, monkeypatch):
    """THE DELIBERATE-LOSS CASE, at the route. The engine's in-memory plan is
    clean, but what reaches disk loses a line (a bug between planning and
    writing). The route's own HEAD-vs-tree check must refuse the push."""
    work, origin = tenant
    real_rotate = cp_rotation.rotate_cp

    def lossy_rotate(working_dir, *, today, **kw):
        plan = real_rotate(working_dir, today=today, **kw)
        arc = working_dir / plan.archive_name
        arc.write_text(arc.read_text().replace("  - its nested detail\n", ""))
        return plan

    monkeypatch.setattr(cp_rotation, "rotate_cp", lossy_rotate)
    r = _post(client, GOOD)
    assert r.status_code == 409, r.text
    assert "lost 1 line" in r.json()["detail"]
    assert len(_origin_log(origin)) == 1  # seed only — nothing pushed
    assert "Old entry one" in _git("show", f"main:{PROJ}/cp.md", cwd=origin)


def test_a_stray_file_touched_is_refused(client, tenant, monkeypatch):
    """ONE commit covering the two files — and only them."""
    work, origin = tenant
    real_rotate = cp_rotation.rotate_cp

    def stray_rotate(working_dir, *, today, **kw):
        plan = real_rotate(working_dir, today=today, **kw)
        (work / "master-cp.md").write_text("stray\n")
        return plan

    monkeypatch.setattr(cp_rotation, "rotate_cp", stray_rotate)
    r = _post(client, GOOD)
    assert r.status_code == 409, r.text
    assert "master-cp.md" in r.json()["detail"]
    assert len(_origin_log(origin)) == 1


def test_user_is_required(client):
    r = _post(client, {"project_code": "1pi-9005-mission-control"})
    assert r.status_code == 400 and "user" in r.text


def test_unknown_code_is_404(client):
    r = _post(client, {**GOOD, "project_code": "zzz-0000-nothing"})
    assert r.status_code == 404


def test_unsigned_request_is_refused(client):
    body = json.dumps(GOOD).encode()
    r = client.post(
        "/api/word-count/rotate", content=body,
        headers={"x-webhook-signature": _signed(body, secret=b"wrong")},
    )
    assert r.status_code == 401
