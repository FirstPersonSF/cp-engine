"""Concurrent tail-appends re-apply on rebase conflict (cp-engine #290).

THE DEFECT. `_push_with_retry` recovers from a lost push race with `pull
--rebase`, which only helps when the two writers touched DIFFERENT lines. Two
appends to the tail of `improvements.md` — or two `updates_append` to one
cp.md, both inserting under `**Updates:**` — conflict every time. The old path
aborted the rebase, re-raised, the temp clone was deleted, and the route
answered 502 "entry written but the push failed". The loser's entry existed
nowhere.

REAL GIT, ON PURPOSE. The sibling `test_webhook_push_with_retry.py` mocks
`subprocess.run`, which can assert the command SEQUENCE but cannot tell you
whether a tail append actually conflicts (it does — `UU improvements.md`) or
whether the recovery leaves BOTH entries on origin. So: a bare origin, the
webhook's clone, and a second writer's clone that pushes in the window between
the webhook's clone and its push.
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
from fastapi.testclient import TestClient

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import git_ops  # noqa: E402
import main as webhook_main  # noqa: E402

from cp_engine.exec_summary_merge import append_update_entry  # noqa: E402
from cp_engine.improvements import append_entry  # noqa: E402

_LOG = """# Improvements log

Protocol prose.

- 2026-09-16 · `sprints parser` — An earlier observation that must survive.
"""

_CP_MD = """---
Project: GGL 5151 GRC Narrative
Filename: ggl-5151.md
---

## Exec Summary  ·  updated 2026-07-14

<!-- cp-engine:start exec-summary -->
**Last session:** 2026-09-01
**Objective:** Land the GRC narrative.
**Status:** Per-pillar status from Joe's doc dump.
**Updates:**
- 2026-09-01 — Seed entry.
**Next up:** Finish pillars four and five.
**Blockers:** None.
<!-- cp-engine:end exec-summary -->
"""

_CP_REL = "1p/google/ggl-5151-grc-narrative/cp.md"


def _git(*args: str, cwd: Path) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return r.stdout


def _clone(origin: Path, dest: Path) -> Path:
    _git("clone", "--quiet", str(origin), str(dest), cwd=origin.parent)
    _git("config", "user.email", "test@example.com", cwd=dest)
    _git("config", "user.name", "Test", cwd=dest)
    return dest


def _origin_text(origin: Path, rel: str) -> str:
    return _git("show", f"main:{rel}", cwd=origin)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(git_ops.time, "sleep", lambda _s: None)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare origin seeded with improvements.md and one cp.md."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=bare)

    seed = _clone(bare, tmp_path / "seed")
    (seed / ".cp-engine.toml").write_text("[tenant]\nname = 'test'\n")
    (seed / "improvements.md").write_text(_LOG)
    (seed / _CP_REL).parent.mkdir(parents=True)
    (seed / _CP_REL).write_text(_CP_MD)
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "seed", cwd=seed)
    _git("push", "--quiet", "-u", "origin", "main", cwd=seed)
    return bare


@pytest.fixture
def webhook_clone(origin: Path, tmp_path: Path) -> Path:
    """The clone the webhook would make — taken BEFORE the second writer pushes."""
    return _clone(origin, tmp_path / "webhook")


def _second_writer_appends_improvement(origin: Path, tmp_path: Path, text: str) -> None:
    """Another hosted user's append lands on origin between our clone and push."""
    other = _clone(origin, tmp_path / "other")
    path = other / "improvements.md"
    updated, changed = append_entry(path.read_text(), "other-area", text, today=date(2026, 9, 17))
    assert changed
    path.write_text(updated)
    _git("commit", "-am", "[improvements] other-area (tony)", cwd=other)
    _git("push", "--quiet", "origin", "main", cwd=other)


def _second_writer_appends_update(origin: Path, tmp_path: Path, text: str) -> None:
    other = _clone(origin, tmp_path / "other")
    path = other / _CP_REL
    updated, changed = append_update_entry(path.read_text(), text, today=date(2026, 9, 17))
    assert changed
    path.write_text(updated)
    _git("commit", "-am", "[project-state] Updates (tony)", cwd=other)
    _git("push", "--quiet", "origin", "main", cwd=other)


# --- the git helper itself ---------------------------------------------------


