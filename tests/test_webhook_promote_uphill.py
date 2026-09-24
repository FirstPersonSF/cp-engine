"""Tests for POST /api/promote-uphill (cp-engine #304 follow-up, v0.124.1).

THE GAP THIS CLOSES: the hosted `promote_uphill` could copy a commitment
(a row) but refused a decision (a sprint-file bullet) with `unsupported_here`,
because the hosted server holds no file write. This route is the file write,
on the service that clones the tenant with a WRITE key — the same hop
`/api/sessions/capture` and `/api/project-state/capture` already take.

Git is real here (a temp repo with a bare origin); the tenant carries the
engine's `.cp-engine/paths.json` and per-week sprint files, so the thing
under test is that the bullet lands in the PARENT's file, the commit is
pushed, and a second call writes nothing. The MC-2 client is the in-memory
fake from `tests/test_promote_uphill.py`, so the parent's spine step is
asserted too.
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
from routers import promote_uphill as promote_router

from cp_engine import promote_uphill as pu
from cp_engine.ingest import _content_hash
from tests.test_promote_uphill import (
    ACCOUNT,
    CHILD,
    DECISION_TEXT,
    IDS,
    PROGRAM,
    PROGRAM_ID,
    WEEK,
    FakeClient,
    _paths_index,
    _sprint_file,
)


def _signed(body: bytes, *, secret: bytes = b"test-secret") -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def tenant(tmp_path: Path) -> Path:
    """A real tenant clone with a bare origin, so commit AND push both run.

    Same tree the engine's own promote_uphill tests use: `.cp-engine/paths.json`
    naming job → program → account, one sprint file per workstream for the
    current week, and one decision on the job written the way auto-ingest
    writes it.
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=origin)

    work = tmp_path / "cp"
    work.mkdir()
    _git("init", "--initial-branch=main", cwd=work)
    _git("config", "user.email", "test@example.com", cwd=work)
    _git("config", "user.name", "Test", cwd=work)
    _git("remote", "add", "origin", str(origin), cwd=work)

    (work / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n[sync.mc_2]\nsupabase_project_ref = "stub"\n'
    )
    _paths_index(work)
    child = _sprint_file(work, WEEK, CHILD)
    _sprint_file(work, WEEK, PROGRAM)
    _sprint_file(work, WEEK, ACCOUNT)
    h = _content_hash(CHILD, "add-decision", DECISION_TEXT)
    child.write_text(
        child.read_text(encoding="utf-8").replace(
            "### Decisions\n",
            f"### Decisions\n- [decision · 2026-09-22] {DECISION_TEXT} <!-- cp:hash={h} -->\n",
            1,
        ),
        encoding="utf-8",
    )

    _git("add", "-A", cwd=work)
    _git("commit", "-m", "seed", cwd=work)
    _git("push", "-u", "origin", "main", cwd=work)
    return work


@pytest.fixture
def mc2(monkeypatch) -> FakeClient:
    fake = FakeClient()
    monkeypatch.setattr(pu, "_resolve_project_id", lambda c, code: IDS.get(code))
    monkeypatch.setattr(promote_router.mc2_db, "get_client", lambda *a, **k: fake)
    return fake


@pytest.fixture
def client(tenant: Path, mc2, monkeypatch) -> TestClient:
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")

    @contextmanager
    def _fake_clone(sparse_paths=None):
        _fake_clone.sparse_paths = sparse_paths
        yield tenant

    monkeypatch.setattr(git_ops, "_cloned_tenant", _fake_clone)
    monkeypatch.setattr(git_ops, "_ssh_env", dict)
    tc = TestClient(webhook_main.app)
    tc.fake_clone = _fake_clone
    return tc


def _post(client: TestClient, payload: dict):
    body = json.dumps(payload).encode()
    return client.post(
        "/api/promote-uphill",
        content=body,
        headers={"x-webhook-signature": _signed(body)},
    )


def _parent_decisions(tenant: Path, stem: str = PROGRAM, week: str = WEEK) -> str:
    body = (tenant / "sprints" / week / f"{stem}.md").read_text(encoding="utf-8")
    return body.split("### Decisions", 1)[1].split("### Discussion notes", 1)[0]


GOOD = {
    "project_code": CHILD,
    "item_kind": "decision",
    "item_ref": DECISION_TEXT,
    "note": "account-wide, per Janet",
    "actor": "tony",
}


