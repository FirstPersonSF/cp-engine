"""MC-2 sync backend — reads project + repo state from MC-2's Postgres.

Two source streams unified into a single tuple of ProjectStates:

1. **Engagements** — `public.projects WHERE mc_status != 'Archived'`,
   joined to `companies` for the kind/code/name. These are client work
   tracked through MC-2's engagement lifecycle (Deal → Open → Closed).

2. **Standalone repos** — `public.repos WHERE project_id IS NULL`, joined
   to `companies` and `github_orgs`. These are code repos NOT linked to
   a specific engagement (mc-2, storyos, unf-forge, etc.). Repos that
   ARE linked to an engagement are intentionally excluded here — their
   info enriches the parent engagement's project CP, not the master
   index.

Auth: reads `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` from the environment,
falling back to `<mc-2 clone>/backend/.env` (clone path from
`TenantConfig.local_repos["mc-2"]`) when env vars are missing. The service
key is required (not the anon key) because the engine reads across all
rows for the master CP — RLS would otherwise filter the result.

# Project identity

Engagements: `<companies.code lowercased>-<projects.number>`. Legacy rows
without `company_id` fall back to just the number.

Repos: `<repos.repo_name>` directly. GitHub slugs are already lowercase
hyphenated; no transformation needed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from supabase import Client

from cp_engine import mc2_db
from cp_engine.config import TenantConfig
from cp_engine.mc2_db import Tables
from cp_engine.state import (
    LinkedRepo,
    PersonHours,
    PersonRollup,
    ProjectAllocation,
    ProjectState,
    WeeklyAllocations,
)
from cp_engine.status import INITIATIVE_STATUSES, MC_STATUSES

# Columns we read for engagement projects. Explicit list (never `*`) per
# Drew's global rule about Supabase performance. `cached_messages` and
# `cached_analysis` on the projects table can be megabytes per row.
#
# `repos!project_id(...)` pulls the set of repos linked to this engagement
# (those with `repos.project_id` pointing at this project). We surface them
# via per-repo `_repo-<repo-name>.md` files in the engagement's working dir,
# mirroring the standalone-repo `_repo.md` layout. Inactive repos are filtered
# downstream in `_engagement_row_to_state` (PostgREST nested filters are
# awkward; cleaner to filter in Python given the small row count).
_ENGAGEMENT_COLUMNS = mc2_db.PROJECTS_SYNC_COLUMNS

# Columns for standalone repo rows.
_REPO_COLUMNS = mc2_db.REPOS_SYNC_COLUMNS

# Columns we read for initiatives. Same `repos!initiative_id(...)` join
# pattern as engagements use for engagement-linked repos — initiative
# membership surfaces as `_repo-<name>.md` files under the initiative's
# working dir.
_INITIATIVE_COLUMNS = mc2_db.INITIATIVES_SYNC_COLUMNS

_VALID_REPO_STATUSES = {"Active", "Holding", "Inactive"}


class MC2Backend:
    """Reads project + repo state from MC-2's Postgres."""

    _client: Client | None = None

    def read_projects(self, config: TenantConfig) -> tuple[ProjectState, ...]:
        client = self._get_client(config)

        # Stream A: engagement projects
        engagement_rows = (
            client.schema("public")
            .table(Tables.PROJECTS)
            .select(_ENGAGEMENT_COLUMNS)
            .neq("mc_status", "Archived")
            .order("updated_at", desc=True)
            .execute()
            .data
            or []
        )
        engagements = tuple(
            _engagement_row_to_state(row)
            for row in engagement_rows
            if _engagement_row_is_valid(row)
        )

        # Stream B: standalone repos (linked to neither an engagement nor
        # an initiative). PostgREST: `is.null` for "<col> IS NULL". A repo
        # with `initiative_id` set surfaces as a `_repo-<name>.md` under
        # its initiative's working dir, NOT as a top-level standalone row.
        repo_rows = (
            client.schema("public")
            .table(Tables.REPOS)
            .select(_REPO_COLUMNS)
            .is_("project_id", "null")
            .is_("initiative_id", "null")
            .neq("status", "Inactive")
            .order("updated_at", desc=True)
            .execute()
            .data
            or []
        )
        repos = tuple(
            _repo_row_to_state(row) for row in repo_rows if _repo_row_is_valid(row)
        )

        # Stream C: initiatives (internal workstreams — Mission Control,
        # StoryOS, etc.). Parallel to engagements but with `source="initiative"`.
        # Linked repos via `repos.initiative_id` surface as `_repo-<name>.md`
        # under the initiative's working dir, mirroring engagement-linked-repo
        # rendering.
        initiative_rows = (
            client.schema("public")
            .table(Tables.INITIATIVES)
            .select(_INITIATIVE_COLUMNS)
            .neq("status", "Archived")
            .order("updated_at", desc=True)
            .execute()
            .data
            or []
        )
        initiatives = tuple(
            _initiative_row_to_state(row)
            for row in initiative_rows
            if _initiative_row_is_valid(row)
        )

        return engagements + repos + initiatives

    def read_allocations(
        self,
        config: TenantConfig,
        week_start: str,
    ) -> WeeklyAllocations:
        """Read sprint_allocations for one week, indexed two ways.

        Returns:
            WeeklyAllocations with `by_project` (canonical_id -> ProjectAllocation)
            and `rollup` (per-person totals split engagement vs internal admin).

        Args:
            week_start: ISO date string (YYYY-MM-DD), expected to be a Monday.
        """
        client = self._get_client(config)
        rows = (
            client.schema("public")
            .table(Tables.SPRINT_ALLOCATIONS)
            .select(
                "hours, "
                "entities!inner(name), "
                "projects!inner(number, full_job_name, is_internal, companies(code))"
            )
            .eq("week_start", week_start)
            .execute()
            .data
            or []
        )

        # Group by project (canonical id) and by person.
        by_project_raw: dict[str, dict[str, float]] = {}  # code -> {person -> hours}
        is_internal_by_project: dict[str, bool] = {}
        person_engagement: dict[str, float] = {}
        person_engagement_projects: dict[str, set[str]] = {}
        person_internal: dict[str, float] = {}

        for row in rows:
            project = row.get("projects") or {}
            entity = row.get("entities") or {}
            if not isinstance(project, dict) or not isinstance(entity, dict):
                continue
            person = entity.get("name")
            number = project.get("number")
            if not person or number is None:
                continue
            code = _canonical_id_from_project_join(project)
            is_internal = bool(project.get("is_internal", False))
            try:
                hours = float(row["hours"])
            except (TypeError, ValueError, KeyError):
                continue

            # Per-project (skip internal — they don't render per-row, but we
            # still feed them into per-person rollup below).
            if not is_internal:
                by_project_raw.setdefault(code, {})
                by_project_raw[code][person] = by_project_raw[code].get(person, 0.0) + hours
                is_internal_by_project[code] = False
            else:
                # Per-project entries for internal projects are not surfaced
                # in by_project (they're rolled into the per-person summary).
                pass

            # Per-person rollup: split engagement vs internal.
            if is_internal:
                person_internal[person] = person_internal.get(person, 0.0) + hours
            else:
                person_engagement[person] = person_engagement.get(person, 0.0) + hours
                person_engagement_projects.setdefault(person, set()).add(code)

        # Build ProjectAllocation entries (sorted by hours desc, then name).
        by_project: dict[str, ProjectAllocation] = {}
        for code, person_hours in by_project_raw.items():
            entries = tuple(
                sorted(
                    (PersonHours(person_name=name, hours=hrs) for name, hrs in person_hours.items()),
                    key=lambda e: (-e.hours, e.person_name),
                )
            )
            by_project[code] = ProjectAllocation(
                project_code=code,
                is_internal=is_internal_by_project.get(code, False),
                entries=entries,
            )

        # Build PersonRollup list (sorted by total hours desc).
        all_people = set(person_engagement.keys()) | set(person_internal.keys())
        rollup_list = [
            PersonRollup(
                person_name=p,
                engagement_hours=person_engagement.get(p, 0.0),
                engagement_project_count=len(person_engagement_projects.get(p, set())),
                internal_hours=person_internal.get(p, 0.0),
            )
            for p in all_people
        ]
        rollup_list.sort(key=lambda r: (-r.total_hours, r.person_name))

        return WeeklyAllocations(
            week_start=week_start,
            by_project=by_project,
            rollup=tuple(rollup_list),
        )

    def _get_client(self, config: TenantConfig) -> Client:
        if self._client is not None:
            return self._client

        self._client = mc2_db.get_client(config)
        return self._client

    def spine_client(self) -> Client:
        """Return the Supabase client read_projects already created.

        Implements sync's opt-in ``SpineClientProvider`` capability (the
        method left the core ``Backend`` protocol in arch-phase-4, #34).
        The spine mirror reuses the exact client (and creds) the project
        read used — never mints a fresh connection."""
        if self._client is None:
            raise RuntimeError(
                "spine_client() called before read_projects(); no client cached."
            )
        return self._client


