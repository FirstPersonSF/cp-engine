"""Tests for POST /api/project-state/capture (cp-engine #251).

THE GAP THIS CLOSES: the Exec Summary is the most-READ surface in the system
and the least-WRITTEN. Six consumers treat it as durable project truth — the
master-CP one-liner, the agenda, the planning bundle, `cxp brief`, the lint,
and hosted MCP's own `get_project_state` — and it had three writers, none of
which author prose. Measured on the live tenant 2026-09-14: 131 of 140
exec-summary rewrites in 90 days were one person; 13 of 23 engagements carried
summaries 30–62 days stale.

`docs/plans/2026-06-30-exec-summary.md` gave the MODEL all prose. The move to
hosted MCP then put the model where it cannot write a tenant file. This route
is the missing half — the model still writes every word.

Git is real here (a temp repo with a bare origin), as in
`test_webhook_sessions_capture.py`: the thing under test is that a merge lands
in the file, the stamp moves, and a commit happens.
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


def _signed(body: bytes, *, secret: bytes = b"test-secret") -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


CP_MD = """---
Project: GGL 5151 GRC Narrative
Filename: ggl-5151.md
---

## Facts

| **Owner** | Brandon |

## Exec Summary  ·  updated 2026-07-14

<!-- cp-engine:start exec-summary -->
**Last session:** 2026-09-01
**Objective:** Land the GRC narrative.
**Status:** Per-pillar status from Joe's doc dump.
**Where it stands:**
- Three of five pillars drafted.
- Waiting on Hinkle's training docs.
**Next up:** Finish pillars four and five.
**Blockers:** None.
<!-- cp-engine:end exec-summary -->

## Current Work

