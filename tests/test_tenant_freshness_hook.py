# tests/test_tenant_freshness_hook.py — the SessionStart tenant-freshness
# gate (cp-engine #80). Subprocess-driven against real throwaway git repos,
# mirroring test_webhook_observability's sync-cli-version shim tests.
import subprocess
from pathlib import Path

_HOOK = (
    Path(__file__).resolve().parent.parent
    / "plugin" / "hooks" / "tenant-freshness.sh"
)


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
             "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
             "HOME": str(cwd)},
    )
    return out.stdout


def _run_hook(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_HOOK)], cwd=cwd, capture_output=True, text=True,
        timeout=30,
    )


def _make_pair(tmp_path: Path) -> tuple[Path, Path]:
    """origin (bare) + a tenant clone with .cp-engine.toml committed."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "--bare", "--initial-branch=main", ".")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=main", ".")
    (seed / ".cp-engine.toml").write_text("[engine]\n")
    (seed / "master-cp.md").write_text("# master\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-u", "origin", "main")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    _git(clone, "branch", "--set-upstream-to=origin/main", "main")
    return origin, clone


def _push_remote_commit(tmp_path: Path, origin: Path, n: int = 1) -> None:
    """Advance origin/main by n commits via a second clone."""
    other = tmp_path / f"other-{n}"
    _git(tmp_path, "clone", str(origin), str(other))
    for i in range(n):
        (other / f"remote-{n}-{i}.md").write_text("x\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-m", f"remote {n}.{i}")
    _git(other, "push", "origin", "main")


def test_noop_outside_tenant(tmp_path: Path) -> None:
    """No .cp-engine.toml up the tree → silent exit 0, no git calls needed."""
    workdir = tmp_path / "plain"
    workdir.mkdir()
    out = _run_hook(workdir)
    assert out.returncode == 0
    assert out.stdout.strip() == ""


def test_current_clone_is_silent(tmp_path: Path) -> None:
    _, clone = _make_pair(tmp_path)
    out = _run_hook(clone)
    assert out.returncode == 0
    assert out.stdout.strip() == ""


def test_clean_behind_fast_forwards(tmp_path: Path) -> None:
    origin, clone = _make_pair(tmp_path)
    _push_remote_commit(tmp_path, origin, n=3)
    out = _run_hook(clone)
    assert out.returncode == 0
    assert "fast-forwarded 3 commit(s)" in out.stdout
    # The pull actually happened.
    assert (clone / "remote-3-0.md").exists()


def test_dirty_and_behind_warns_without_pulling(tmp_path: Path) -> None:
    """Marcello's shape: dirty tree + behind → loud warning, files untouched."""
    origin, clone = _make_pair(tmp_path)
    _push_remote_commit(tmp_path, origin, n=2)
    (clone / "master-cp.md").write_text("# locally modified\n")
    out = _run_hook(clone)
    assert out.returncode == 0
    assert "TENANT CLONE STALE" in out.stdout
    assert "2 commit(s) behind" in out.stdout
    assert "1 uncommitted file(s)" in out.stdout
    # No pull happened; local modification intact.
    assert (clone / "master-cp.md").read_text() == "# locally modified\n"
    assert not (clone / "remote-2-0.md").exists()


def test_diverged_warns_even_when_clean(tmp_path: Path) -> None:
    """Ahead + behind (the unpushed-local-commit shape) → warn, never pull."""
    origin, clone = _make_pair(tmp_path)
    _push_remote_commit(tmp_path, origin, n=1)
    (clone / "local.md").write_text("local\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-m", "local unpushed")
    out = _run_hook(clone)
    assert out.returncode == 0
    assert "TENANT CLONE STALE" in out.stdout
    assert "1 ahead" in out.stdout


def test_hook_runs_from_subdirectory(tmp_path: Path) -> None:
    """The gate walks up to the tenant root from any working subdir."""
    origin, clone = _make_pair(tmp_path)
    _push_remote_commit(tmp_path, origin, n=1)
    sub = clone / "1p" / "acme" / "acme-1234"
    sub.mkdir(parents=True)
    out = _run_hook(sub)
    assert out.returncode == 0
    # subdir creation dirtied the tree? untracked dirs with no files don't
    # show in porcelain; clean-behind path fast-forwards.
    assert "fast-forwarded 1 commit(s)" in out.stdout


def test_unreachable_origin_degrades_to_note(tmp_path: Path) -> None:
    origin, clone = _make_pair(tmp_path)
    # Point origin somewhere that doesn't exist.
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    out = _run_hook(clone)
    assert out.returncode == 0
    assert "could not fetch origin" in out.stdout
