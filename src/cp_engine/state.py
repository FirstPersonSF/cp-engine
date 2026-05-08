"""Shared data shapes consumed by sync, render, and (eventually) the CLI.

Lives in its own module to break the sync↔render circular import.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

# Whether a ProjectState came from MC-2's `projects` table (a client
# engagement) or from `repos` (a tracked code repo, possibly standalone).
# Renderers branch on this to choose engagement-shape vs repo-shape tables.
EntrySource = Literal["engagement", "repo"]

# Which MC-2 company kind the entry belongs to. For engagements, derived
# from the engagement's company. For repos, derived from the repo's company.
# Renderers group by this to produce the three master-CP sections.
CompanyKind = Literal["client", "self-fpsf", "self-canonic"]


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
