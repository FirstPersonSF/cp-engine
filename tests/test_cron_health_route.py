"""POST /api/cron/{job} — the daily health line, triggered by a Railway cron.

Each test FAILS on the code before this route existed (404 on every path) and
pins one property: signed vs unsigned, slot gating across DST, idempotent
retry, and a post that failed returning non-2xx with a failed run row.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import main as webhook_main  # noqa: E402
import run_ledger  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from routers import cron as cron_router  # noqa: E402

from cp_engine import daily_health  # noqa: E402
from cp_engine.clock import tenant_timezone  # noqa: E402

SECRET = "test-secret"
PDT_MORNING = datetime(2026, 9, 30, 12, 24, tzinfo=UTC).astimezone(tenant_timezone())
PST_MORNING = datetime(2026, 12, 2, 13, 24, tzinfo=UTC).astimezone(tenant_timezone())


class _Q:
    """A chainable query over one table's rows (eq / in_ honoured)."""

    def __init__(self, store, name):
        self.store, self.name, self._row, self._filters = store, name, None, []

    def insert(self, row):
        self._row = row
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        self._filters.append(lambda r: r.get(col) in vals)
        return self

    def gte(self, *_a):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a):
        return self

    def execute(self):
        if self._row is not None:
            self.store.setdefault(self.name, []).append(self._row)
            return type("R", (), {"data": [self._row]})()
        rows = [r for r in self.store.get(self.name, [])
                if all(f(r) for f in self._filters)]
        return type("R", (), {"data": rows})()


class _Ledger:
    def __init__(self):
        self.rows: dict[str, list[dict]] = {}

    def table(self, name):
        return _Q(self.rows, name)

    def runs(self):
        return [r for r in self.rows.get("webhook_runs", [])
                if r["route"] == "/api/cron/health"]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Signed requests, a fake ledger, a fake clone, a counted Slack post."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", SECRET)
    ledger = _Ledger()
    monkeypatch.setattr(run_ledger.mc2_db, "get_client", lambda *a, **kw: ledger)

    @contextlib.contextmanager
    def fake_clone(**_kw):
        yield tmp_path

    monkeypatch.setattr(cron_router.git_ops, "_read_only_clone", fake_clone)
    monkeypatch.setattr(cron_router.pipeline, "_load_tenant_config", lambda root: object())
    monkeypatch.setattr(daily_health, "gather", lambda **kw: daily_health.HealthReport(
        when=PDT_MORNING, checks=[daily_health.Check("Sync", True, "05:08 ok")]))
    posts: list[str] = []

    def fake_post(report, *, config, client, channel=None):
        posts.append(report.render())
        return f"1727.{len(posts):04d}"

    monkeypatch.setattr(daily_health, "post", fake_post)
    monkeypatch.setattr(cron_router, "_now", lambda: PDT_MORNING)
    cron_router._posted_memo.clear()
    return type("E", (), {"ledger": ledger, "posts": posts, "mp": monkeypatch})


def _post(body: dict, *, sign: bool = True, job: str = "health"):
    raw = json.dumps(body).encode()
    headers = {}
    if sign:
        ts = str(int(time.time()))
        headers = {"x-webhook-timestamp": ts,
                   "x-webhook-signature": hmac.new(
                       SECRET.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()}
    return TestClient(webhook_main.app, raise_server_exceptions=False).post(
        f"/api/cron/{job}", content=raw, headers=headers)


def test_unsigned_is_rejected_and_recorded_signed_posts(env):
    bad = _post({"slot": "23 12 * * *"}, sign=False)
    assert bad.status_code == 401 and env.posts == []
    assert [r["status"] for r in env.ledger.runs()] == ["rejected"]

    good = _post({"slot": "23 12 * * *"})
    assert good.status_code == 200, good.text
    assert good.json()["status"] == "posted" and len(env.posts) == 1
    row = env.ledger.runs()[-1]
    assert row["status"] == "ok"
    assert row["detail"]["outcome"] == "posted" and row["detail"]["posted_ts"] == "1727.0001"


def test_unknown_job_is_404_only_after_the_signature(env):
    assert _post({"slot": "23 12 * * *"}, sign=False, job="nope").status_code == 401
    assert _post({"slot": "23 12 * * *"}, job="nope").status_code == 404


@pytest.mark.parametrize("now,posting_slot,silent_slot", [
    (PDT_MORNING, "23 12 * * *", "23 13 * * *"),   # PDT: 12:23Z = 05:23
    (PST_MORNING, "23 13 * * *", "23 12 * * *"),   # PST: 13:23Z = 05:23
])
def test_exactly_one_of_the_two_slots_posts(env, now, posting_slot, silent_slot):
    env.mp.setattr(cron_router, "_now", lambda: now)
    skip = _post({"slot": silent_slot})
    assert skip.status_code == 200 and skip.json()["status"] == "skipped"
    assert env.posts == []
    hit = _post({"slot": posting_slot})
    assert hit.json()["status"] == "posted" and len(env.posts) == 1
    outcomes = [r["detail"].get("outcome") for r in env.ledger.runs()]
    assert outcomes == ["skipped", "posted"]


def test_a_retry_for_the_same_day_does_not_post_twice(env):
    first = _post({"slot": "23 12 * * *"})
    assert first.json()["status"] == "posted"
    second = _post({"slot": "23 12 * * *"})
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate"
    assert second.json()["posted_ts"] == first.json()["posted_ts"]
    assert len(env.posts) == 1


def test_the_ledger_alone_blocks_a_repost_after_a_restart(env):
    """The memo is process-local; the ledger row is what survives a redeploy."""
    assert _post({"slot": "23 12 * * *"}).json()["status"] == "posted"
    cron_router._posted_memo.clear()  # a new process
    assert _post({"slot": "23 12 * * *"}).json()["status"] == "duplicate"
    assert len(env.posts) == 1


def test_a_post_that_failed_is_non_2xx_and_a_failed_row(env):
    def boom(*a, **k):
        raise RuntimeError("channel_not_found")

    env.mp.setattr(daily_health, "post", boom)
    resp = _post({"slot": "23 12 * * *"})
    assert resp.status_code == 502
    assert "channel_not_found" in resp.json()["error"]
    row = env.ledger.runs()[-1]
    assert row["status"] == "failed" and "channel_not_found" in row["error"]
    # A failed post is not "posted": the next try is allowed to post.
    env.mp.setattr(daily_health, "post", lambda *a, **k: "1727.9999")
    assert _post({"slot": "23 12 * * *"}).json()["status"] == "posted"


def test_bad_slot_is_rejected(env):
    resp = _post({"slot": "*/5 * * * *"})
    assert resp.status_code == 400 and env.posts == []


def test_dry_run_renders_and_never_posts_or_marks_the_day(env):
    resp = _post({"slot": "23 13 * * *", "dry_run": True})
    assert resp.json()["status"] == "dry_run" and "Sync" in resp.json()["lines"]
    assert env.posts == []
    assert _post({"slot": "23 12 * * *"}).json()["status"] == "posted"