Hand-written prose that must survive every merge.
"""


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
    proj = work / "1p" / "google" / "ggl-5151-grc-narrative"
    proj.mkdir(parents=True)
    (proj / "cp.md").write_text(CP_MD)

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
    monkeypatch.setattr(git_ops, "_ssh_env", dict)
    return TestClient(webhook_main.app)


def _post(client: TestClient, payload: dict):
    body = json.dumps(payload).encode()
    return client.post(
        "/api/project-state/capture",
        content=body,
        headers={"x-webhook-signature": _signed(body)},
    )


def _cp_text(tenant: Path) -> str:
    return (tenant / "1p/google/ggl-5151-grc-narrative/cp.md").read_text()


GOOD = {
    "project_code": "ggl-5151-grc-narrative",
    "user": "Tony",
    "fields": {"Status": "All five pillars drafted; Hinkle still owed."},
}


class TestTheMergeLands:
    def test_writes_the_field_and_commits(self, client, tenant):
        r = _post(client, GOOD)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["changed"] == ["Status"]
        assert body["commit"]

        text = _cp_text(tenant)
        assert "**Status:** All five pillars drafted; Hinkle still owed." in text

    def test_untouched_fields_survive(self, client, tenant):
        """The 58% case — the reason this route is per-field, not whole-region."""
        _post(client, GOOD)
        text = _cp_text(tenant)
        assert "**Objective:** Land the GRC narrative." in text
        assert "**Next up:** Finish pillars four and five." in text
        assert "**Blockers:** None." in text
        assert "**Last session:** 2026-09-01" in text
        assert "- Three of five pillars drafted." in text

    def test_content_outside_the_region_survives(self, client, tenant):
        _post(client, GOOD)
        text = _cp_text(tenant)
        assert "Hand-written prose that must survive every merge." in text
        assert "| **Owner** | Brandon |" in text

    def test_the_freshness_stamp_advances(self, client, tenant):
        _post(client, GOOD)
        assert "updated 2026-07-14" not in _cp_text(tenant)

    def test_bulleted_field_replaces_all_its_bullets(self, client, tenant):
        r = _post(client, {
            **GOOD,
            "fields": {"Where it stands": ["Pillars done.", "Hinkle overdue."]},
        })
        assert r.status_code == 200, r.text
        text = _cp_text(tenant)
        assert "- Pillars done." in text
        assert "Three of five pillars drafted" not in text
        assert "**Next up:** Finish pillars four and five." in text


class TestTheNoOp:
    def test_identical_content_does_not_commit(self, client, tenant):
        """Re-sending what the file already says must not manufacture freshness.

        The `· updated` stamp is what the staleness check reads, so a caller
        retrying after a timeout — or a scheduled one — must not make a
        months-old summary look current.
        """
        r = _post(client, {
            **GOOD,
            "fields": {"Status": "Per-pillar status from Joe's doc dump."},
        })
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["changed"] == []
        assert body["commit"] is None
        assert "updated 2026-07-14" in _cp_text(tenant)


class TestCallerErrors:
    def test_unknown_field_is_400_not_a_silent_no_write(self, client):
        r = _post(client, {**GOOD, "fields": {"Statuz": "typo"}})
        assert r.status_code == 400
        assert "unknown exec-summary field" in r.text

    def test_unknown_project_is_404(self, client):
        r = _post(client, {**GOOD, "project_code": "nope-9999"})
        assert r.status_code == 404

    def test_empty_fields_is_400(self, client):
        r = _post(client, {**GOOD, "fields": {}})
        assert r.status_code == 400

    def test_empty_value_is_refused(self, client):
        """An empty value would blank a field while reporting success."""
        r = _post(client, {**GOOD, "fields": {"Status": "  "}})
        assert r.status_code == 400
        assert "real prose" in r.text

    def test_missing_user_is_400(self, client):
        payload = {k: v for k, v in GOOD.items() if k != "user"}
        assert _post(client, payload).status_code == 400

    def test_wrong_value_type_is_400(self, client):
        r = _post(client, {**GOOD, "fields": {"Status": 42}})
        assert r.status_code == 400

    def test_a_bad_signature_is_refused_and_writes_nothing(self, client, tenant):
        body = json.dumps(GOOD).encode()
        r = client.post(
            "/api/project-state/capture",
            content=body,
            headers={"x-webhook-signature": "deadbeef"},
        )
        assert r.status_code in (401, 403)
        assert "All five pillars" not in _cp_text(tenant)


class TestTheRollOffIsReported:
    """#294: `roll_off_after_days` was accepted and never read. The report
    exists now, and the route is where a hosted wrap-up sees it — nothing
    deletes; the count is the cue to rotate where a checkout exists."""

    _WITH_UPDATES = CP_MD.replace(
        "**Blockers:** None.\n",
        "**Blockers:** None.\n"
        "**Updates:**\n"
        "- 2026-01-05 — An entry well past any roll-off threshold.\n"
        "- 2026-01-02 — Another one, older still.\n",
    )

    def _seed(self, tenant: Path) -> None:
        (tenant / "1p/google/ggl-5151-grc-narrative/cp.md").write_text(self._WITH_UPDATES)
        _git("add", "-A", cwd=tenant)
        _git("commit", "-m", "seed updates", cwd=tenant)
        _git("push", "origin", "main", cwd=tenant)

    def test_the_response_names_what_is_over_age(self, client, tenant):
        self._seed(tenant)
        r = _post(client, {**GOOD, "fields": {},
                           "updates_append": "A fresh entry that lands at the top of Updates."})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "Updates" in body["changed"]
        assert body["roll_off"]["count"] == 2
        assert body["roll_off"]["dates"] == ["2026-01-05", "2026-01-02"]
        assert body["roll_off"]["older_than"] < "2026-09"
        text = _cp_text(tenant)
        assert "2026-01-02 — Another one" in text, "roll-off must be reported, never performed"

    def test_a_no_op_append_still_reports(self, client, tenant):
        self._seed(tenant)
        r = _post(client, {**GOOD, "fields": {},
                           "updates_append": "An entry well past any roll-off threshold."})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["changed"] == [] and body["commit"] is None
        assert body["roll_off"]["count"] == 2

    def test_a_field_only_call_reports_nothing(self, client, tenant):
        r = _post(client, GOOD)
        assert r.status_code == 200
        assert r.json()["roll_off"] is None

    def test_a_region_without_updates_is_a_400_not_a_phantom_change(self, client, tenant):
        """The old path returned changed=True while writing nothing (#294)."""
        r = _post(client, {**GOOD, "fields": {}, "updates_append": "Nowhere for this to go."})
        assert r.status_code == 400, r.text
