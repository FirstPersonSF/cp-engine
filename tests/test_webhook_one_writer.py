"""Webhook as one serialized writer + managed-region guard (architecture plan step 2).

REAL GIT, ON PURPOSE (same reasoning as test_webhook_append_conflict_reapply):
a mocked ``subprocess.run`` can assert a command sequence but not whether two
appends under one heading actually conflict, or what reaches origin.

CONTROLS — run against origin/main before this change, both FAIL there:

* ``test_two_overlapping_writes_to_one_sprint_file_both_land`` — two
  requests clone the same tip and append under the same heading. Without the
  lock the second push's rebase conflicts and that request raises; its bullet
  exists nowhere.
* ``test_foreign_edit_inside_a_managed_region_is_reverted_and_preserved`` —
  a route that writes inside a managed region had that write committed as-is
  (and destroyed by the next render).
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
import threading
from pathlib import Path

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import git_ops  # noqa: E402

_SPRINT_REL = "sprints/2026-W40/ggl-5151.md"
_CP_REL = "1p/google/ggl-5151-grc-narrative/cp.md"


def _digest_line(body: str) -> str:
    # Mirrors cp_engine.region_guard.digest WITHOUT importing it, so this file
    # also runs (as the control) against an engine that predates it.
    norm = "\n".join(ln.rstrip() for ln in body.splitlines()).strip("\n")
    return f"<!-- cp-engine:digest {hashlib.sha256(norm.encode()).hexdigest()[:12]} -->"


_FACTS = "| Sessions this week | 2 |"
_SPRINT = f"""# GGL 5151 — W40

<!-- cp-engine:start sprint-facts -->
{_FACTS}
{_digest_line(_FACTS)}
<!-- cp-engine:end sprint-facts -->

## Meeting notes

- seed bullet
"""

_CP = """# GGL 5151

