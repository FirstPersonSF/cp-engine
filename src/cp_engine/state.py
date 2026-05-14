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
# engagement), `repos` (a tracked code repo, possibly standalone), or
# `initiatives` (an internal workstream — Mission Control, StoryOS, etc.).
# Renderers branch on this to choose engagement-shape vs initiative-shape
# vs repo-shape tables.
EntrySource = Literal["engagement", "repo", "initiative"]

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
# (`<tenant>/<scope>/<dir_slug>/`), with inactive dirs under
# `<tenant>/<scope>/inactive/<dir_slug>/`. Pre-v0.7 layouts (which had
# an extra `projects/` segment) are migrated by `cp migrate-projects-flat`.
#
# v0.7.1 renamed `archived/` → `inactive/`: projects often flip back to
# active (engagements paused and resumed, internal flag toggled, etc.),
# so "inactive" captures the actual semantics better than "archived"
# (which suggests a one-way trip).
INACTIVE_DIR_NAME = "inactive"


def scope_root(tenant_root: Path, scope: str) -> Path:
    """Return `<tenant_root>/<scope>` — the parent of a scope's working dirs."""
    return tenant_root / scope


def working_dir(tenant_root: Path, scope: str, dir_slug: str) -> Path:
    """Return the working directory for a project at `<tenant_root>/<scope>/<dir_slug>/`."""
    return tenant_root / scope / dir_slug


def inactive_dir(tenant_root: Path, scope: str, dir_slug: str) -> Path:
    """Return the inactive location for a project that's dropped out of sync's view."""
    return tenant_root / scope / INACTIVE_DIR_NAME / dir_slug


def inactive_root(tenant_root: Path, scope: str) -> Path:
    """Return `<tenant_root>/<scope>/inactive` — the parent of inactive dirs."""
    return tenant_root / scope / INACTIVE_DIR_NAME


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

    # Engagement-only — populated from tenant config's per-project `contacts`
    # array. Plain dicts (typically `{"name": "...", "role": "..."}`) keep the
    # shape flexible without forcing a contact schema on every consumer.
    contacts: tuple[dict, ...] = ()

    # Engagement-or-initiative — repos in MC-2 linked to this parent via
    # `repos.project_id` (engagements) or `repos.initiative_id` (initiatives).
    # Each linked repo gets its own `_repo-<repo-name>.md` written into the
    # parent's working dir, mirroring the standalone-repo `_repo.md` shape.
    # Repos can be dual-linked (engagement AND initiative) — they then appear
    # under both working dirs. This field stays empty on standalone-repo
    # ProjectStates.
    linked_repos: tuple[LinkedRepo, ...] = ()


@dataclass(frozen=True)
class LinkedRepo:
    """A repo in MC-2 linked to an engagement project via `repos.project_id`.

    Carries just enough to render `_repo-<repo-name>.md` under the parent
    engagement's working dir: the GitHub coordinate, status, and a short
    description. Mirrors the repo-side fields of ProjectState but stays
    intentionally minimal — engagement-linked repos don't need the full
    project lifecycle tracking that standalone repos do.
    """

    repo_name: str
    github_org: str
    status: str  # one of REPO_STATUSES (Active | Holding | Inactive); we filter Inactive at the query layer
    description: str | None = None


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


@dataclass(frozen=True)
class ClientAsk:
    """One open question to the client, captured during sprint planning or via deepening."""

    text: str
    asked_date: str  # ISO date
    status: str      # "open" | "answered" | "dropped"
    who: str | None = None


@dataclass(frozen=True)
class Risk:
    """One dependency or risk on a project's sprint plan."""

    text: str
    severity: str    # "escalated" | "watching" | "dependency"
    category: str    # value from tenant config risk_categories
    raised_date: str
    why_it_matters: str | None = None


@dataclass(frozen=True)
class HorizonItem:
    """One forward-looking item (milestone, decision, opportunity) on a project's horizon."""

    text: str
    bucket: str      # "milestone" | "decision" | "opportunity"
    target_date: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class Outbound:
    """One outbound client message: sent, drafted, or queued."""

    text: str
    status: str      # "sent" | "draft" | "queued"
    date: str
    note: str | None = None


