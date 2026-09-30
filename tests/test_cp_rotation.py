"""Word-count rotation: aged Updates move verbatim to the archive, nothing is lost (#280).

WHAT IS PINNED. `cp_rotation` moves Exec Summary Updates entries older than
28 days out of `cp.md` into `cp-archive-<YYYY-MM>.md`, byte-for-byte, and
refuses — writing nothing — when the result would lose or invent a line. The
deliberate-loss tests below break the planner on purpose; they are the reason
the no-loss check exists, and they fail against a planner with the check
removed (the control recorded in the #280 commit).
"""
from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import pytest

from cp_engine import cp_rotation
from cp_engine.cp_rotation import (
    RotationError,
    RotationLossError,
    archive_name_for,
    check_rotation_loss,
    loss_between,
    plan_rotation,
    rotate_cp,
)

TODAY = date(2026, 9, 30)

CP_MD = """---
Project: 1pi-9005 Mission Control
---

## Facts

| **Owner** | Drew |

<!-- cp-engine:start exec-summary -->
## Exec Summary  ·  updated 2026-09-28

**Objective:** Run the tenant.
**Status:** Live.
**Where it stands:**
- Things work.
**Next up:**
- More things.
**Blockers:**
- None.

**Updates:**
- 2026-09-28 — Fresh entry that stays.
- 2026-09-02 (eve) — Exactly 28 days old; stays (the threshold is strictly older).
- 2026-09-01 — Old entry one, moves.
  - a nested sub-bullet that moves with it
- 2026-08-20 — Old entry two, moves.

  An indented second paragraph that belongs to entry two.
- (Older updates — 2026-07-16 through 2026-08-03 — rotated to [archive](docs/a.md))
- _Entries 2026-07-01 and earlier rotated to [`_archive.md`](_archive.md)._
- An undated entry that cannot be aged and stays.
<!-- cp-engine:end exec-summary -->

## Project Notes

Hand-written prose that never moves, even though it mentions 2026-01-01.
- 2026-01-01 — a dated bullet OUTSIDE Updates; not an Updates entry, stays.

<!-- cp-engine:start sprint-facts -->
- engine strip content
<!-- cp-engine:end sprint-facts -->
"""


def _plan(text: str = CP_MD, archive: str | None = None):
    return plan_rotation(text, archive, today=TODAY, project_label="1pi-9005-mission-control")


def test_aged_entries_move_verbatim_and_everything_else_stays():
    plan = _plan()
    assert plan.changed
    assert [m.entry_date.isoformat() for m in plan.moved] == ["2026-09-01", "2026-08-20"]

    # Gone from cp.md, with their continuations.
    for gone in ("Old entry one", "a nested sub-bullet", "Old entry two", "second paragraph"):
        assert gone not in plan.cp_md_text
    # Present in the archive, VERBATIM — indentation included.
    assert "- 2026-09-01 — Old entry one, moves.\n  - a nested sub-bullet that moves with it\n" in plan.archive_text
    assert "  An indented second paragraph that belongs to entry two." in plan.archive_text
    # Newest first, under a dated section.
    arc = plan.archive_text
    assert arc.index("## Rolled off 2026-09-30") < arc.index("2026-09-01 —") < arc.index("2026-08-20 —")

    # What stays: fresh, exactly-at-threshold, pointer bullets, undated,
    # everything outside Updates.
    for kept in (
        "2026-09-28 — Fresh entry",
        "2026-09-02 (eve) — Exactly 28 days",
        "(Older updates — 2026-07-16",
        "_Entries 2026-07-01 and earlier",
        "An undated entry",
        "a dated bullet OUTSIDE Updates",
        "- engine strip content",
        "**Status:** Live.",
    ):
        assert kept in plan.cp_md_text, kept


def test_the_freshness_stamp_does_not_move():
    """Rotation changes no field's truth; advancing the stamp would
    manufacture freshness (#249)."""
    assert "updated 2026-09-28" in _plan().cp_md_text


def test_only_the_updates_block_changes():
    plan = _plan()
    before_lines = CP_MD.splitlines()
    after_lines = plan.cp_md_text.splitlines()
    removed = [ln for ln in before_lines if ln not in after_lines]
    assert all(
        ln.startswith(("- 2026-09-01", "  - a nested", "- 2026-08-20", "  An indented"))
        for ln in removed if ln.strip()
    ), removed


def test_a_new_archive_is_scaffolded_with_an_anchor_block():
    arc = _plan().archive_text
    assert arc.startswith("---\nProject: 1pi-9005-mission-control\n")
    assert f"Filename: {archive_name_for(TODAY)}" in arc
    assert archive_name_for(TODAY) == "cp-archive-2026-09.md"


def test_an_existing_archive_is_appended_to_not_rewritten():
    existing = "---\nProject: x\n---\n\n# Updates archive — 2026-09\n\n- 2026-08-01 — an earlier roll-off.\n"
    plan = _plan(archive=existing)
    assert plan.archive_text.startswith(existing.rstrip("\n"))
    assert plan.archive_text.count("Provenance:") == 0  # no second scaffold
    assert "- 2026-08-01 — an earlier roll-off." in plan.archive_text