class TestThePromotionLands:
    def test_copies_the_bullet_into_the_parents_sprint_file(self, client, tenant):
        r = _post(client, GOOD)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["promoted"] is True and body["already"] is False
        assert body["parent_code"] == PROGRAM
        assert body["level"]["code"] == PROGRAM
        assert body["from"]["code"] == CHILD and body["to"]["code"] == PROGRAM
        assert body["sprint_path"] == f"sprints/{WEEK}/{PROGRAM}.md"

        zone = _parent_decisions(tenant)
        assert (
            f"- [decision · 2026-09-22] {DECISION_TEXT} (promoted from {CHILD}) "
            f"<!-- cp:hash={body['cp_hash']} -->"
        ) in zone
        # the child's bullet is untouched
        h = _content_hash(CHILD, "add-decision", DECISION_TEXT)
        assert f"cp:hash={h}" in (tenant / body["source"]).read_text(encoding="utf-8")

    def test_the_commit_is_real_pushed_and_named(self, client, tenant):
        r = _post(client, GOOD)
        sha = r.json()["commit"]
        assert sha

        tracked = _git("show", "--name-only", "--format=%s", sha, cwd=tenant)
        assert f"sprints/{WEEK}/{PROGRAM}.md" in tracked
        subject = tracked.splitlines()[0]
        assert subject.startswith(f"[promote-uphill] {CHILD} → {PROGRAM}: ")
        assert "Ship the regional migrations" in subject
        assert subject.endswith("(tony)")
        # And it reached the remote.
        assert sha[:8] in _git("ls-remote", "origin", "main", cwd=tenant)

    def test_the_spine_step_goes_through_the_db_path(self, client, mc2):
        r = _post(client, GOOD)
        assert r.json()["step"]["position"] == 1
        steps = mc2.rows("spine_steps")
        assert len(steps) == 1 and steps[0]["project_id"] == PROGRAM_ID
        assert steps[0]["est_item_id"] == pu.PROMOTIONS_EST_ITEM_ID
        assert steps[0]["title"].startswith(f"Promoted from {CHILD}: Ship the regional")
        assert "account-wide, per Janet" in steps[0]["note"]

    def test_by_hash_works_too(self, client, tenant):
        h = _content_hash(CHILD, "add-decision", DECISION_TEXT)
        r = _post(client, {**GOOD, "item_ref": h})
        assert r.status_code == 200, r.text
        assert r.json()["promoted"] is True
        assert "cp:hash=" in _parent_decisions(tenant)

    def test_the_clone_materialises_the_sprint_files(self, client):
        """A decision lives in `sprints/`; the capture routes' cone does not
        include it. A clone without it would find no decision to promote."""
        _post(client, GOOD)
        paths = client.fake_clone.sparse_paths
        assert "sprints" in paths and ".cp-engine" in paths and "1p" in paths

    def test_without_an_mc2_client_the_bullet_lands_and_the_step_is_reported(
        self, client, tenant, monkeypatch
    ):
        monkeypatch.setattr(promote_router.mc2_db, "get_client", lambda *a, **k: None)
        r = _post(client, GOOD)
        assert r.status_code == 200, r.text
        assert r.json()["promoted"] is True
        assert "NOT written" in r.json()["step"]["note"]
        assert "cp:hash=" in _parent_decisions(tenant)


class TestAlreadyPromoted:
    def test_second_call_is_200_already_with_no_commit(self, client, tenant, mc2):
        first = _post(client, GOOD).json()
        head = _git("rev-parse", "HEAD", cwd=tenant).strip()
        second = _post(client, GOOD)
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["ok"] is True and body["already"] is True and body["promoted"] is False
        assert body["commit"] is None
        assert body["cp_hash"] == first["cp_hash"]
        assert body["sprint_path"] == first["sprint_path"]
        assert _git("rev-parse", "HEAD", cwd=tenant).strip() == head
        assert _parent_decisions(tenant).count("cp:hash=") == 1
        assert len(mc2.rows("spine_steps")) == 1


class TestItRefusesBadInput:
    def test_unsigned_is_rejected(self, client):
        body = json.dumps(GOOD).encode()
        r = client.post("/api/promote-uphill", content=body)
        assert r.status_code in (401, 403)

    def test_no_parent_is_400(self, client, tenant):
        head = _git("rev-parse", "HEAD", cwd=tenant).strip()
        r = _post(client, {**GOOD, "project_code": ACCOUNT})
        assert r.status_code == 400
        assert "no parent" in r.json()["detail"]
        assert _git("rev-parse", "HEAD", cwd=tenant).strip() == head

    def test_unknown_decision_is_400(self, client, tenant):
        r = _post(client, {**GOOD, "item_ref": "deadbeef"})
        assert r.status_code == 400
        assert "no decision on" in r.json()["detail"]
        assert "cp:hash=" not in _parent_decisions(tenant)

    def test_unindexed_code_is_400(self, client):
        r = _post(client, {**GOOD, "project_code": "zzz-9999"})
        assert r.status_code == 400
        assert "paths.json" in r.json()["detail"]

    def test_commitments_are_not_served_here(self, client):
        r = _post(client, {**GOOD, "item_kind": "commitment", "item_ref": "c-1"})
        assert r.status_code == 400
        assert "item_kind" in r.json()["detail"]

    @pytest.mark.parametrize("field", ["project_code", "item_ref"])
    def test_missing_required_field(self, client, field):
        r = _post(client, {**GOOD, field: "  "})
        assert r.status_code == 400
        assert field in r.json()["detail"]

    def test_a_bad_week_is_400(self, client):
        r = _post(client, {**GOOD, "week": "next week"})
        assert r.status_code == 400
        assert "week" in r.json()["detail"]


class TestPushFailure:
    def test_push_failure_is_502_with_the_file_written(self, client, tenant, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("remote hung up")

        monkeypatch.setattr(git_ops, "_commit_with_message_and_push", _boom)
        r = _post(client, GOOD)
        assert r.status_code == 502
        assert "push failed" in r.json()["detail"]
        assert "cp:hash=" in _parent_decisions(tenant)
