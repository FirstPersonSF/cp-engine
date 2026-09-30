"""Tests for `scripts/release.py`.

The script is loaded by file path because it lives under `scripts/`
(uv-run shebang, not under `src/`). We exercise the pure-Python
preflight helpers and stub `subprocess` calls so no network or git
state is required.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_SCRIPT = REPO_ROOT / "scripts" / "release.py"


def _load_release() -> ModuleType:
    """Load `scripts/release.py` as a module by file path."""
    spec = importlib.util.spec_from_file_location("release_script", RELEASE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_script"] = module
    spec.loader.exec_module(module)
    return module


release = _load_release()


def _make_completed(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["git"], returncode=0, stdout=stdout, stderr=""
    )


def _patch_release(tmp_path: Path, *, current: str, changelog: str):
    """Build a fake repo skeleton + return context patches for `run`.

    Caller provides the current version-in-pyproject and the changelog
    contents. `run` is patched to return clean working tree, branch
    `main`, no tag locally, no tag on origin.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        f'[project]\nname = "cp-engine"\nversion = "{current}"\n'
    )
    cl = tmp_path / "CHANGELOG.md"
    cl.write_text(changelog)

    def fake_run(cmd, **kwargs):
        # Map git subcommands to known-good outputs.
        if cmd[:2] == ["git", "status"]:
            return _make_completed("")  # clean tree
        if cmd[:2] == ["git", "rev-parse"]:
            return _make_completed("main")
        if cmd[:3] == ["git", "tag", "--list"]:
            return _make_completed("")  # tag not local
        if cmd[:3] == ["git", "ls-remote", "--tags"]:
            return _make_completed("")  # tag not on origin
        return _make_completed("")

    return pyproject, cl, fake_run


def test_preflight_passes_when_changelog_top_matches_new(tmp_path: Path) -> None:
    pyproject, cl, fake_run = _patch_release(
        tmp_path,
        current="0.15.0",
        changelog="## v0.15.1 — 2026-06-02\n\n- Patch.\n\n## v0.15.0 — 2026-05-30\n",
    )
    with (
        patch.object(release, "PYPROJECT", pyproject),
        patch.object(release, "CHANGELOG", cl),
        patch.object(release, "run", side_effect=fake_run),
    ):
        cur_v, new_v = release.preflight("0.15.1")
    assert str(cur_v) == "0.15.0"
    assert str(new_v) == "0.15.1"


def test_preflight_rejects_when_future_section_drafted_above(tmp_path: Path) -> None:
    """Drafting `## v0.16.0` ahead of `v0.15.1` must fail preflight — the
    section for the version being released must be the highest one in
    the changelog."""
    pyproject, cl, fake_run = _patch_release(
        tmp_path,
        current="0.15.0",
        changelog=(
            "## v0.16.0 — TBD\n\n- Future stuff.\n\n"
            "## v0.15.1 — 2026-06-02\n\n- Patch.\n"
        ),
    )
    with (
        patch.object(release, "PYPROJECT", pyproject),
        patch.object(release, "CHANGELOG", cl),
        patch.object(release, "run", side_effect=fake_run),
        pytest.raises(release.ReleaseError, match=r"first version section is `## v0\.16\.0`"),
    ):
        release.preflight("0.15.1")


def test_preflight_rejects_when_section_missing(tmp_path: Path) -> None:
    """The existing 'no section for new version' check must still fire."""
    pyproject, cl, fake_run = _patch_release(
        tmp_path,
        current="0.15.0",
        changelog="## v0.15.0 — 2026-05-30\n\n- Previous.\n",
    )
    with (
        patch.object(release, "PYPROJECT", pyproject),
        patch.object(release, "CHANGELOG", cl),
        patch.object(release, "run", side_effect=fake_run),
        pytest.raises(release.ReleaseError, match=r"no `## v0\.15\.1.*` section"),
    ):
        release.preflight("0.15.1")