class TestCommitWithReapply:
    def test_the_race_really_conflicts(self, origin, webhook_clone, tmp_path, monkeypatch):
        """Establish the premise: a tail append vs a tail append is NOT a
        clean rebase. Without this the rest of the file could pass on a
        recovery path that never runs."""
        monkeypatch.setattr(git_ops, "_ssh_env", dict)
        path = webhook_clone / "improvements.md"
        path.write_text(
            append_entry(
                path.read_text(), "ours", "Our observation, twenty chars.", today=date(2026, 9, 17)
            )[0]
        )
        _second_writer_appends_improvement(origin, tmp_path, "Their observation, twenty chars.")

        # Today's behaviour (no reapply): abort + raise. Preserved for the
        # non-append callers.
        with pytest.raises(subprocess.CalledProcessError):
            git_ops._commit_with_message_and_push(webhook_clone, "[improvements] ours (drew)")
        assert "Our observation" not in _origin_text(origin, "improvements.md")

    def test_reapply_lands_both_entries_on_origin(
        self, origin, webhook_clone, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(git_ops, "_ssh_env", dict)
        path = webhook_clone / "improvements.md"

        def do_append() -> bool:
            updated, changed = append_entry(
                path.read_text(), "ours", "Our observation, twenty chars.", today=date(2026, 9, 17)
            )
            if changed:
                path.write_text(updated)
            return changed

        assert do_append()
        _second_writer_appends_improvement(origin, tmp_path, "Their observation, twenty chars.")

        sha = git_ops._commit_with_message_and_push(
            webhook_clone, "[improvements] ours (drew)", reapply=do_append
        )
        assert sha
        text = _origin_text(origin, "improvements.md")
        assert "`ours` — Our observation, twenty chars." in text
        assert "`other-area` — Their observation, twenty chars." in text
        # The earlier entry survived and nothing was duplicated.
        assert text.count("An earlier observation that must survive.") == 1
        assert text.count("Our observation") == 1
        assert text.count("Their observation") == 1
        # Origin's tip is OUR recovered commit, with the same message.
        assert _git("log", "-1", "--format=%s", cwd=origin).strip() == "[improvements] ours (drew)"
        assert _git("rev-parse", "main", cwd=origin).strip() == sha

    def test_no_conflict_markers_leak(self, origin, webhook_clone, tmp_path, monkeypatch):
        monkeypatch.setattr(git_ops, "_ssh_env", dict)
        path = webhook_clone / "improvements.md"

        def do_append() -> bool:
            updated, changed = append_entry(
                path.read_text(), "ours", "Our observation, twenty chars.", today=date(2026, 9, 17)
            )
            if changed:
                path.write_text(updated)
            return changed

        do_append()
        _second_writer_appends_improvement(origin, tmp_path, "Their observation, twenty chars.")
        git_ops._commit_with_message_and_push(webhook_clone, "m", reapply=do_append)
        text = _origin_text(origin, "improvements.md")
        assert "<<<<<<<" not in text and ">>>>>>>" not in text

    def test_winner_already_logged_the_same_entry(
        self, origin, webhook_clone, tmp_path, monkeypatch
    ):
        """The dedupe fires on re-apply: nothing to push, and that is success
        — the caller's content IS on origin."""
        monkeypatch.setattr(git_ops, "_ssh_env", dict)
        path = webhook_clone / "improvements.md"

        def do_append() -> bool:
            updated, changed = append_entry(
                path.read_text(),
                "other-area",
                "Same observation, twenty chars.",
                today=date(2026, 9, 17),
            )
            if changed:
                path.write_text(updated)
            return changed

        assert do_append()
        _second_writer_appends_improvement(origin, tmp_path, "Same observation, twenty chars.")
        git_ops._commit_with_message_and_push(webhook_clone, "m", reapply=do_append)
        text = _origin_text(origin, "improvements.md")
        assert text.count("Same observation") == 1
        assert "<<<<<<<" not in text

    def test_exhaustion_still_raises(self, origin, webhook_clone, tmp_path, monkeypatch):
        """A writer that keeps landing ahead of us exhausts max_attempts and
        raises — bounded, and the 502 survives for the genuinely stuck case."""
        monkeypatch.setattr(git_ops, "_ssh_env", dict)
        path = webhook_clone / "improvements.md"
        n = [0]

        def do_append() -> bool:
            # Every re-apply is immediately beaten by another push.
            n[0] += 1
            _second_writer_appends_improvement(
                origin, tmp_path / f"w{n[0]}", f"Concurrent writer number {n[0]} landed."
            )
            updated, changed = append_entry(
                path.read_text(), "ours", "Our observation, twenty chars.", today=date(2026, 9, 17)
            )
            if changed:
                path.write_text(updated)
            return changed

        path.write_text(
            append_entry(
                path.read_text(), "ours", "Our observation, twenty chars.", today=date(2026, 9, 17)
            )[0]
        )
        _second_writer_appends_improvement(
            origin, tmp_path / "w0", "Concurrent writer number 0 landed."
        )
        with pytest.raises(subprocess.CalledProcessError):
            git_ops._commit_with_message_and_push(webhook_clone, "m", reapply=do_append)
        assert n[0] == git_ops._PUSH_MAX_ATTEMPTS - 1


# --- the routes, end to end --------------------------------------------------


def _signed(body: bytes, *, secret: bytes = b"test-secret") -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def _post(client: TestClient, route: str, payload: dict):
    body = json.dumps(payload).encode()
    return client.post(route, content=body, headers={"x-webhook-signature": _signed(body)})


@pytest.fixture
def racing_client(origin: Path, webhook_clone: Path, tmp_path: Path, monkeypatch):
    """A client whose clone is stale by one push: the second writer lands
    between the (fake) clone and the route's push — the exact window #290 is
    about. `race` is set by the test to the second writer's action."""
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    state: dict = {"race": None}

    @contextmanager
    def _fake_clone(sparse_paths=None):
        if state["race"] is not None:
            state["race"]()
        yield webhook_clone

    monkeypatch.setattr(git_ops, "_cloned_tenant", _fake_clone)
    monkeypatch.setattr(git_ops, "_ssh_env", dict)
    return TestClient(webhook_main.app), state


class TestImprovementsRoute:
    def test_loser_entry_lands_alongside_the_winner(self, racing_client, origin, tmp_path):
        client, state = racing_client
        state["race"] = lambda: _second_writer_appends_improvement(
            origin, tmp_path, "Their observation, twenty chars."
        )
        r = _post(
            client,
            "/api/improvements/append",
            {
                "area": "hosted-mcp",
                "observation": "A tenant file was readable but not writable from a hosted session.",
                "user": "drew",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["changed"] is True and body["commit"]

        text = _origin_text(origin, "improvements.md")
        assert "`hosted-mcp` — A tenant file was readable but not writable" in text
        assert "`other-area` — Their observation, twenty chars." in text
        assert text.count("An earlier observation that must survive.") == 1
        assert "<<<<<<<" not in text
        assert _git("rev-parse", "main", cwd=origin).strip() == body["commit"]


class TestProjectStateRoute:
    def test_append_only_call_recovers(self, racing_client, origin, tmp_path):
        client, state = racing_client
        state["race"] = lambda: _second_writer_appends_update(
            origin, tmp_path, "Tony's session delta."
        )
        r = _post(
            client,
            "/api/project-state/capture",
            {
                "project_code": "ggl-5151-grc-narrative",
                "user": "drew",
                "updates_append": "Drew's session delta.",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["changed"] == ["Updates"]
        text = _origin_text(origin, _CP_REL)
        assert "— Drew's session delta." in text
        assert "— Tony's session delta." in text
        assert "— Seed entry." in text
        assert "<<<<<<<" not in text

    def test_field_replace_keeps_the_502_and_says_retry(self, racing_client, origin, tmp_path):
        """A call that REPLACES a field does not get auto-resolved: two people
        disagreeing about the same prose is not ours to settle. But the 502
        now tells the caller what to do."""
        client, state = racing_client

        def other_writes_status():
            other = _clone(origin, tmp_path / "other")
            p = other / _CP_REL
            p.write_text(p.read_text().replace("**Status:** Per-pillar", "**Status:** Tony's"))
            _git("commit", "-am", "tony status", cwd=other)
            _git("push", "--quiet", "origin", "main", cwd=other)

        state["race"] = other_writes_status
        r = _post(
            client,
            "/api/project-state/capture",
            {
                "project_code": "ggl-5151-grc-narrative",
                "user": "drew",
                "fields": {"Status": "Drew's status, in conflict."},
            },
        )
        assert r.status_code == 502, r.text
        assert "retry" in r.json()["detail"]
        assert "Tony's" in _origin_text(origin, _CP_REL)
        assert "Drew's status" not in _origin_text(origin, _CP_REL)