# ──────────────────────────────────────────────────────────────────────
#  Supabase credential loading — MOVED to cp_engine.mc2_db (arch-phase-3)
# ──────────────────────────────────────────────────────────────────────
#
# Back-compat re-exports for names with live importers (cli.py, mcp_server,
# slack, prep_planning + their tests). New code should import from mc2_db.
# CAUTION: these are import-time value bindings — monkeypatching them here
# does NOT affect mc2_db internals or any module that calls mc2_db directly;
# patch `cp_engine.mc2_db.<name>` instead.

_load_supabase_creds = mc2_db.load_supabase_creds
_load_ingest_creds = mc2_db.load_ingest_creds
_mc2_env_file = mc2_db._mc2_env_file
_read_dotenv = mc2_db._read_dotenv


# ──────────────────────────────────────────────────────────────────────
#  Engagement row → ProjectState
# ──────────────────────────────────────────────────────────────────────


def _engagement_row_is_valid(row: dict) -> bool:
    """Defensive: skip rows MC-2 should never produce but technically could."""
    if row.get("number") is None:
        return False
    if row.get("mc_status") not in MC_STATUSES:
        return False
    return True


def _engagement_row_to_state(row: dict) -> ProjectState:
    """Transform an engagement-projects row into a ProjectState."""
    company = row.get("companies") or {}
    if not isinstance(company, dict):
        company = {}
    kind = company.get("kind") or "client"

    return ProjectState(
        code=_engagement_canonical_id(row),
        name=row.get("full_job_name") or row.get("name") or "",
        source="engagement",
        mc2_id=row.get("id"),
        company_kind=kind,  # type: ignore[arg-type]
        company_code=company.get("code"),
        company_name=company.get("name"),
        status=row["mc_status"],
        is_internal=bool(row.get("is_internal", False)),
        owner=row.get("account_manager") or None,
        last_touched=_parse_iso(row.get("updated_at")),
        deadline=None,
        deal_stage=row.get("deal_stage"),
        budget=_parse_numeric(row.get("budget")),
        dropbox_folder_url=row.get("dropbox_folder_url") or None,
        linked_repos=_parse_linked_repos(row.get("repos")),
    )