def test_preflight_rejects_when_tag_exists_on_origin(tmp_path: Path) -> None:
    """A tag that exists on remote but was deleted locally must fail
    preflight. Without this check, the release commits land on main,
    then `git push origin v<X>` fails afterwards — leaving the branch
    ahead of remote with no clean recovery."""
    pyproject, cl, _ = _patch_release(
        tmp_path,
        current="0.15.0",
        changelog="## v0.15.1 — 2026-06-02\n\n- Patch.\n",
    )

    def fake_run_with_remote_tag(cmd, **kwargs):
        if cmd[:2] == ["git", "status"]:
            return _make_completed("")
        if cmd[:2] == ["git", "rev-parse"]:
            return _make_completed("main")
        if cmd[:3] == ["git", "tag", "--list"]:
            return _make_completed("")  # NOT local
        if cmd[:3] == ["git", "ls-remote", "--tags"]:
            # Simulate: tag IS on origin even though absent locally.
            return _make_completed(
                "0123456789abcdef0123456789abcdef01234567\trefs/tags/v0.15.1"
            )
        return _make_completed("")

    with (
        patch.object(release, "PYPROJECT", pyproject),
        patch.object(release, "CHANGELOG", cl),
        patch.object(release, "run", side_effect=fake_run_with_remote_tag),
        pytest.raises(release.ReleaseError, match="already exists on origin"),
    ):
        release.preflight("0.15.1")


def test_preflight_rejects_when_tag_exists_locally(tmp_path: Path) -> None:
    """Belt-and-suspenders: the original local-tag check must still
    fire even with the new remote check in place."""
    pyproject, cl, _ = _patch_release(
        tmp_path,
        current="0.15.0",
        changelog="## v0.15.1 — 2026-06-02\n\n- Patch.\n",
    )

    def fake_run_with_local_tag(cmd, **kwargs):
        if cmd[:2] == ["git", "status"]:
            return _make_completed("")
        if cmd[:2] == ["git", "rev-parse"]:
            return _make_completed("main")
        if cmd[:3] == ["git", "tag", "--list"]:
            return _make_completed("v0.15.1")  # IS local
        if cmd[:3] == ["git", "ls-remote", "--tags"]:
            return _make_completed("")
        return _make_completed("")

    with (
        patch.object(release, "PYPROJECT", pyproject),
        patch.object(release, "CHANGELOG", cl),
        patch.object(release, "run", side_effect=fake_run_with_local_tag),
        pytest.raises(release.ReleaseError, match="already exists locally"),
    ):
        release.preflight("0.15.1")


# --- uv.lock must be staged with the release commit -------------------------


def test_uv_lock_is_in_the_release_commit_paths() -> None:
    """`uv.lock` is regenerated by the release's own test/build steps.

    The `uv run` invocations re-resolve the workspace and rewrite cp-engine's
    `version` entry to match the freshly-bumped pyproject. Leaving it unstaged
    produced a dirty tree immediately after three "successful" releases
    (v0.96.0, v0.97.0, v0.97.1 — hand-committed each time), and the dirty tree
    then aborts the NEXT release's clean-tree preflight for an unrelated
    reason.

    Asserted against the source rather than by driving a full release, because
    the staging list is the thing that regressed — a file quietly dropped from
    it fails here.
    """
    src = RELEASE_SCRIPT.read_text()
    assert "UV_LOCK" in src, "uv.lock path constant is gone"
    # It must reach the `git add`, not merely be defined.
    add_block = src[src.index("add_paths = ["):src.index('run(["git", "add"')]
    assert "UV_LOCK" in add_block, "UV_LOCK defined but never staged"


def test_release_stages_every_version_bearing_file() -> None:
    """Every file the script REWRITES must also be committed by it.

    Guards the general form of the uv.lock bug: a version written to disk but
    left out of `git add` ships a tag whose tree disagrees with itself.
    """
    src = RELEASE_SCRIPT.read_text()
    add_block = src[src.index("add_paths = ["):src.index('run(["git", "add"')]
    for const in ("PYPROJECT", "INIT_PY", "PLUGIN_JSON",
                  "MARKETPLACE_JSON", "WEBHOOK_PYPROJECT", "UV_LOCK"):
        assert const in add_block, f"{const} is not staged by the release commit"


