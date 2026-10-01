"""POST /api/cron/sync and /api/cron/draft-summaries — the tenant's daily sync
and Monday Exec Summary drafts, moved off GitHub Actions' late scheduler.

Every test here FAILS on the code before these jobs existed (404: no such
job). Each pins one property:

- 202 `accepted` at once, the work in the background, and a completion row on
  ``/api/cron/<job>#background`` that says how it ended;
- per-slot idempotency: one run per slot + day — a second request while one is
  running, or after one finished, runs nothing; a FAILED run may be retried;
  the ledger alone holds after a restart;
- commit parity with the workflows: message and author/committer identical to
  sync.yml / draft-summaries.yml (the draft body is checked against the
  workflow's own Python, copied verbatim);
- the push is the webhook's rebase-or-fail path: a conflicting commit landed
  meanwhile fails the run and pushes nothing;
- draft gating: Monday before 10:00 tenant time, or nothing.

The tenant remote is a real bare git repo (`CP_TENANT_REPO_URL` = its path),
so clone, commit and push are real; only `sync_tenant` / `run_drafts` and the
config load are faked.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import main as webhook_main  # noqa: E402
import pipeline  # noqa: E402
import run_ledger  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from routers import cron as cron_router  # noqa: E402

from cp_engine import daily_health  # noqa: E402
from cp_engine.clock import tenant_timezone  # noqa: E402
from cp_engine.exec_summary_draft import DraftResult  # noqa: E402

SECRET = "test-secret"
TZ = tenant_timezone()
# 2026-10-05 is a Monday. 12:17Z = 05:17 PDT.
MONDAY_0517 = datetime(2026, 10, 5, 12, 17, tzinfo=UTC).astimezone(TZ)
MONDAY_1100 = datetime(2026, 10, 5, 18, 0, tzinfo=UTC).astimezone(TZ)
TUESDAY_0517 = datetime(2026, 10, 6, 12, 17, tzinfo=UTC).astimezone(TZ)
SYNC_SLOT = "0 14 * * *"
DRAFT_SLOT = "17 12 * * 1"
BOT = ("cp-engine-bot", "cp-engine-bot@users.noreply.github.com")


# ── fakes ────────────────────────────────────────────────────────────────


class _Q:
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
        rows = [r for r in self.store.get(self.name, []) if all(f(r) for f in self._filters)]
        return type("R", (), {"data": rows})()


class _Ledger:
    def __init__(self):
        self.rows: dict[str, list[dict]] = {}

    def table(self, name):
        return _Q(self.rows, name)

    def runs(self, route):
        return [r for r in self.rows.get("webhook_runs", []) if r["route"] == route]


def _git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def _person_env():
    return {**os.environ, "GIT_AUTHOR_NAME": "Drew", "GIT_AUTHOR_EMAIL": "d@x",
            "GIT_COMMITTER_NAME": "Drew", "GIT_COMMITTER_EMAIL": "d@x"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A bare tenant remote, signed requests, a fake ledger, captured spawns."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", SECRET)
    remote = tmp_path / "cp.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(remote), str(seed))
    (seed / "master-cp.md").write_text("# master\nline one\n", encoding="utf-8")
    _git(seed, "add", "-A", env=_person_env())
    _git(seed, "commit", "-q", "-m", "seed", env=_person_env())
    _git(seed, "push", "-q", "origin", "HEAD:main", env=_person_env())

    monkeypatch.setenv("CP_TENANT_REPO_URL", str(remote))
    monkeypatch.delenv("CP_TENANT_BRANCH", raising=False)
    monkeypatch.delenv("GIT_SSH_KEY", raising=False)
    # The webhook service sets these (Railway, 2026-10-01): the bot identity
    # must win over them, or the commit is authored as the webhook.
    monkeypatch.setenv("GIT_AUTHOR_NAME", "cp-engine-webhook")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "webhook@firstperson.is")

    ledger = _Ledger()
    monkeypatch.setattr(run_ledger.mc2_db, "get_client", lambda *a, **kw: ledger)
    monkeypatch.setattr(cron_router.pipeline, "_load_tenant_config",
                        lambda root: type("Cfg", (), {"root": root})())
    monkeypatch.setattr(cron_router, "_now", lambda: MONDAY_0517)
    spawned: list = []
    monkeypatch.setattr(pipeline, "_spawn_background", lambda coro: spawned.append(coro))
    # getattr: so the control run against the pre-change route fails on the
    # behaviour (404), not on fixture setup.
    getattr(cron_router, "_inflight", {}).clear()
    getattr(cron_router, "_done_memo", {}).clear()
    yield type("E", (), {"ledger": ledger, "spawned": spawned, "mp": monkeypatch,
                         "remote": remote, "seed": seed, "tmp": tmp_path})
    for coro in spawned:  # never leave an un-awaited coroutine behind
        coro.close()
    getattr(cron_router, "_inflight", {}).clear()
    getattr(cron_router, "_done_memo", {}).clear()


def _post(job: str, body: dict):
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return TestClient(webhook_main.app, raise_server_exceptions=False).post(
        f"/api/cron/{job}", content=raw,
        headers={"x-webhook-timestamp": ts, "x-webhook-signature": sig})


def _finish(env):
    """Run the newest spawned background tail to completion."""
    asyncio.run(env.spawned.pop())


def _remote_log(env, fmt="%s"):
    return _git(env.remote, "log", "-1", f"--format={fmt}", "main")


def _fake_sync(env, *, write=True, quarantine=False, before=None):
    def sync_tenant(config, *, dry_run=False, **_kw):
        if before:
            before(config.root)
        if write and not dry_run:
            p = config.root / "master-cp.md"
            p.write_text(p.read_text(encoding="utf-8") + "rendered\n", encoding="utf-8")
        if quarantine and not dry_run:
            q = config.root / "exceptions" / "region-edits"
            q.mkdir(parents=True)
            (q / "ggl-5168-carry-forward.md").write_text("an edit\n", encoding="utf-8")
        return type("R", (), {"projects_seen": 3, "files_written": (), "warnings": 0,
                              "warning_messages": ()})()
    env.mp.setattr("cp_engine.sync.sync_tenant", sync_tenant)


# ── sync: 202 + background completion row ─────────────────────────────────


def test_sync_answers_202_and_the_background_row_says_how_it_ended(env):
    _fake_sync(env)
    resp = _post("sync", {"slot": SYNC_SLOT})
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "accepted"
    (accepted,) = env.ledger.runs("/api/cron/sync")
    assert accepted["status"] == "accepted"
    assert accepted["detail"] == {"job": "sync", "slot": SYNC_SLOT, "day": "2026-10-05",
                                  "outcome": "accepted"}
    assert env.ledger.runs("/api/cron/sync#background") == []  # not yet run
    assert len(env.spawned) == 1

    _finish(env)
    (done,) = env.ledger.runs("/api/cron/sync#background")
    assert done["status"] == "ok", done
    assert done["detail"]["outcome"] == "pushed"
    assert done["detail"]["commit_sha"] == _remote_log(env, "%H")
    assert "rendered" in _git(env.remote, "show", "main:master-cp.md")
    assert cron_router._inflight == {}


def test_sync_commit_matches_the_workflow_message_and_bot_identity(env):
    _fake_sync(env)
    assert _post("sync", {"slot": SYNC_SLOT}).status_code == 202
    _finish(env)
    msg = _git(env.remote, "log", "-1", "--format=%B", "main")
    import re

    assert re.fullmatch(r"\[cp-sync\] \d{4}-\d\d-\d\dT\d\d:\d\dZ — auto-sync from MC-2", msg), msg
    assert _remote_log(env, "%an|%ae|%cn|%ce") == "|".join([*BOT, *BOT])
    # The health line's git fallback finds it (the prefix it greps).
    clone = env.tmp / "check"
    _git(env.tmp, "clone", "-q", str(env.remote), str(clone))
    when, sha = daily_health.last_sync_commit(clone)
    assert sha and when is not None


def test_sync_commit_message_is_the_workflows_date_format():
    # sync.yml: "[cp-sync] $(date -u +%Y-%m-%dT%H:%MZ) — auto-sync from MC-2"
    at = datetime(2026, 9, 30, 15, 8, tzinfo=TZ)  # 22:08Z
    assert cron_router.sync_commit_message(at) == "[cp-sync] 2026-09-30T22:08Z — auto-sync from MC-2"


def test_sync_with_nothing_changed_commits_nothing_and_ends_the_slot(env):
    _fake_sync(env, write=False)
    head = _remote_log(env, "%H")
    assert _post("sync", {"slot": SYNC_SLOT}).status_code == 202
    _finish(env)
    (done,) = env.ledger.runs("/api/cron/sync#background")
    assert done["status"] == "ok" and done["detail"]["outcome"] == "no_changes"
    assert _remote_log(env, "%H") == head
    assert _post("sync", {"slot": SYNC_SLOT}).json()["status"] == "duplicate"


def test_sync_region_edits_are_flagged_and_the_run_is_partial(env):
    _fake_sync(env, quarantine=True)
    assert _post("sync", {"slot": SYNC_SLOT}).status_code == 202
    _finish(env)
    (done,) = env.ledger.runs("/api/cron/sync#background")
    assert done["status"] == "partial"
    assert done["detail"]["region_edits"] == ["exceptions/region-edits/ggl-5168-carry-forward.md"]
    assert "managed-region edit" in done["error"]
    # Committed with the render, as the workflow's `git add -A` does.
    assert "an edit" in _git(env.remote, "show",
                             "main:exceptions/region-edits/ggl-5168-carry-forward.md")


def test_sync_push_conflict_fails_and_pushes_nothing(env):
    """Rebase-or-fail: a person's edit to the same line, landed while sync ran,
    wins. The run is a failed row, origin keeps the person's commit."""
    def person_edits_meanwhile(_root):
        other = env.tmp / "person"
        _git(env.tmp, "clone", "-q", str(env.remote), str(other))
        p = other / "master-cp.md"
        p.write_text(p.read_text(encoding="utf-8") + "a person's line\n", encoding="utf-8")
        _git(other, "commit", "-qam", "person", env=_person_env())
        _git(other, "push", "-q", "origin", "HEAD:main", env=_person_env())

    _fake_sync(env, before=person_edits_meanwhile)
    env.mp.setattr("git_ops._push_backoff_delay", lambda attempt: 0)
    assert _post("sync", {"slot": SYNC_SLOT}).status_code == 202
    _finish(env)
    (done,) = env.ledger.runs("/api/cron/sync#background")
    assert done["status"] == "failed" and done["detail"]["outcome"] == "failed"
    assert _remote_log(env) == "person"
    assert cron_router._inflight == {}
    # A failed slot is retryable.
    _fake_sync(env)
    assert _post("sync", {"slot": SYNC_SLOT}).status_code == 202


