"""A read after this server's own write must see the write (#353 item 6).

WHAT BROKE (10-09). `capture_project_state` returned ok — the webhook had
committed and pushed the new Exec Summary — and the very next
`get_project_state` served the PRE-write summary. The tree clone is pulled on
read behind a debounce (TREE_PULL_DEBOUNCE_SECONDS, 60s); the read that
oriented the write had just reset the clock, so the read-back skipped the
fetch and served the clone as it stood before the push.

WHAT THESE PIN. A successful tree-committing write expires the debounce, so
the next read fetches once and reflects it. A failed write does not, and the
debounce still holds for every read that follows no write of ours — the cost
stays one fetch per write.

Real git against a real file:// upstream, as in `test_tree_pull.py`: the bug
was a clock deciding whether git ran, and a mocked subprocess would hide it.

    python -m pytest prototypes/hosted-mcp/test_read_after_write.py -v
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

PROJ = Path("1p") / "google" / "ggl-5136-go-safety-website"


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def cp_md(status: str) -> str:
    return (
        "# ggl-5136\n\n<!-- cp-engine:start exec-summary -->\n"
        "## Exec Summary  ·  updated 2026-10-09\n\n"
        f"**Status:** {status}\n"
        "<!-- cp-engine:end exec-summary -->\n"
    )


def commit_status(upstream: Path, status: str) -> None:
    (upstream / PROJ / "cp.md").write_text(cp_md(status), encoding="utf-8")
    git("add", "-A", cwd=upstream)
    git("commit", "-qm", f"status {status}", cwd=upstream)


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "http://example.invalid")
    mp.setenv("SUPABASE_ANON_KEY", "x")
    import server as mod

    yield mod
    mp.undo()


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    up = tmp_path / "upstream"
    (up / PROJ).mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=up)
    git("config", "user.email", "t@example.com", cwd=up)
    git("config", "user.name", "t", cwd=up)
    commit_status(up, "Before the write")
    return up


@pytest.fixture
def hosted(server, monkeypatch, upstream):
    """The server's tree tools pointed at `upstream`, DB edges stubbed."""
    state: dict = {"root": None, "last_pull": 0.0}
    monkeypatch.setattr(server, "_TREE_STATE", state)
    monkeypatch.setattr(server, "TENANT_REPO", f"file://{upstream}")
    monkeypatch.setattr(server, "GIT_SSH_KEY", "")
    monkeypatch.setattr(server, "TREE_PULL_DEBOUNCE_SECONDS", 60)
    monkeypatch.setattr(server, "tree_available", lambda: (True, ""))
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(server, "user_client", lambda: object())
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    monkeypatch.setattr(server, "caller_subject", lambda: "drew")
    monkeypatch.setattr(server, "resolve_project_id", lambda c, code: None)
    monkeypatch.setattr(server, "find_project_dir", lambda root, code: root / PROJ)
    monkeypatch.setattr(server, "resolve_write_scope",
                        lambda c, code: {"id": "p", "kind": "project", "project_code": code})
    monkeypatch.setattr(server, "_project_codes_for_lint", lambda *a: None)
    monkeypatch.setattr(server, "_current_exec_stamp", lambda code: None)
    yield server
    root = state.get("root")
    if root:  # the conftest guard fails a module that leaks a clone
        shutil.rmtree(Path(root).parent, ignore_errors=True)


def status_read(server) -> str:
    out = server.get_project_state("ggl-5136")
    assert out.get("available") is True, out
    return out["exec_summary"]


def webhook_commits(server, monkeypatch, upstream, status, *, ok=True):
    """Stand in for mc-2 → webhook: on ok, the commit is PUSHED before return."""
    def fake_post(code, fields, entry):
        if ok:
            commit_status(upstream, status)
            return {"ok": True, "backend": {"changed": list(fields), "commit": "abc"}}
        return {"ok": False, "reason": "mc-2 refused"}

    monkeypatch.setattr(server, "call_mc2_capture_project_state", fake_post)


def test_read_back_after_own_write_is_current(hosted, monkeypatch, upstream):
    """THE CONTROL — the 10-09 shape. Unfixed, the read inside the debounce
    window serves "Before the write" though the write reported ok."""
    assert "Before the write" in status_read(hosted)

    webhook_commits(hosted, monkeypatch, upstream, "After the write")
    out = hosted.capture_project_state("ggl-5136", status="After the write")
    assert out["ok"] is True, out

    summary = status_read(hosted)
    assert "After the write" in summary, summary


def test_failed_write_keeps_the_debounce(hosted, monkeypatch, upstream):
    """A refused write committed nothing; it must not buy a fetch."""
    assert "Before the write" in status_read(hosted)
    webhook_commits(hosted, monkeypatch, upstream, "unused", ok=False)
    assert hosted.capture_project_state("ggl-5136", status="Nope")["ok"] is False
    pulled = hosted._TREE_STATE["last_pull"]
    status_read(hosted)
    assert hosted._TREE_STATE["last_pull"] == pulled


def test_unrelated_reads_stay_debounced(hosted, monkeypatch, upstream):
    """Bounded cost: one fetch per write, then the window holds again. A
    commit someone ELSE pushes is not seen until the window passes."""
    status_read(hosted)
    webhook_commits(hosted, monkeypatch, upstream, "After the write")
    hosted.capture_project_state("ggl-5136", status="After the write")
    assert "After the write" in status_read(hosted)

    commit_status(upstream, "Someone else's push")
    assert "After the write" in status_read(hosted)


@pytest.mark.parametrize("verb", [
    "capture_session", "log_improvement", "rotate_word_count", "promote_uphill",
])
def test_every_tree_committing_verb_expires_the_debounce(server, verb):
    """The same staleness follows every verb whose write is a tenant commit;
    guard the call sites so a new one is not missed silently."""
    import inspect

    src = inspect.getsource(getattr(server, verb))
    assert "_expire_tree_debounce()" in src, f"{verb} leaves the read-back stale"