def _parse_linked_repos(repos_payload: object) -> tuple[LinkedRepo, ...]:
    """Build LinkedRepos from the embedded `repos!project_id(...)` payload.

    Filters out Inactive repos (PostgREST nested filtering is awkward; we
    keep the query simple and prune in Python given the tiny row count
    per engagement). Skips rows missing a github_org or repo_name — defensive
    against data-quality issues in MC-2.
    """
    if not isinstance(repos_payload, list):
        return ()
    out: list[LinkedRepo] = []
    for r in repos_payload:
        if not isinstance(r, dict):
            continue
        status = r.get("status") or "Active"
        if status == "Inactive":
            continue
        repo_name = (r.get("repo_name") or "").strip()
        if not repo_name:
            continue
        org = r.get("github_orgs") or {}
        if not isinstance(org, dict):
            continue
        org_name = (org.get("name") or "").strip()
        if not org_name:
            continue
        desc = r.get("description") or None
        out.append(
            LinkedRepo(
                repo_name=repo_name,
                github_org=org_name,
                status=status,
                description=desc,
            )
        )
    out.sort(key=lambda r: r.repo_name)
    return tuple(out)


def _engagement_canonical_id(row: dict) -> str:
    """Canonical project id = slugified `full_job_name`
    ("ibx-5192-platform-sales-readiness-summit"). Falls back to the legacy
    `<company>-<number>` form only when `full_job_name` is empty (defensive;
    all live rows have it)."""
    from cp_engine.state import slug_full_job_name

    slug = slug_full_job_name(row.get("full_job_name"))
    if slug:
        return slug
    number = row["number"]
    company = row.get("companies") or {}
    prefix = (company.get("code") or "").strip().lower() if isinstance(company, dict) else ""
    return f"{prefix}-{number}" if prefix else str(number)


