"""Shared data shapes consumed by sync, render, and (eventually) the CLI.

Lives in its own module to break the sync↔render circular import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

# Whether a ProjectState came from MC-2's `projects` table (a client
# engagement) or from `repos` (a tracked code repo, possibly standalone).
# Renderers branch on this to choose engagement-shape vs repo-shape tables.
EntrySource = Literal["engagement", "repo"]

# Which MC-2 company kind the entry belongs to. For engagements, derived
# from the engagement's company. For repos, derived from the repo's company.
# Renderers group by this to produce the three master-CP sections.
CompanyKind = Literal["client", "self-fpsf", "self-canonic"]

# Maps company_kind to the v0.3 working-tree scope directory name. This is
# the contract between MC-2's data model and cp's filesystem layout.
_SCOPE_BY_KIND: dict[str, str] = {
    "client": "1p",
    "self-fpsf": "firstpersonsf",
    "self-canonic": "canonic",
}


def scope_for(company_kind: str) -> str:
    """Return the working-tree scope directory for a company kind.

    Raises ValueError on unknown kinds rather than silently misplacing a
    project — better to fail loud than scaffold under the wrong scope.
    """
    try:
        return _SCOPE_BY_KIND[company_kind]
    except KeyError:
        raise ValueError(
            f"Unknown company_kind {company_kind!r}; expected one of "
            f"{sorted(_SCOPE_BY_KIND)}"
        ) from None


# v0.7 working-tree layout: working dirs live directly under their scope
# (`<tenant>/<scope>/<dir_slug>/`), with archived dirs under
# `<tenant>/<scope>/archived/<dir_slug>/`. Pre-v0.7 layouts (which had
# an extra `projects/` segment) are migrated by `cp migrate-projects-flat`.
ARCHIVED_DIR_NAME = "archived"


def scope_root(tenant_root: Path, scope: str) -> Path:
    """Return `<tenant_root>/<scope>` — the parent of a scope's working dirs."""
    return tenant_root / scope


def working_dir(tenant_root: Path, scope: str, dir_slug: str) -> Path:
    """Return the working directory for a project at `<tenant_root>/<scope>/<dir_slug>/`."""
    return tenant_root / scope / dir_slug


def archived_dir(tenant_root: Path, scope: str, dir_slug: str) -> Path:
    """Return the archive location for a stale project."""
    return tenant_root / scope / ARCHIVED_DIR_NAME / dir_slug


def archived_root(tenant_root: Path, scope: str) -> Path:
    """Return `<tenant_root>/<scope>/archived` — the parent of archived dirs."""
    return tenant_root / scope / ARCHIVED_DIR_NAME


_SLUG_NON_ALPHANUM = re.compile(r"[^a-z0-9]+")


def dir_slug(code: str, name: str | None) -> str:
    """Build a working-directory name from a project's code and full name.

    Returns `<code>-<name-tail-slugified>` so working dirs are readable in
    a directory listing (`ggl-5177-event-safety-playbook` rather than
    `ggl-5177`). When the name doesn't add information beyond the code
    (empty, or just the code itself), returns the bare code.

    Slugification:
      - lowercase
      - strip a leading occurrence of the code (in any case, with spaces
        for hyphens) so "GGL 5177 Event Safety Playbook" doesn't become
        "ggl-5177-ggl-5177-event-safety-playbook"
      - collapse non-alphanumeric runs to single hyphens
      - trim leading/trailing hyphens
    """
    code_lc = code.lower().strip()
    if not name:
        return code_lc

    # Normalize spaces/hyphens so "GGL 5177" and "ggl-5177" both match.
    work = name.lower().strip()

    # Strip a leading code occurrence so the tail is just the human name.
    # Try the hyphenated form first ("ggl-5177"), then the spaced form
    # ("ggl 5177"). The replacement happens in lowercase space.
    code_with_spaces = code_lc.replace("-", " ")
    for prefix in (code_lc, code_with_spaces):
        if work.startswith(prefix):
            work = work[len(prefix) :].lstrip(" -")
            break

    tail = _SLUG_NON_ALPHANUM.sub("-", work).strip("-")
    if not tail:
        return code_lc
    return f"{code_lc}-{tail}"


@dataclass(frozen=True)
class ProjectState:
    """One trackable item in cp-engine's master CP.

    Spans both client engagements (`source="engagement"`, sourced from
    MC-2 `projects`) and standalone code repos (`source="repo"`, sourced
    from MC-2 `repos` with `project_id IS NULL`).

    Engagement-only fields (account_manager, deal_stage, budget) live on
    the engagement variant; repo-only fields (github_org, repo_name,
    description) live on the repo variant. The renderer dispatches on
    `source` to choose which table schema to use.

    Engagement-backed repos (a repo with `project_id` set) do NOT appear
    as separate ProjectState entries — their information enriches the
    parent engagement's project CP. See render.py for that handling.
    """

    code: str  # canonical id: ggl-5188 (engagement), mc-2 (repo)
    name: str  # full_job_name for engagement, repo_name for repo
    source: EntrySource
    company_kind: CompanyKind
    company_code: str | None  # GGL, IBX, 1PI, CNC, ...
    company_name: str | None  # Google, First Person, Canonic, ...

    # Status semantics differ by source:
    # - engagement: one of MC_STATUSES (Deal | Open | Holding | Closed | Archived)
    # - repo: one of REPO_STATUSES (Active | Holding | Inactive)
    status: str

    is_internal: bool  # only meaningful for engagements
    owner: str | None
    last_touched: datetime | None
    deadline: datetime | None  # not tracked yet for either source
    one_line_summary: str | None = None  # regenerated during deepening pass

    # Engagement-only fields
    deal_stage: str | None = None
    budget: float | None = None
    dropbox_folder_url: str | None = None

    # Repo-only fields
    github_org: str | None = None
    repo_name: str | None = None  # raw GitHub slug, distinct from `code`
    description: str | None = None  # ≤120 char one-liner from repos.description


@dataclass(frozen=True)
class Issue:
    """One tracked GitHub Issue, surfaced in a project CP's tracked-issues table."""

    number: int
    title: str
    status: str
    owner: str | None
    updated: datetime | None


@dataclass(frozen=True)
class PersonHours:
    """One person's hours on one project for one week."""

    person_name: str
    hours: float


@dataclass(frozen=True)
class ProjectAllocation:
    """All allocations for one project in one week, sorted by hours desc."""

    project_code: str  # canonical id (matches ProjectState.code)
    is_internal: bool  # excludes from per-row rendering, included in per-person rollup
    entries: tuple[PersonHours, ...]

    @property
    def total_hours(self) -> float:
        return sum(e.hours for e in self.entries)


@dataclass(frozen=True)
class PersonRollup:
    """One person's total hours for the week, split engagement vs internal admin."""

    person_name: str
    engagement_hours: float
    engagement_project_count: int
    internal_hours: float

    @property
    def total_hours(self) -> float:
        return self.engagement_hours + self.internal_hours


@dataclass(frozen=True)
class WeeklyAllocations:
    """All allocations for one week, indexed two ways."""

    week_start: str  # ISO date (YYYY-MM-DD)
    by_project: dict[str, ProjectAllocation]
    rollup: tuple[PersonRollup, ...]  # sorted by total_hours desc
