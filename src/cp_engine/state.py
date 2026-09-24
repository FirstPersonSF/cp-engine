"""Shared data shapes consumed by sync, render, and (eventually) the CLI.

Lives in its own module to break the sync↔render circular import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal

# Which MC-2 company kind the workstream belongs to, from its company row.
# Renderers group by this to produce the master-CP sections.
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


def company_slug(company_name: str | None) -> str:
    """Kebab-case a company name for use as a working-dir name.

    "Infoblox" -> "infoblox". "Sentinel One" -> "sentinel-one".
    "AT&T, Inc." -> "at-t-inc". Empty / None / whitespace-only -> "unknown"
    (so the caller never builds a path with `//` or a missing segment).
    """
    if not company_name or not company_name.strip():
        return "unknown"
    slug = _SLUG_NON_ALPHANUM.sub("-", company_name.lower()).strip("-")
    return slug or "unknown"


def account_scope_for(project: "ProjectState") -> str:
    """Project working-dir parent: `<scope>/<account>` for clients.

    For client engagements this is `1p/<company-slug>` (the new account-
    nested layout). For self-companies (FPSF / Canonic) this is just
    `scope_for(company_kind)` — those scopes already group by self-
    company and don't gain another layer.

    Used by sync to place project dirs and by render to build navigation
    links. Keeping a single function as the authority means a layout
    change is one edit, not a sweep.
    """
    scope = scope_for(project.company_kind)
    if project.company_kind == "client":
        return f"{scope}/{company_slug(project.company_name)}"
    return scope


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


def dir_slug(code: str, name: str | None = None) -> str:
    """Working-directory name for a project = its slugified `code`.

    Since `code` is now the canonical full_job_name slug
    ("ibx-5192-platform-sales-readiness-summit"), the dir IS the code. `name`
    is retained for signature compatibility with existing call sites but no
    longer affects the result (the code already carries the description)."""
    return _SLUG_NON_ALPHANUM.sub("-", code.lower().strip()).strip("-")


def slug_full_job_name(full_job_name: str | None) -> str:
    """Slugify MC-2's `full_job_name` ("IBX 5192 Platform Sales Readiness
    Summit") into the canonical, filesystem/LLM-friendly project id
    ("ibx-5192-platform-sales-readiness-summit"). Returns "" for empty input
    (caller decides the fallback)."""
    if not full_job_name:
        return ""
    return _SLUG_NON_ALPHANUM.sub("-", full_job_name.lower()).strip("-")


@dataclass(frozen=True)
class ProjectState:
    """One workstream in cp-engine's master CP — ONE entry kind (#301).

    Every entry is an MC-2 `projects` row: a client job, a client account
    or program node, or an internal workstream (Mission Control, StoryOS).
    There is no `source` discriminator any more — the engine branches on
    the row's SHAPE: `has_agreement` (a commercial envelope exists),
    `parent_code` (where it sits in its company's tree) and
    `company_kind`. `label` is the derived display word.

    Repos linked to a workstream (`repos.project_id`) do NOT appear as
    separate entries — they render as `_repo-<name>.md` files inside the
    parent's working dir (`linked_repos`).
    """

    code: str  # canonical id: slug(full_job_name), e.g. ggl-5188-activation
    name: str  # full_job_name
    company_kind: CompanyKind
    company_code: str | None  # GGL, IBX, 1PI, CNC, ...
    company_name: str | None  # Google, First Person, Canonic, ...

    # One vocabulary for every workstream: MC_STATUSES
    # (Deal | Open | Holding | Closed | Archived). Active = Deal ∪ Open.
    status: str

    # MC-2's flag as stored. Internal workstreams carry True; it gates
    # NOTHING in the engine any more (they deserve working dirs like every
    # other workstream) and is kept for the allocation rollup's
    # engagement-vs-internal hours split.
    is_internal: bool
    owner: str | None
    last_touched: datetime | None
    deadline: datetime | None  # not tracked yet for either source
    one_line_summary: str | None = None  # regenerated during deepening pass

    # Days the hand-written Exec Summary trails real project activity, or None
    # when it is current / undatable. The one_line_summary above is derived
    # from that hand-written region and nothing automated refreshes it, so a
    # busy project can render months-old prose as current fact. Set during the
    # same deepening pass; see summary.summary_stale_days.
    summary_stale_days: int | None = None

    # The newest dated decision recorded for this project since the Exec
    # Summary was written, as (text, ISO date) — None when the summary is
    # current or nothing newer exists. Rendered BESIDE the summary, never
    # as it: the model owns summary prose (2026-06-30-exec-summary.md).
    latest_signal: tuple[str, str] | None = None

    # When the WORK last moved, from the newest human-dated sprint-file bullet
    # (#260). Distinct from `last_touched` above, which is MC-2's
    # `projects.updated_at` — the job RECORD's mtime, moved by a status flip or
    # a budget edit and not by a week of delivery. Ten projects rendered a May
    # date on 2026-09-15 while several had spine writes that same week.
    #
    # `last_touched` is NOT redefined: `render.is_closed_recent` asks "when did
    # the record change" and is right to. This is a second field, not a fix to
    # that one.
    #
    # WHY NOT mtime. #260 first tried the spine-file mtime `summary_stale_days`
    # uses. Every project landed on the SAME date, because one tenant-wide
    # ingest rewrites every sprint file — a column reading "today" for all 31
    # encodes when sync ran, which is worse than an obviously stale May date
    # because it looks current. Human-assigned dates do not move when a file is
    # rewritten; measured across 51 sprint-file projects they spread May →
    # September and differentiate (ggl-5185 correctly reads August).
    #
    # None when the project has no dated bullet at all — 6 of the rendered
    # projects, all repos/initiatives. Renders as an em dash: no signal is the
    # honest answer, and inventing one is the failure mode above.
    last_activity: date | None = None

    # MC-2 row uuid (`projects.id`). Threaded through so the spine mirror
    # can key `spine_elements.project_id` without re-querying. None only
    # when a fake/legacy state is built without one.
    mc2_id: str | None = None

    # The commercial envelope, when `has_agreement`.
    deal_stage: str | None = None
    budget: float | None = None
    dropbox_folder_url: str | None = None

    # Kept for the `_repo.md`-era view shape; no reader sets them from MC-2
    # any more (standalone repos are gone — every repo hangs off a
    # workstream via `linked_repos`). Templates still read the keys.
    github_org: str | None = None
    repo_name: str | None = None
    description: str | None = None

    # Populated from tenant config's per-project `contacts` array. Plain
    # dicts (typically `{"name": "...", "role": "..."}`) keep the shape
    # flexible without forcing a contact schema on every consumer.
    contacts: tuple[dict, ...] = ()

    # Repos in MC-2 linked to this workstream via `repos.project_id`. Each
    # gets its own `_repo-<repo-name>.md` written into the working dir.
    linked_repos: tuple[LinkedRepo, ...] = ()

    # Workstream shape (mc-2 mig 190+, cp-engine #300/#301). `parent_code`
    # is the canonical code of the parent workstream, None at the top of a
    # company. `has_agreement` is `deal_stage IS NOT NULL` — the commercial
    # envelope exists — and is what the engine branches on; `label` is the
    # DERIVED display word (account | program | job | initiative), never
    # authored (design doc 2026-09-22, decision 3).
    parent_code: str | None = None
    has_agreement: bool = False
    label: WorkstreamLabel | None = None


# The derived display label. Evaluated in this order, first match wins:
# account = no parent, under a client company; program = has children;
# job = has an agreement; initiative = none of the above.
WorkstreamLabel = Literal["account", "program", "job", "initiative"]


def derive_label(
    *, company_kind: str, parent_code: str | None, has_agreement: bool, has_children: bool
) -> WorkstreamLabel:
    """The display label for a workstream, from its shape alone.

    Rendering and reference-style only — the engine branches on
    `has_agreement`, `parent_code` and `company_kind` directly.
    """
    if parent_code is None and company_kind == "client":
        return "account"
    if has_children:
        return "program"
    if has_agreement:
        return "job"
    return "initiative"


@dataclass(frozen=True)
class LinkedRepo:
    """A repo in MC-2 linked to a workstream via `repos.project_id`.

    Carries just enough to render `_repo-<repo-name>.md` under the parent
    working dir: the GitHub coordinate, status, and a short description.
    """

    repo_name: str
    github_org: str
    status: str  # repos.status (Active | Holding | Inactive); Inactive is filtered by the reader
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
    # Optional due date (`· by YYYY-MM-DD` in the bracket) — the deadline the
    # ask carries; prep + the attention digest escalate against it (#70).
    by: str | None = None


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
