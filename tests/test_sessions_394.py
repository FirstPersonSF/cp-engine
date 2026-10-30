"""Several cp sessions at once (#394): lock, scoped commits, worktrees, registry.

Every git test runs against real git: a bare "remote", the tenant clone,
session worktrees made by `cxp session start`, and a third clone standing in
for the auto-ingest webhook pushing `cp:hash` bullets while sessions work.

The acceptance shape from the issue: two sessions on different workstreams,
both editing and wrapping up in either order, produce two clean pushes with
neither session's changes in the other's commit, and no webhook bullet lost;
concurrent syncs serialise.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from cp_engine import sessions
from cp_engine.cli import main
from cp_engine.merge_check import check_merge
from cp_engine.sessions import (
    LockHeld,
    SessionEntry,
    SessionError,
    commit_session,
    end_session,
    hook_register,
    hook_unregister,
    list_sessions,
    others_line,
    push_session,
    register,
    session_paths,
    start_session,
    tenant_lock,
)

REPO = Path(__file__).resolve().parents[1]

GGL = "1p/ggl-5216-google/ggl-5168-activation"
ACCOUNT = "1p/ggl-5216-google"
IBX = "1p/ibx-5153-ai-campaign"
WEEK = "sprints/2026-W45"

SPRINT_BODY = textwrap.dedent(
    """\
    # {code} — W45

    ### Decisions

    - (none yet)

    ### Meetings

    - kickoff <!-- cp:hash={h} -->
    """
)


def git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc.stdout.strip()


def _ident(repo: Path) -> None:
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """(remote, tenant clone, webhook clone) with a small seeded tree."""
    monkeypatch.setenv("CXP_SESSIONS_DIR", str(tmp_path / "sessions"))
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    _ident(seed)
    (seed / ".cp-engine.toml").write_text('[tenant]\nname = "test"\n')
    (seed / ".gitignore").write_text(".cp-engine.local.toml\n.cp-engine/*\n!.cp-engine/paths.json\n")
    (seed / "improvements.md").write_text("# Improvements\n")
    (seed / "master-cp.md").write_text("# Master\n\n## Decisions\n")
    for d, h in ((ACCOUNT, "a0000001"), (GGL, "b0000001"), (IBX, "c0000001")):
        (seed / d).mkdir(parents=True)
        (seed / d / "cp.md").write_text(f"# {Path(d).name}\n\nnotes\n")
    (seed / WEEK).mkdir(parents=True)
    for code, h in (("ggl-5168-activation", "b0000002"), ("ibx-5153-ai-campaign", "c0000002"),
                    ("ggl-5216-google", "a0000002")):
        (seed / WEEK / f"{code}.md").write_text(SPRINT_BODY.format(code=code, h=h))
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "seed")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "-u", "origin", "main")

    tenant = tmp_path / "cp"
    subprocess.run(["git", "clone", "-q", str(remote), str(tenant)], check=True)
    _ident(tenant)
    (tenant / ".cp-engine.local.toml").write_text("[local]\n")
    webhook = tmp_path / "webhook"
    subprocess.run(["git", "clone", "-q", str(remote), str(webhook)], check=True)
    _ident(webhook)
    return remote, tenant, webhook


def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text() + text)


def _webhook_bullet(webhook: Path, code: str, h: str) -> None:
    git(webhook, "pull", "-q", "--no-rebase")
    _append(webhook / WEEK / f"{code}.md", f"- webhook bullet <!-- cp:hash={h} -->\n")
    git(webhook, "commit", "-qam", f"[auto-ingest] {code}")
    git(webhook, "push", "-q")


def _hand_edit(root: Path, sprint_code: str, note: str) -> None:
    """A session's edit: a decision near the TOP of its sprint file (the
    webhook appends at the bottom, so the two merge cleanly)."""
    p = root / WEEK / f"{sprint_code}.md"
    p.write_text(p.read_text().replace("- (none yet)", f"- {note}"))


def _files_in(repo: Path, rev: str) -> set[str]:
    return set(git(repo, "show", "--name-only", "--format=", rev).split())


# ──────────────────────────────────────────────────────────────────────
#  1. Lock
# ──────────────────────────────────────────────────────────────────────

_HOLD = textwrap.dedent(
    """\
    import sys, time
    from pathlib import Path
    from cp_engine.sessions import tenant_lock
    with tenant_lock(Path(sys.argv[1]), purpose="cxp sync", project="ibx-5153"):
        print("held", flush=True)
        time.sleep(float(sys.argv[2]))
    """
)


def _hold_lock(root: Path, seconds: float = 30) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD, str(root), str(seconds)],
        stdout=subprocess.PIPE, text=True, env=env,
    )
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_lock_contention_names_the_holder(env):
    _, tenant, _ = env
    holder = _hold_lock(tenant)
    try:
        with pytest.raises(LockHeld) as info:
            with tenant_lock(tenant, purpose="cxp sync", wait=0.6, poll=0.1):
                pytest.fail("acquired a lock another process holds")
        assert info.value.holder.pid == holder.pid
        assert "ibx-5153" in str(info.value) and str(holder.pid) in str(info.value)
    finally:
        holder.kill()
        holder.wait()
    # Holder gone (killed, never released): the kernel freed the flock.
    with tenant_lock(tenant, purpose="cxp sync", wait=0):
        pass


def test_lock_is_shared_across_worktrees_of_one_clone(env):
    _, tenant, _ = env
    entry = start_session(tenant, "ibx-5153")
    holder = _hold_lock(Path(entry.worktree))
    try:
        with pytest.raises(LockHeld):
            with tenant_lock(tenant, purpose="cxp sync", wait=0.3, poll=0.1):
                pass
    finally:
        holder.kill()
        holder.wait()


def test_stale_lock_file_from_a_dead_pid_does_not_block(env):
    _, tenant, _ = env
    path = sessions.lock_path(tenant)
    assert path.parent.name == ".git", "the lock lives in the common git dir"
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    path.write_text(json.dumps({
        "pid": dead.pid, "host": "x", "purpose": "cxp sync", "project": None,
        "worktree": str(tenant),
        "started_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
    }))
    assert sessions.read_lock_holder(tenant) is None
    with tenant_lock(tenant, purpose="cxp sync", wait=0) as me:
        assert json.loads(path.read_text())["pid"] == me.pid == os.getpid()
    assert path.read_text() == "", "release clears the holder info"


def test_long_held_lock_is_reported_as_possibly_hung(env, monkeypatch):
    _, tenant, _ = env
    holder = _hold_lock(tenant)
    try:
        info = json.loads(sessions.lock_path(tenant).read_text())
        info["started_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        sessions.lock_path(tenant).write_text(json.dumps(info))
        with pytest.raises(LockHeld) as exc:
            with tenant_lock(tenant, purpose="cxp sync", wait=0.2, poll=0.1):
                pass
        assert exc.value.stale and f"kill {holder.pid}" in str(exc.value)
    finally:
        holder.kill()
        holder.wait()


# The CLI: sync refuses while another holds the lock; render --check doesn't
# take it. Uses test_render_check's fake-backend harness.

def test_cli_sync_refuses_while_locked_and_render_check_does_not_lock(tmp_path, monkeypatch):
    from tests.test_render_check import _setup

    root, _client, backend = _setup(tmp_path, monkeypatch)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    monkeypatch.setenv("CXP_LOCK_WAIT", "0.3")
    holder = _hold_lock(root)
    try:
        result = CliRunner().invoke(main, ["sync"])
        assert result.exit_code == 1
        assert "holds the tenant lock" in result.stderr and str(holder.pid) in result.stderr
        assert backend.reads == 0, "sync must not start under someone else's lock"

        result = CliRunner().invoke(main, ["render", "--check"])
        assert result.exit_code == 0, result.output
    finally:
        holder.kill()
        holder.wait()
    result = CliRunner().invoke(main, ["sync"])
    assert result.exit_code == 0, result.output
    assert backend.reads == 1


# ──────────────────────────────────────────────────────────────────────
#  2. Scoped commits
# ──────────────────────────────────────────────────────────────────────


def test_scoped_commit_excludes_another_sessions_edit(env):
    remote, tenant, _ = env
    # Session A (ggl-5168) and session B (ibx-5153) both editing ONE checkout.
    _append(tenant / GGL / "cp.md", "A's note\n")
    (tenant / GGL / "sessions").mkdir()
    (tenant / GGL / "sessions" / "2026-10-30-1200-drew.md").write_text("A session\n")
    _hand_edit(tenant, "ggl-5168-activation", "A decided")
    _append(tenant / "improvements.md", "- A friction\n")
    _append(tenant / IBX / "cp.md", "B's half-finished edit\n")
    _hand_edit(tenant, "ibx-5153-ai-campaign", "B half-decided")
    _append(tenant / ACCOUNT / "cp.md", "account edit by someone else\n")
    _append(tenant / "master-cp.md", "- B's cross-cutting decision\n")
    # B has even STAGED its file; A's commit must still leave it out.
    git(tenant, "add", f"{IBX}/cp.md")

    result = commit_session(tenant, ["ggl-5168"], "[session] ggl-5168: A")

    assert set(result.committed) == {
        f"{GGL}/cp.md", f"{GGL}/sessions/2026-10-30-1200-drew.md",
        f"{WEEK}/ggl-5168-activation.md", "improvements.md",
    }
    assert _files_in(tenant, "HEAD") == set(result.committed)
    left = set(git(tenant, "status", "--porcelain").replace("M ", "").split())
    assert {f"{IBX}/cp.md", f"{WEEK}/ibx-5153-ai-campaign.md",
            f"{ACCOUNT}/cp.md", "master-cp.md"} <= left
    assert set(result.left_alone) == {
        f"{IBX}/cp.md", f"{WEEK}/ibx-5153-ai-campaign.md",
        f"{ACCOUNT}/cp.md", "master-cp.md",
    }


def test_parent_session_does_not_sweep_up_a_nested_childs_files(env):
    _, tenant, _ = env
    _append(tenant / ACCOUNT / "cp.md", "account note\n")
    _append(tenant / GGL / "cp.md", "child session's edit\n")
    mine, others = session_paths(tenant, ["ggl-5216"])
    assert mine == [f"{ACCOUNT}/cp.md"]
    assert others == [f"{GGL}/cp.md"]
    # Naming both claims both; --path adds a hand-edited roll-up.
    _append(tenant / "master-cp.md", "- decision\n")
    mine, _ = session_paths(tenant, ["ggl-5216", "ggl-5168"], ["master-cp.md"])
    assert mine == sorted([f"{ACCOUNT}/cp.md", f"{GGL}/cp.md", "master-cp.md"])


def test_scoped_commit_stages_deletions_and_renames(env):
    _, tenant, _ = env
    (tenant / GGL / "old.md").write_text("x\n")
    git(tenant, "add", "-A")
    git(tenant, "commit", "-qm", "add old")
    git(tenant, "mv", f"{GGL}/old.md", f"{GGL}/new.md")
    result = commit_session(tenant, ["ggl-5168"], "rename")
    assert {f"{GGL}/old.md", f"{GGL}/new.md"} <= set(result.committed)
    assert git(tenant, "status", "--porcelain") == ""


def test_nothing_to_commit_is_not_an_error(env):
    _, tenant, _ = env
    _append(tenant / IBX / "cp.md", "other\n")
    result = commit_session(tenant, ["ggl-5168"], "noop")
    assert result.sha is None and result.left_alone == [f"{IBX}/cp.md"]


# ──────────────────────────────────────────────────────────────────────
#  3. Worktrees + push — the acceptance test
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("order", ["A-then-B", "B-then-A"])
def test_two_worktree_sessions_push_in_either_order_cleanly(env, order):
    remote, tenant, webhook = env
    a = start_session(tenant, "ggl-5168")
    b = start_session(tenant, "ibx-5153", fetch=False)
    wa, wb = Path(a.worktree), Path(b.worktree)
    assert wa.parent == wb.parent == Path(os.environ["CXP_SESSIONS_DIR"])
    assert (wa / ".cp-engine.local.toml").is_symlink()

    _append(wa / GGL / "cp.md", "A's work\n")
    _hand_edit(wa, "ggl-5168-activation", "A decided")
    _append(wb / IBX / "cp.md", "B's work\n")
    _hand_edit(wb, "ibx-5153-ai-campaign", "B decided")
    # The webhook writes both sprint files while both sessions work.
    _webhook_bullet(webhook, "ggl-5168-activation", "d0000001")
    _webhook_bullet(webhook, "ibx-5153-ai-campaign", "d0000002")

    first, second = ((wa, "ggl-5168"), (wb, "ibx-5153"))
    if order == "B-then-A":
        first, second = second, first
    shas = {}
    for wt, code in (first, second):
        res = commit_session(wt, [code], f"[session] {code}")
        shas[code] = res.sha
        pushed = push_session(wt)
        assert pushed.pushed and pushed.merged and pushed.target == "origin/main"
        # The webhook keeps writing between the two pushes.
        _webhook_bullet(webhook, "ggl-5168-activation", f"e{len(shas):07d}")

    git(tenant, "fetch", "-q")
    assert _files_in(tenant, shas["ggl-5168"]) == {f"{GGL}/cp.md", f"{WEEK}/ggl-5168-activation.md"}
    assert _files_in(tenant, shas["ibx-5153"]) == {f"{IBX}/cp.md", f"{WEEK}/ibx-5153-ai-campaign.md"}
    git(tenant, "merge", "-q", "--ff-only", "origin/main")
    lost, checked = check_merge(tenant, ref="origin/main")
    assert lost == [] and checked >= 2
    ggl = (tenant / WEEK / "ggl-5168-activation.md").read_text()
    ibx = (tenant / WEEK / "ibx-5153-ai-campaign.md").read_text()
    for h in ("b0000002", "d0000001", "e0000001", "e0000002"):
        assert f"cp:hash={h}" in ggl
    assert "A decided" in ggl and "B decided" not in ggl
    assert "cp:hash=d0000002" in ibx and "B decided" in ibx
    assert "A's work" in (tenant / GGL / "cp.md").read_text()
    assert "B's work" in (tenant / IBX / "cp.md").read_text()

    # Both end cleanly: their work is on origin/main.
    for wt in (wa, wb):
        entry, main_root = end_session(wt)
        assert not wt.exists()
        assert main_root == tenant.resolve()
        assert entry.branch not in git(tenant, "branch", "--list")
    assert list_sessions(tenant) == []


def test_push_stops_on_a_conflict_and_leaves_the_merge_for_the_session(env):
    _, tenant, webhook = env
    a = start_session(tenant, "ggl-5168")
    wa = Path(a.worktree)
    _hand_edit(wa, "ggl-5168-activation", "A decided")
    git(webhook, "pull", "-q")
    _hand_edit(webhook, "ggl-5168-activation", "someone else decided")
    git(webhook, "commit", "-qam", "conflicting")
    git(webhook, "push", "-q")
    commit_session(wa, ["ggl-5168"], "A")
    with pytest.raises(sessions.MergeConflict) as info:
        push_session(wa)
    assert info.value.files == [f"{WEEK}/ggl-5168-activation.md"]
    assert (Path(git(wa, "rev-parse", "--git-dir")) / "MERGE_HEAD").exists() or \
        git(wa, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False)


def test_end_refuses_unpushed_or_uncommitted_work(env):
    _, tenant, _ = env
    a = start_session(tenant, "ggl-5168")
    wa = Path(a.worktree)
    _append(wa / GGL / "cp.md", "unsaved\n")
    with pytest.raises(SessionError, match="uncommitted"):
        end_session(wa)
    commit_session(wa, ["ggl-5168"], "A")
    with pytest.raises(SessionError, match="not on origin/main"):
        end_session(wa)
    assert wa.exists()
    # By code from the main checkout, forced: gone, branch deleted.
    entry, _ = end_session(tenant, "ggl-5168", force=True)
    assert not wa.exists() and entry.branch not in git(tenant, "branch", "--list")


def test_capture_session_pushes_from_a_session_branch(env):
    """capture-session's own push used plain `git push`, which a
    `cp-session/…` branch tracking origin/main refuses."""
    from cp_engine.capture_session import _push_with_retry

    remote, tenant, _ = env
    wa = Path(start_session(tenant, "ggl-5168").worktree)
    _append(wa / GGL / "cp.md", "x\n")
    git(wa, "commit", "-qam", "[session] x")
    pushed, _ = _push_with_retry(wa)
    assert pushed
    assert git(tenant, "ls-remote", str(remote), "refs/heads/main").split()[0] == git(wa, "rev-parse", "HEAD")


# ──────────────────────────────────────────────────────────────────────
#  4. Registry
# ──────────────────────────────────────────────────────────────────────


def test_registry_add_list_prune(env, tmp_path):
    _, tenant, _ = env
    a = start_session(tenant, "ggl-5168")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    register(tenant, SessionEntry(id="claude:dead", worktree=str(tenant), kind="hook", pid=dead.pid))
    register(tenant, SessionEntry(id="claude:gone", worktree=str(tmp_path / "nope"), kind="hook", pid=os.getpid()))
    register(tenant, SessionEntry(
        id="claude:old", worktree=str(tenant), kind="hook", pid=None,
        started_at=(datetime.now(timezone.utc) - timedelta(days=2)).isoformat()))
    live = register(tenant, SessionEntry(id="claude:live", worktree=str(tenant), kind="hook", pid=os.getpid()))

    ids = {e.id for e in list_sessions(tenant)}
    assert ids == {a.id, live.id}
    raw = json.loads(sessions.registry_path(tenant).read_text())
    assert {d["id"] for d in raw} == ids, "pruned entries leave the file"
    # Every worktree of the clone reads the same registry.
    assert {e.id for e in list_sessions(Path(a.worktree))} == ids


def test_hook_register_attaches_to_a_worktree_entry_and_unregisters(env):
    _, tenant, _ = env
    a = start_session(tenant, "ggl-5168")
    e = hook_register(Path(a.worktree), "sess-1", pid=os.getpid())
    assert e.id == a.id and e.claude_session == "sess-1"
    h = hook_register(tenant, "sess-2", pid=os.getpid())
    assert h.kind == "hook" and h.id == "claude:sess-2"
    assert len(list_sessions(tenant)) == 2

    line = others_line(tenant, committed_only_mine=True)
    assert line == (
        f"1 other cp session active: ggl-5168-activation ({a.worktree}); "
        "only your files were committed."
    )
    line = others_line(Path(a.worktree), committed_only_mine=False)
    assert line.startswith("1 other cp session active: no project yet (")

    hook_unregister(tenant, "sess-2")
    hook_unregister(tenant, "sess-1")
    remaining = list_sessions(tenant)
    assert [x.id for x in remaining] == [a.id] and remaining[0].claude_session is None


def test_register_hook_cli_reads_the_hook_payload(env, monkeypatch):
    _, tenant, _ = env
    start_session(tenant, "ibx-5153")
    monkeypatch.chdir(tenant)
    monkeypatch.setattr(sessions, "_claude_pid", lambda start=None: os.getpid())
    payload = json.dumps({"session_id": "s-9", "cwd": str(tenant)})
    result = CliRunner().invoke(main, ["session", "register"], input=payload)
    assert result.exit_code == 0
    assert "1 other cp session active: ibx-5153-ai-campaign" in result.output
    assert any(e.id == "claude:s-9" for e in list_sessions(tenant))
    result = CliRunner().invoke(main, ["session", "register", "--remove"], input=payload)
    assert result.exit_code == 0
    assert not any(e.id == "claude:s-9" for e in list_sessions(tenant))


def test_session_cli_end_to_end(env, monkeypatch):
    _, tenant, webhook = env
    monkeypatch.chdir(tenant)
    r = CliRunner().invoke(main, ["session", "start", "ggl-5168"])
    assert r.exit_code == 0, r.output
    wt = Path(r.output.splitlines()[1].strip())
    assert wt.is_dir()
    r = CliRunner().invoke(main, ["session", "list"])
    assert "ggl-5168-activation" in r.output and "worktree" in r.output

    monkeypatch.chdir(wt)
    _append(wt / GGL / "cp.md", "work\n")
    _webhook_bullet(webhook, "ggl-5168-activation", "f0000001")
    r = CliRunner().invoke(main, ["session", "commit", "ggl-5168", "-m", "[session] ggl-5168"])
    assert r.exit_code == 0, r.output + (r.stderr or "")
    assert "Pushed to origin/main after merging it (merge-check clean)." in r.output
    r = CliRunner().invoke(main, ["session", "end"])
    assert r.exit_code == 0, r.output + (r.stderr or "")
    assert not wt.exists()