def _canonical_id_from_project_join(project: dict) -> str:
    """Same shape as _engagement_canonical_id but reads from a project sub-object
    in a join result (e.g. sprint_allocations → projects → companies)."""
    return _engagement_canonical_id(project)


# ──────────────────────────────────────────────────────────────────────
#  Repo row → ProjectState
# ──────────────────────────────────────────────────────────────────────


def _repo_row_is_valid(row: dict) -> bool:
    """Defensive: skip rows where the embedded joins came back empty."""
    if not row.get("repo_name"):
        return False
    if row.get("status") not in _VALID_REPO_STATUSES:
        return False
    if not row.get("github_orgs") or not row.get("companies"):
        # Inner joins in the SELECT should make this impossible, but
        # PostgREST occasionally returns empty objects; defend.
        return False
    return True


def _repo_row_to_state(row: dict) -> ProjectState:
    """Transform a repos row into a ProjectState."""
    org = row.get("github_orgs") or {}
    company = row.get("companies") or {}
    kind = company.get("kind") or "client"

    return ProjectState(
        code=row["repo_name"],
        name=row["repo_name"],
        source="repo",
        mc2_id=row.get("id"),
        company_kind=kind,  # type: ignore[arg-type]
        company_code=company.get("code"),
        company_name=company.get("name"),
        status=row["status"],
        is_internal=False,  # repos don't carry this flag
        owner=row.get("owner") or None,
        last_touched=_parse_iso(row.get("updated_at")),
        deadline=None,
        github_org=org.get("name"),
        repo_name=row["repo_name"],
        description=row.get("description"),
    )


# ──────────────────────────────────────────────────────────────────────
#  Initiative row → ProjectState
# ──────────────────────────────────────────────────────────────────────


def _initiative_row_is_valid(row: dict) -> bool:
    """Skip rows MC-2 should never produce but technically could."""
    if not row.get("code"):
        return False
    if row.get("status") not in INITIATIVE_STATUSES:
        return False
    return True


def _initiative_row_to_state(row: dict) -> ProjectState:
    """Transform an initiatives row into a ProjectState with source='initiative'.

    The canonical id is just `code` (no company prefix). Linked repos
    populate `linked_repos` via the same `repos!initiative_id(...)` join
    payload shape as engagements; the render pipeline doesn't need to
    distinguish — engagement-vs-initiative-owned repos render the same way.
    """
    company = row.get("companies") or {}
    if not isinstance(company, dict):
        company = {}
    kind = company.get("kind") or "self-fpsf"

    return ProjectState(
        code=(row.get("code") or "").strip(),
        name=row.get("name") or "",
        source="initiative",
        mc2_id=row.get("id"),
        company_kind=kind,  # type: ignore[arg-type]
        company_code=company.get("code"),
        company_name=company.get("name"),
        status=row["status"],
        # is_internal stays False even though initiatives are conceptually
        # internal work. The `is_internal` boolean on ProjectState is the
        # "skip this — it's an MC-2 pseudo-project that doesn't deserve a
        # working dir" gate used by the scaffolding loop in sync.py.
        # Initiatives DO deserve working dirs, so they opt out of that
        # gate by leaving is_internal=False. Internal-vs-external is
        # encoded by `source="initiative"` instead.
        is_internal=False,
        owner=row.get("owner") or None,
        last_touched=_parse_iso(row.get("updated_at")),
        deadline=None,
        # Engagement-specific fields stay None for initiatives.
        deal_stage=None,
        budget=None,
        dropbox_folder_url=None,
        # Linked repos use the same payload key/shape as engagements.
        linked_repos=_parse_linked_repos(row.get("repos")),
    )


# ──────────────────────────────────────────────────────────────────────
#  Shared helpers
# ──────────────────────────────────────────────────────────────────────


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    parsed = datetime.fromisoformat(s)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_numeric(v) -> float | None:
    """Parse Supabase numeric fields. Comes back as str or float; either way
    yields a float, or None if absent/unparseable."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
