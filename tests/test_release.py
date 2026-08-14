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