# ── sync: per-slot idempotency ────────────────────────────────────────────


def test_a_second_request_while_running_runs_nothing(env):
    _fake_sync(env)
    assert _post("sync", {"slot": SYNC_SLOT}).status_code == 202
    again = _post("sync", {"slot": SYNC_SLOT})
    assert again.status_code == 200 and again.json()["status"] == "running"
    assert len(env.spawned) == 1  # no second background run


def test_a_second_request_after_done_is_a_duplicate_other_slot_runs(env):
    _fake_sync(env)
    assert _post("sync", {"slot": SYNC_SLOT}).status_code == 202
    _finish(env)
    sha = _remote_log(env, "%H")
    dup = _post("sync", {"slot": SYNC_SLOT})
    assert dup.status_code == 200 and dup.json()["status"] == "duplicate"
    assert dup.json()["commit_sha"] == sha
    assert env.spawned == []
    # The 22:00 slot is its own run.
    assert _post("sync", {"slot": "0 22 * * *"}).status_code == 202


def test_the_ledger_alone_blocks_a_rerun_after_a_restart(env):
    _fake_sync(env)
    assert _post("sync", {"slot": SYNC_SLOT}).status_code == 202
    _finish(env)
    cron_router._done_memo.clear()  # a new process
    assert _post("sync", {"slot": SYNC_SLOT}).json()["status"] == "duplicate"


