import subprocess
from datetime import date

import pytest

from cp_engine.shell_snapshot import build_snapshot, slugify_label


def test_slugify_label():
    assert slugify_label("before IBX workshop") == "before-ibx-workshop"
    assert slugify_label("v2: after  re-work!") == "v2-after-re-work"


def test_build_snapshot_filename_and_row():
    working = (
        "---\n"
        "id: ibx-5153/deliverable/pos\n"
        "project: ibx-5153\n"
        "layer: Deliverables\n"
        "title: Positioning narrative\n"
        "status: active\n"
        "stage: revised\n"
        "---\n"
        "# The narrative\nbody text\n"
    )
    snap = build_snapshot(
        working_text=working,
        deliverable_id="ibx-5153/deliverable/pos",
        project_code="ibx-5153",
        label="before IBX workshop",
        reason="client source material pending",
        commit="c812f9a",
        working_copy_dirty=False,
        created=date(2026, 6, 10),
    )
    assert snap.filename == "2026-06-10-before-ibx-workshop.md"
    assert "# The narrative\nbody text" in snap.frozen_text
    assert "snapshot:" in snap.frozen_text
    assert "label: before IBX workshop" in snap.frozen_text or \
           "label: 'before IBX workshop'" in snap.frozen_text
    assert snap.row["id"] == "ibx-5153/deliverable/pos@2026-06-10-before-ibx-workshop"
    assert snap.row["deliverable_id"] == "ibx-5153/deliverable/pos"
    assert snap.row["project_code"] == "ibx-5153"
    assert snap.row["label"] == "before IBX workshop"
    assert snap.row["reason"] == "client source material pending"
    assert snap.row["commit"] == "c812f9a"
    assert snap.row["working_copy_dirty"] is False
    assert snap.row["created"] == "2026-06-10"


def test_build_snapshot_preserves_original_frontmatter():
    working = "---\nid: p/deliverable/d\nproject: p\nlayer: Deliverables\nstage: final\n---\nbody\n"
    snap = build_snapshot(
        working_text=working, deliverable_id="p/deliverable/d", project_code="p",
        label="shipped", reason=None, commit=None, working_copy_dirty=True,
        created=date(2026, 6, 13),
    )
    assert "stage: final" in snap.frozen_text
    assert snap.row["reason"] is None
    assert snap.row["working_copy_dirty"] is True


# --- deliverable resolution + git helpers ---


def test_resolve_deliverable_file(tmp_path):
    from cp_engine.shell_snapshot import resolve_deliverable_file
    d = tmp_path / "1p/acct/proj-1/shell/Deliverables"
    d.mkdir(parents=True)
    f = d / "pos.md"
    f.write_text(
        "---\nid: proj-1/deliverable/pos\nproject: proj-1\nlayer: Deliverables\n---\nbody\n"
    )
    proj_dir = tmp_path / "1p/acct/proj-1"
    found = resolve_deliverable_file(proj_dir, "proj-1/deliverable/pos")
    assert found == f


def test_resolve_deliverable_file_missing(tmp_path):
    from cp_engine.shell_snapshot import resolve_deliverable_file, DeliverableNotFound
    proj_dir = tmp_path / "1p/acct/proj-1"
    (proj_dir / "shell/Deliverables").mkdir(parents=True)
    with pytest.raises(DeliverableNotFound):
        resolve_deliverable_file(proj_dir, "proj-1/deliverable/nope")


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path):
    _git(["init"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


def test_git_head_commit_in_repo(tmp_path):
    from cp_engine.shell_snapshot import git_head_commit
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hello\n")
    _git(["add", "a.txt"], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    sha = git_head_commit(tmp_path)
    assert isinstance(sha, str)
    assert sha


def test_git_head_commit_outside_repo(tmp_path):
    from cp_engine.shell_snapshot import git_head_commit
    # tmp_path is NOT a git repo — must return None, never raise.
    assert git_head_commit(tmp_path) is None


def test_working_copy_is_dirty(tmp_path):
    from cp_engine.shell_snapshot import working_copy_is_dirty
    _init_repo(tmp_path)
    f = tmp_path / "a.txt"
    f.write_text("hello\n")
    _git(["add", "a.txt"], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    # clean committed file
    assert working_copy_is_dirty(f, tmp_path) is False
    # modified working copy
    f.write_text("hello world\n")
    assert working_copy_is_dirty(f, tmp_path) is True