# ── #316: the release gate states the pass count it saw ─────────────────


@pytest.mark.parametrize("line,expected", [
    ("812 passed in 41.20s", "812 passed"),
    ("==== 812 passed, 3 skipped, 2 warnings in 41.20s ====",
     "812 passed, 3 skipped, 2 warnings"),
    ("1 failed, 811 passed in 40.00s (0:00:40)", "1 failed, 811 passed"),
])
def test_parse_pytest_summary_reads_the_closing_line(line, expected):
    assert release.parse_pytest_summary(f"....\nwarnings block\n{line}\n") == expected


def test_a_truncated_run_has_no_summary():
    """The #316 failure: output that ends on dots or the warnings block —
    what a `-qq` run or a killed one looks like — must not yield a count."""
    assert release.parse_pytest_summary(
        "tests/test_x.py::test_3_passed PASSED\n......  [ 50%]\n"
    ) is None


def test_run_pytest_counted_returns_pytests_own_exit_and_counts(tmp_path, capsys):
    """Against a real pytest subprocess, not a stub: a failing file must come
    back non-zero with its counts, and the output must still stream."""
    t = tmp_path / "test_tiny.py"
    t.write_text("def test_ok():\n    pass\n\ndef test_bad():\n    assert False\n")
    code, summary = release.run_pytest_counted(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-o", "addopts=", str(t)],
    )
    assert code == 1
    assert summary == "1 failed, 1 passed"
    assert "1 failed, 1 passed" in capsys.readouterr().out


def test_pyproject_addopts_does_not_carry_q():
    """`-q` in addopts turns every `pytest -q` into `-qq`, which hides the
    summary line the release gate and the standing rule both depend on."""
    import tomllib

    opts = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())[
        "tool"]["pytest"]["ini_options"].get("addopts", "")
    assert "-q" not in opts.split()


# ── CI gate (architecture plan step 0) ─────────────────────────────────


class _FakeRun:
    """Stand-in for release.run: answers gh / git by the command's head."""

    def __init__(self, *, green="a" * 40, ancestor=True, changed=("CHANGELOG.md",)):
        self.green, self.ancestor, self.changed = green, ancestor, changed

    def __call__(self, cmd, *, capture=False, check=True, env=None):
        if cmd[:3] == ["gh", "run", "list"]:
            return _make_completed(self.green)
        if cmd[:2] == ["git", "fetch"]:
            return _make_completed("")
        if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(cmd, 0 if self.ancestor else 1, "", "")
        if cmd[:3] == ["git", "diff", "--name-only"]:
            return _make_completed("\n".join(self.changed))
        raise AssertionError(f"unexpected command {cmd}")


def test_ci_gate_passes_when_only_the_changelog_changed_since_green():
    release = _load_release()
    with patch.object(release, "run", _FakeRun()):
        assert release.ci_gate() == "a" * 40


def test_ci_gate_refuses_when_code_changed_since_the_last_green_run():
    release = _load_release()
    fake = _FakeRun(changed=("CHANGELOG.md", "src/cp_engine/sync.py"))
    with patch.object(release, "run", fake), pytest.raises(release.ReleaseError, match="sync.py"):
        release.ci_gate()


def test_ci_gate_refuses_when_green_is_not_an_ancestor():
    release = _load_release()
    with patch.object(release, "run", _FakeRun(ancestor=False)), pytest.raises(
        release.ReleaseError, match="not an ancestor"
    ):
        release.ci_gate()


def test_ci_gate_refuses_with_no_green_run():
    release = _load_release()
    with patch.object(release, "run", _FakeRun(green="")), pytest.raises(
        release.ReleaseError, match="no successful"
    ):
        release.ci_gate()