<!-- cp-engine:start exec-summary -->
**Status:** seeded
<!-- cp-engine:end exec-summary -->
"""


def _git(*args: str, cwd: Path) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return r.stdout


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(git_ops.time, "sleep", lambda _s: None)


@pytest.fixture
def origin(tmp_path: Path, monkeypatch) -> Path:
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _git("init", "--bare", "--initial-branch=main", cwd=bare)
    seed = tmp_path / "seed"
    _git("clone", "--quiet", str(bare), str(seed), cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=seed)
    _git("config", "user.name", "T", cwd=seed)
    (seed / ".cp-engine.toml").write_text("[tenant]\nname = 'test'\n")
    (seed / _SPRINT_REL).parent.mkdir(parents=True)
    (seed / _SPRINT_REL).write_text(_SPRINT)
    (seed / _CP_REL).parent.mkdir(parents=True)
    (seed / _CP_REL).write_text(_CP)
    _git("add", "-A", cwd=seed)
    _git("commit", "-m", "seed", cwd=seed)
    _git("push", "--quiet", "-u", "origin", "main", cwd=seed)
    monkeypatch.setenv("CP_TENANT_REPO_URL", f"file://{bare}")
    monkeypatch.delenv("CP_TENANT_BRANCH", raising=False)
    monkeypatch.delenv("GIT_SSH_KEY", raising=False)
    return bare


def _origin(origin: Path, rel: str) -> str:
    return _git("show", f"main:{rel}", cwd=origin)


def _append_under_notes(root: Path, bullet: str) -> None:
    p = root / _SPRINT_REL
    p.write_text(p.read_text().replace("## Meeting notes\n\n", f"## Meeting notes\n\n- {bullet}\n", 1))


# ── serialization ──────────────────────────────────────────────────────────


def test_two_overlapping_writes_to_one_sprint_file_both_land(origin: Path) -> None:
    """CONTROL (fails on origin/main). Two deliveries overlap: each clones,
    appends under the same heading, then pushes. The barrier forces the old
    code into the worst interleaving (both clone before either pushes); with
    the lock the second cannot clone until the first has pushed, so its
    barrier wait times out and it proceeds on the new tip."""
    barrier = threading.Barrier(2, timeout=1.5)
    errors: list[BaseException] = []

    def write(bullet: str) -> None:
        try:
            with git_ops._cloned_tenant() as root:
                _append_under_notes(root, bullet)
                try:
                    barrier.wait()
                except threading.BrokenBarrierError:
                    pass
                git_ops._commit_with_message_and_push(root, f"[auto-ingest] {bullet}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(b,)) for b in ("from A", "from B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert errors == [], errors
    text = _origin(origin, _SPRINT_REL)
    assert "- from A" in text and "- from B" in text


def test_a_coroutine_holding_a_clone_across_await_does_not_deadlock_the_loop(
    origin: Path,
) -> None:
    """Two routes hold a clone open across an `await` (spine promote). A
    second coroutine on the same loop must not block on that lock — it would
    never be released. It proceeds unlocked (rebase-or-fail) instead."""
    done: list[str] = []

    async def holder() -> None:
        with git_ops._cloned_tenant():
            await asyncio.sleep(0.3)
        done.append("holder")

    async def second() -> None:
        await asyncio.sleep(0.05)
        with git_ops._cloned_tenant():
            pass
        done.append("second")

    async def main() -> None:
        await asyncio.wait_for(asyncio.gather(holder(), second()), timeout=20)

    asyncio.run(main())
    assert sorted(done) == ["holder", "second"]


def test_worker_thread_waits_for_the_loop_holder(origin: Path) -> None:
    order: list[str] = []

    async def main() -> None:
        with git_ops._cloned_tenant():
            fut = asyncio.ensure_future(asyncio.to_thread(_worker))
            await asyncio.sleep(0.3)
            order.append("loop-release")
        await asyncio.wait_for(fut, timeout=20)

    def _worker() -> None:
        with git_ops._cloned_tenant():
            order.append("worker-in")

    asyncio.run(main())
    assert order == ["loop-release", "worker-in"]


# ── region guard ───────────────────────────────────────────────────────────


def test_foreign_edit_inside_a_managed_region_is_reverted_and_preserved(origin: Path) -> None:
    """CONTROL (fails on origin/main). A route writes a line INSIDE
    `sprint-facts` (the #263 shape) next to a legitimate hand-section bullet."""
    with git_ops._cloned_tenant() as root:
        p = root / _SPRINT_REL
        p.write_text(p.read_text().replace(
            "<!-- cp-engine:end sprint-facts -->",
            "- Account summary that belongs outside\n<!-- cp-engine:end sprint-facts -->", 1,
        ))
        _append_under_notes(root, "legit bullet")
        git_ops._commit_with_message_and_push(root, "[auto-ingest] ggl-5151: meeting x")

    text = _origin(origin, _SPRINT_REL)
    assert "- legit bullet" in text                       # the route's real write lands
    assert "Account summary that belongs outside" not in text  # region reverted
    listing = _git("ls-tree", "-r", "--name-only", "main", cwd=origin)
    quarantined = [ln for ln in listing.splitlines() if ln.startswith("exceptions/region-edits/")]
    assert len(quarantined) == 1, listing
    assert "Account summary that belongs outside" in _origin(origin, quarantined[0])
    msg = _git("log", "-1", "--format=%B", "main", cwd=origin)
    assert "Region-Guard-Reverted: " + _SPRINT_REL + "#sprint-facts" in msg


def test_engine_render_inside_the_clone_is_committed_as_is(origin: Path) -> None:
    from cp_engine.render import splice_managed_region

    with git_ops._cloned_tenant() as root:
        p = root / _SPRINT_REL
        p.write_text(splice_managed_region(p.read_text(), "sprint-facts",
                                           "| Sessions this week | 3 |", source=p))
        git_ops._commit_with_message_and_push(root, "[auto-ingest] render")
    text = _origin(origin, _SPRINT_REL)
    assert "| Sessions this week | 3 |" in text
    assert "exceptions/region-edits" not in _git("ls-tree", "-r", "--name-only", "main", cwd=origin)


def test_exec_summary_writes_pass_through(origin: Path) -> None:
    with git_ops._cloned_tenant() as root:
        p = root / _CP_REL
        p.write_text(p.read_text().replace("**Status:** seeded", "**Status:** written by capture_project_state"))
        git_ops._commit_with_message_and_push(root, "[project-state] status")
    assert "written by capture_project_state" in _origin(origin, _CP_REL)


def test_sparse_clone_still_commits_the_quarantine(origin: Path) -> None:
    """project-state / sessions / rotation clone sparse; exceptions/ is
    outside the cone, so the preserved text must be staged explicitly."""
    with git_ops._cloned_tenant(sparse_paths=["sprints"]) as root:
        p = root / _SPRINT_REL
        p.write_text(p.read_text().replace(
            "<!-- cp-engine:end sprint-facts -->", "- stray\n<!-- cp-engine:end sprint-facts -->", 1
        ))
        git_ops._commit_with_message_and_push(root, "[session] x")
    listing = _git("ls-tree", "-r", "--name-only", "main", cwd=origin)
    assert any(ln.startswith("exceptions/region-edits/") for ln in listing.splitlines()), listing
    assert "- stray" not in _origin(origin, _SPRINT_REL)