def test_nothing_past_age_is_an_honest_no_op():
    fresh = CP_MD.replace("2026-09-01 — Old", "2026-09-20 — Old").replace(
        "2026-08-20 — Old", "2026-09-21 — Old"
    )
    plan = _plan(fresh)
    assert not plan.changed and plan.moved == ()
    assert plan.cp_md_text == fresh


def test_a_file_with_no_region_is_an_error_not_a_silent_no_op():
    with pytest.raises(RotationError, match="exec-summary"):
        _plan("# just prose\n")


def test_word_counts_are_reported():
    plan = _plan()
    assert plan.words_after < plan.words_before
    s = plan.summary()
    assert s["changed"] is True and len(s["moved"]) == 2
    assert s["older_than"] == "2026-09-02"


# ── the no-loss check ─────────────────────────────────────────────────


def test_loss_between_treats_a_move_as_no_loss():
    lost, unexpected = loss_between(
        {"a": "x\ny\n", "b": ""}, {"a": "x\n", "b": "y\n"}
    )
    assert lost == [] and unexpected == []


def test_loss_between_counts_duplicates():
    """Two identical entries collapsing into one is a loss."""
    lost, _ = loss_between({"a": "- dup\n- dup\n"}, {"a": "- dup\n"})
    assert lost == ["- dup"]


def test_loss_between_refuses_invented_lines_beyond_the_scaffold():
    _, unexpected = loss_between({"a": "x\n"}, {"a": "x\n## heading\nstray\n"}, ["## heading"])
    assert unexpected == ["stray"]


def _lossy_plan(monkeypatch, drop: str) -> None:
    """Sabotage the planner: the archive text it builds silently loses `drop`.

    Patched at `RotationPlan` construction, i.e. AFTER the move is planned and
    BEFORE the loss check runs — the exact window a planner bug would sit in.
    """
    real_plan = cp_rotation.RotationPlan

    def sabotaged(cp_md_text, archive_text, *rest):
        return real_plan(cp_md_text, archive_text.replace(drop, ""), *rest)

    monkeypatch.setattr(cp_rotation, "RotationPlan", sabotaged)


def test_a_planner_that_drops_an_entry_is_refused(monkeypatch):
    """THE DELIBERATE-LOSS CASE. A moved entry's nested bullet leaves cp.md
    and never reaches the archive: the plan must be refused, not returned."""
    _lossy_plan(monkeypatch, "  - a nested sub-bullet that moves with it\n")
    with pytest.raises(RotationLossError) as exc:
        _plan()
    assert exc.value.lost == ["  - a nested sub-bullet that moves with it"]


def test_a_refused_rotation_writes_neither_file(tmp_path, monkeypatch):
    wd = tmp_path / "1pi-9005-mission-control"
    wd.mkdir()
    (wd / "cp.md").write_text(CP_MD)
    _lossy_plan(monkeypatch, "Old entry two, moves.")
    with pytest.raises(RotationLossError):
        rotate_cp(wd, today=TODAY)
    assert (wd / "cp.md").read_text() == CP_MD
    assert not (wd / archive_name_for(TODAY)).exists()


def test_rotate_cp_writes_both_files(tmp_path):
    wd = tmp_path / "proj"
    wd.mkdir()
    (wd / "cp.md").write_text(CP_MD)
    plan = rotate_cp(wd, today=TODAY)
    assert plan.changed
    assert "Old entry one" not in (wd / "cp.md").read_text()
    assert "Old entry one" in (wd / "cp-archive-2026-09.md").read_text()
    # Running it again moves nothing.
    again = rotate_cp(wd, today=TODAY)
    assert not again.changed


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_check_rotation_loss_reads_head_vs_tree(tmp_path):
    """The on-disk half, modelled on merge-check: HEAD vs working tree."""
    _git("init", "--initial-branch=main", cwd=tmp_path)
    _git("config", "user.email", "t@e.com", cwd=tmp_path)
    _git("config", "user.name", "T", cwd=tmp_path)
    wd = tmp_path / "proj"
    wd.mkdir()
    (wd / "cp.md").write_text(CP_MD)
    _git("add", "-A", cwd=tmp_path)
    _git("commit", "-m", "seed", cwd=tmp_path)

    plan = rotate_cp(wd, today=TODAY)
    paths = ["proj/cp.md", f"proj/{plan.archive_name}"]
    assert check_rotation_loss(tmp_path, paths, plan.scaffold_lines) == ([], [])

    # Now lose a line on disk, after the engine's own check passed.
    arc = wd / plan.archive_name
    arc.write_text(arc.read_text().replace("- 2026-08-20 — Old entry two, moves.\n", ""))
    lost, _ = check_rotation_loss(tmp_path, paths, plan.scaffold_lines)
    assert lost == ["- 2026-08-20 — Old entry two, moves."]
