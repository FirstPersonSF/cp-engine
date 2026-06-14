from datetime import date

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
