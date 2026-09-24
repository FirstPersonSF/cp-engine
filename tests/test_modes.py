"""Reading-mode file sets (`cp_engine.modes`) after weekly-cp.md's
retirement (#305, plan D8).

The weekly review loads `master-cp.md` plus every ACCOUNT and PROGRAM
`cp.md` — the files that carry the hand-written cross-cutting decisions —
and no mode names `weekly-cp.md` in either direction.
"""

from __future__ import annotations

from cp_engine.modes import (
    ALL_MODES,
    MODE_1_INDEX_ONLY,
    MODE_2_SINGLE_PROJECT,
    MODE_4_WEEKLY_REVIEW,
)


def test_no_mode_names_weekly_cp() -> None:
    for mode in ALL_MODES:
        assert not any("weekly-cp" in p for p in mode.loads), mode.name
        assert not any("weekly-cp" in p for p in mode.excludes), mode.name


def test_weekly_review_loads_master_and_every_account_and_program_cp() -> None:
    assert MODE_4_WEEKLY_REVIEW.loads[0] == "master-cp.md"
    assert "<account>/cp.md" in MODE_4_WEEKLY_REVIEW.loads
    assert "<program>/cp.md" in MODE_4_WEEKLY_REVIEW.loads
    # Jobs and initiatives are opened one at a time with `also load`.
    assert "<job>/cp.md" in MODE_4_WEEKLY_REVIEW.excludes
    assert "<initiative>/cp.md" in MODE_4_WEEKLY_REVIEW.excludes
    assert MODE_4_WEEKLY_REVIEW.triggers == ("run weekly review",)


def test_index_only_and_single_project_unchanged_otherwise() -> None:
    assert MODE_1_INDEX_ONLY.loads == ("master-cp.md",)
    assert MODE_2_SINGLE_PROJECT.loads == ("master-cp.md", "<scope>/<dir_slug>/cp.md")