def test_the_same_slot_on_the_next_day_runs(env):
    _fake_sync(env)
    assert _post("sync", {"slot": SYNC_SLOT}).status_code == 202
    _finish(env)
    env.mp.setattr(cron_router, "_now", lambda: TUESDAY_0517)
    assert _post("sync", {"slot": SYNC_SLOT}).status_code == 202


def test_a_crashed_background_run_is_a_failed_row_and_releases_the_slot(env):
    def boom(config, **_kw):
        raise RuntimeError("MC-2 down")

    env.mp.setattr("cp_engine.sync.sync_tenant", boom)
    assert _post("sync", {"slot": SYNC_SLOT}).status_code == 202
    _finish(env)
    (done,) = env.ledger.runs("/api/cron/sync#background")
    assert done["status"] == "failed" and "MC-2 down" in done["error"]
    assert cron_router._inflight == {}


def test_sync_dry_run_commits_nothing_and_does_not_end_the_slot(env):
    _fake_sync(env)
    head = _remote_log(env, "%H")
    assert _post("sync", {"slot": SYNC_SLOT, "dry_run": True}).status_code == 202
    _finish(env)
    (done,) = env.ledger.runs("/api/cron/sync#background")
    assert done["detail"]["outcome"] == "dry_run" and _remote_log(env, "%H") == head
    assert _post("sync", {"slot": SYNC_SLOT}).status_code == 202


