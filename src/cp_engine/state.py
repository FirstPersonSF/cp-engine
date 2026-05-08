"""Shared data shapes consumed by sync, render, and (eventually) the CLI.

Lives in its own module to break the sync↔render circular import.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ProjectState:
    """Source-of-truth state for one project, normalized across backends."""

    code: str
    name: str
    status: str  # one of MC_STATUSES
    is_internal: bool
    owner: str | None
    last_touched: datetime | None
    deadline: datetime | None
    one_line_summary: str | None = None  # regenerated during deepening pass


@dataclass(frozen=True)
class Issue:
    """One tracked GitHub Issue, surfaced in a project CP's tracked-issues table."""

    number: int
    title: str
    status: str
    owner: str | None
    updated: datetime | None
