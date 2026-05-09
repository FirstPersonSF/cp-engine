"""Reading-mode contracts.

The four modes (index-only / single-project / sprint / weekly review)
are defined in spec v02 §6. This module owns the contract for which
files each mode loads; the actual loading happens in the consuming
Claude session, gated by the generated CLAUDE.md.

The engine's responsibility is template-rendering CLAUDE.md so that
the rules below are encoded into instructions Claude reads.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mode:
    """One of the four loading modes."""

    number: int  # 1-4
    name: str
    loads: tuple[str, ...]  # path patterns relative to tenant root
    excludes: tuple[str, ...]
    triggers: tuple[str, ...]  # phrases that activate this mode


MODE_1_INDEX_ONLY = Mode(
    number=1,
    name="index-only",
    loads=("master-cp.md",),
    excludes=("<scope>/<code>/cp.md", "weekly-cp.md", "canonic/"),
    triggers=("(default — any session without a scoping phrase)",),
)

MODE_2_SINGLE_PROJECT = Mode(
    number=2,
    name="single-project",
    loads=("master-cp.md", "<scope>/<dir_slug>/cp.md"),
    excludes=("<scope>/<other>/cp.md", "weekly-cp.md"),
    triggers=("update <code>", "check status <code>", "switch to <code>"),
)

MODE_3_SPRINT = Mode(
    number=3,
    name="sprint",
    loads=("master-cp.md", "canonic/sprint-cp.md"),
    excludes=("<scope>/<code>/cp.md", "weekly-cp.md"),
    triggers=("update sprint", "check status sprint"),
)

MODE_4_WEEKLY_REVIEW = Mode(
    number=4,
    name="weekly-review",
    loads=(
        "master-cp.md",
        "weekly-cp.md",
        "<scope>/*/cp.md",  # active projects only (excludes inactive/)
        "canonic/sprint-cp.md",  # if present
    ),
    excludes=(),
    triggers=("run weekly review",),
)

ALL_MODES: tuple[Mode, ...] = (
    MODE_1_INDEX_ONLY,
    MODE_2_SINGLE_PROJECT,
    MODE_3_SPRINT,
    MODE_4_WEEKLY_REVIEW,
)