@pytest.mark.parametrize("slot", ["*/5 * * * *", "0 14 1 * *", "", None])
def test_bad_slot_is_rejected(env, slot):
    assert _post("sync", {"slot": slot}).status_code == 400
    assert env.spawned == []


# ── draft-summaries ───────────────────────────────────────────────────────


def _fake_drafts(env, results=None, *, write=True):
    seen = {}

    def run_drafts(config, *, now, apply, **_kw):
        seen["apply"], seen["now"] = apply, now
        if write and apply:
            cp = config.root / "ggl-5168" / "cp.md"
            cp.parent.mkdir(exist_ok=True)
            cp.write_text("drafted\n", encoding="utf-8")
        return results if results is not None else [
            DraftResult("ggl-5168", "stale 20d", "written", source_note="W39, W40"),
            DraftResult("sap-5171", "stale 30d", "rejected", detail="Next up: unsupported '5/27'"),
            DraftResult("ibx-5153", "current", "skipped"),
        ]
    env.mp.setattr("cp_engine.exec_summary_draft.run_drafts", run_drafts)
    return seen


@pytest.mark.parametrize("now,expected", [
    (MONDAY_0517, 202),
    (MONDAY_1100, 200),   # Monday, but past 10:00 tenant time
    (TUESDAY_0517, 200),  # same UTC slot time, wrong day
])
def test_drafts_gate_is_monday_before_ten_tenant_time(env, now, expected):
    _fake_drafts(env)
    env.mp.setattr(cron_router, "_now", lambda: now)
    resp = _post("draft-summaries", {"slot": DRAFT_SLOT})
    assert resp.status_code == expected, resp.text
    if expected == 200:
        assert resp.json()["status"] == "skipped" and env.spawned == []


def test_drafts_gate_matches_the_cli_flag():
    """The route and `cxp draft-summaries --planning-morning-only` share it."""
    from cp_engine.exec_summary_draft import is_planning_morning

    assert is_planning_morning(MONDAY_0517.replace(tzinfo=None))
    assert not is_planning_morning(MONDAY_1100.replace(tzinfo=None))
    assert not is_planning_morning(TUESDAY_0517.replace(tzinfo=None))


def test_drafts_commit_and_row(env):
    seen = _fake_drafts(env)
    assert _post("draft-summaries", {"slot": DRAFT_SLOT}).status_code == 202
    _finish(env)
    assert seen["apply"] is True
    (done,) = env.ledger.runs("/api/cron/draft-summaries#background")
    assert done["status"] == "ok" and done["detail"]["outcome"] == "pushed"
    assert done["detail"]["tally"] == {"written": 1, "rejected": 1, "skipped": 1}
    assert _remote_log(env, "%an|%ae|%cn|%ce") == "|".join([*BOT, *BOT])
    assert _remote_log(env, "%s") == \
        "[draft-summary] 1 Exec Summaries drafted from recent material (#251)"


# The commit body step of draft-summaries.yml, verbatim (the `BODY=$(python …)`
# heredoc), so parity is checked against the workflow, not a re-reading of it.
_WORKFLOW_BODY_PY = '''
import json
try:
    data = json.load(open("/tmp/draft-report.json"))
except Exception:
    raise SystemExit(0)
rows = data.get("results") or []
for r in rows:
    if r.get("outcome") == "written":
        print(f"- {r['code']}: from {r.get('source_note') or '—'}")
rejected = [r for r in rows if r.get("outcome") in ("rejected", "error")]
if rejected:
    print()
    print(f"NOT written ({len(rejected)}) — needs a person:")
    for r in rejected:
        print(f"- {r['code']}: {r['outcome']} — {r.get('detail', '')[:200]}")
'''