@dataclass(frozen=True)
class InboundUpdate:
    """One inbound update from the client, captured in a sprint file."""

    date: str
    who: str
    text: str


@dataclass(frozen=True)
class Deliverable:
    """One deliverable in a sprint plan; position drives priority."""

    text: str
    position: int    # 1-indexed priority


@dataclass(frozen=True)
class MeetingNotes:
    """Decisions plus prose discussion from a partners' weekly review."""

    source: str | None = None
    attendees: str | None = None
    duration: str | None = None
    decisions: tuple[str, ...] = ()
    discussion_prose: str = ""


@dataclass(frozen=True)
class SprintFacts:
    """Engine-managed facts strip at the top of a sprint file."""

    stage: str | None
    owner: str | None
    budget_short: str | None
    last_touched_short: str | None
    last_sprint_hours_line: str | None
    sessions_this_week: int
    open_issues: int


@dataclass(frozen=True)
class SprintCommit:
    """One commit referenced in a sprint file's recent-activity block."""

    sha_short: str
    subject: str
    author: str
    when_short: str


@dataclass(frozen=True)
class WhereItStands:
    """Engine-managed snapshot of recent activity for one project's sprint."""

    last_session_date: str | None
    last_session_who: str | None
    last_session_summary: str | None
    recent_commits: tuple[SprintCommit, ...]
    open_tracked_issues: tuple[Issue, ...]


@dataclass(frozen=True)
class CarryForward:
    """Open items rolled forward from the prior sprint."""

    asks: tuple[ClientAsk, ...]
    risks: tuple[Risk, ...]
    horizon: tuple[HorizonItem, ...]


@dataclass(frozen=True)
class Stakeholder:
    """One person on the client side, with their role and context.

    Parsed from the `### Stakeholders` subsection inside `## Client communication`
    in a sprint file. Bracket convention: `[name · role · context]`.
    """

    name: str
    role: str | None = None
    context: str | None = None


@dataclass(frozen=True)
class Theme:
    """One key thread of discussion for a sprint week.

    Parsed from `## Themes` in `sprints/<W##>/_week.md` (week-scope, not
    per-project). Bracket convention: `[theme · YYYY-MM-DD] text`.
    """

    text: str
    date: str  # ISO date


@dataclass(frozen=True)
class DecisionEntry:
    """One structured decision recorded during a sprint deepening.

    Distinct from `MeetingNotes.decisions` (which is a plain tuple of
    strings for backward compatibility with the freeform partners-review
    surface). DecisionEntry carries the metadata needed for projection
    into project cp.md `recent-decisions-strip` and weekly-cp.md
    `decisions-strip` (the latter only when `cross_cutting=True`).

    Bracket convention in the sprint file's `### Decisions` block:
    `[decision · YYYY-MM-DD][cross-cutting] text` (the `[cross-cutting]`
    flag is optional and absent when False).
    """

    text: str
    date: str  # ISO date
    cross_cutting: bool = False


@dataclass(frozen=True)
class SprintFile:
    """All parsed content of one project's sprint file for one week."""

    project_code: str
    week_iso: str
    week_start: str
    week_end: str
    prior_sprint: str | None
    facts: SprintFacts
    where_it_stands: WhereItStands
    carry_forward: CarryForward
    client_outbound: tuple[Outbound, ...]
    client_open_asks: tuple[ClientAsk, ...]
    client_inbound: tuple[InboundUpdate, ...]
    risks: tuple[Risk, ...]
    allocation: tuple[PersonHours, ...]
    deliverables: tuple[Deliverable, ...]
    definition_of_done: str
    horizon: tuple[HorizonItem, ...]
    meeting_notes: MeetingNotes | None
    # Phase 1.2 (v0.8.5) additions — projected up into project cp.md
    # and weekly-cp.md engine-managed regions during sync. Empty when
    # the sprint file pre-dates v0.8.5 (no `### Stakeholders` subsection
    # or no bracket-formatted decisions).
    stakeholders: tuple[Stakeholder, ...] = ()
    decisions: tuple[DecisionEntry, ...] = ()

    @property
    def total_allocation_hours(self) -> float:
        return sum(p.hours for p in self.allocation)

    @property
    def escalated_risk_count(self) -> int:
        return sum(1 for r in self.risks if r.severity == "escalated")