def _workflow_message(results, n, tmp_path):
    report = tmp_path / "draft-report.json"
    report.write_text(json.dumps({"results": [r.as_dict() for r in results]}), encoding="utf-8")
    body = subprocess.run([sys.executable, "-c",
                           _WORKFLOW_BODY_PY.replace("/tmp/draft-report.json", str(report))],
                          capture_output=True, text=True, check=True).stdout.rstrip("\n")
    # git commit -m SUBJECT -m "$BODY", then git's whitespace cleanup:
    repo = tmp_path / "msg"
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "commit", "-q", "--allow-empty", "-m",
         f"[draft-summary] {n} Exec Summaries drafted from recent material (#251)",
         "-m", body, env=_person_env())
    return _git(repo, "log", "-1", "--format=%B")


@pytest.mark.parametrize("results", [
    [DraftResult("a", "r", "written", source_note="W40"),
     DraftResult("b", "r", "written"),
     DraftResult("c", "r", "rejected", detail="x" * 300),
     DraftResult("d", "r", "error", detail="timeout")],
    [DraftResult("a", "r", "written", source_note="W39, W40")],
    [DraftResult("c", "r", "rejected", detail="Status: unsupported")],
])
def test_draft_commit_message_matches_the_workflow(tmp_path, results):
    ours_repo = tmp_path / "ours"
    _git(tmp_path, "init", "-q", str(ours_repo))
    _git(ours_repo, "commit", "-q", "--allow-empty", "-m",
         cron_router.draft_commit_message(results, 3), env=_person_env())
    ours = _git(ours_repo, "log", "-1", "--format=%B")
    assert ours == _workflow_message(results, 3, tmp_path)


def test_drafts_where_every_attempt_errored_fail_and_commit_nothing(env):
    head = _remote_log(env, "%H")
    _fake_drafts(env, [DraftResult("a", "r", "error", detail="ANTHROPIC_API_KEY not set"),
                       DraftResult("b", "r", "error", detail="ANTHROPIC_API_KEY not set")],
                 write=False)
    assert _post("draft-summaries", {"slot": DRAFT_SLOT}).status_code == 202
    _finish(env)
    (done,) = env.ledger.runs("/api/cron/draft-summaries#background")
    assert done["status"] == "failed" and "ANTHROPIC_API_KEY" in done["error"]
    assert _remote_log(env, "%H") == head
    assert _post("draft-summaries", {"slot": DRAFT_SLOT}).status_code == 202  # retryable


def test_drafts_dry_run_bypasses_the_gate_and_writes_nothing(env):
    seen = _fake_drafts(env)
    env.mp.setattr(cron_router, "_now", lambda: TUESDAY_0517)
    head = _remote_log(env, "%H")
    assert _post("draft-summaries", {"slot": DRAFT_SLOT, "dry_run": True}).status_code == 202
    _finish(env)
    assert seen["apply"] is False and _remote_log(env, "%H") == head
    (done,) = env.ledger.runs("/api/cron/draft-summaries#background")
    assert done["detail"]["outcome"] == "dry_run"


def test_drafts_one_run_per_slot(env):
    _fake_drafts(env)
    assert _post("draft-summaries", {"slot": DRAFT_SLOT}).status_code == 202
    assert _post("draft-summaries", {"slot": DRAFT_SLOT}).json()["status"] == "running"
    _finish(env)
    assert _post("draft-summaries", {"slot": DRAFT_SLOT}).json()["status"] == "duplicate"
    # The 13:17 shot is its own slot, as in the workflow (a no-op once the
    # 12:17 drafts landed: those summaries are current).
    assert _post("draft-summaries", {"slot": "17 13 * * 1"}).status_code == 202


def test_sync_and_drafts_do_not_block_each_other_in_flight(env):
    _fake_sync(env)
    _fake_drafts(env)
    assert _post("sync", {"slot": SYNC_SLOT}).status_code == 202
    assert _post("draft-summaries", {"slot": DRAFT_SLOT}).status_code == 202
