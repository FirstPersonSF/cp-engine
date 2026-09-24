"""MC-2 sync backend — reads workstream state from MC-2's Postgres.

ONE stream (#301): `public.projects WHERE mc_status != 'Archived'`, joined
to `companies` for the kind/code/name and to `repos` on `project_id` for
the linked-repo files. Client jobs, account and program nodes, and
internal workstreams (Mission Control, StoryOS, …) are all rows of that
one table; the engine tells them apart by SHAPE (`has_agreement`,
`parent_code`, `company_kind`), never by which table they came from.
Standalone repos no longer exist — every repo hangs off a workstream.

Auth: reads `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` from the environment,
falling back to `<mc-2 clone>/backend/.env` (clone path from
`TenantConfig.local_repos["mc-2"]`) when env vars are missing. The service
key is required (not the anon key) because the engine reads across all
rows for the master CP — RLS would otherwise filter the result.

# Project identity

`slug(full_job_name)`: `ggl-5136-go-safety-website`,
`1pi-9005-mission-control`. See `cp_engine.codes` for the parser.
"""

from __future__ import annotations

from datetime import datetime, timezone

from supabase import Client

from cp_engine import mc2_db
from cp_engine.config import TenantConfig
from cp_engine.mc2_db import Tables
from cp_engine.state import (
    derive_label,
    LinkedRepo,
    PersonHours,
    PersonRollup,
    ProjectAllocation,
    ProjectState,
    WeeklyAllocations,
)
from cp_engine.status import MC_STATUSES

# Columns we read for workstream rows. Explicit list (never `*`) per Drew's
# global rule about Supabase performance. `cached_messages` and
# `cached_analysis` on the projects table can be megabytes per row.
#
# `repos!project_id(...)` pulls the set of repos linked to this workstream.
# We surface them via per-repo `_repo-<repo-name>.md` files in the working
# dir. Inactive repos are filtered downstream in `_parse_linked_repos`
# (PostgREST nested filters are awkward; cleaner to filter in Python given
# the small row count).
_PROJECT_COLUMNS = mc2_db.PROJECTS_SYNC_COLUMNS


class MC2Backend:
    """Reads workstream state from MC-2's Postgres."""

    _client: Client | None = None

    def read_projects(self, config: TenantConfig) -> tuple[ProjectState, ...]:
        client = self._get_client(config)
        rows = (
            client.schema("public")
            .table(Tables.PROJECTS)
            .select(_PROJECT_COLUMNS)
            .neq("mc_status", "Archived")
            .order("updated_at", desc=True)
            .execute()
            .data
            or []
        )
        return workstream_rows_to_states(rows)

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
_load_dropbox_creds = mc2_db.load_dropbox_creds
_mc2_env_file = mc2_db._mc2_env_file
_read_dotenv = mc2_db._read_dotenv


# ──────────────────────────────────────────────────────────────────────
#  Workstream row → ProjectState
# ──────────────────────────────────────────────────────────────────────


def _engagement_row_is_valid(row: dict) -> bool:
    """Defensive: skip rows MC-2 should never produce but technically could."""
    if row.get("number") is None:
        return False
    if row.get("mc_status") not in MC_STATUSES:
        return False
    return True


def _engagement_row_to_state(row: dict) -> ProjectState:
    """Transform one `projects` row into a ProjectState (shape fields
    `parent_code` / `label` are filled by `workstream_rows_to_states`)."""
    company = row.get("companies") or {}
    if not isinstance(company, dict):
        company = {}
    kind = company.get("kind") or "client"

    return ProjectState(
        code=_engagement_canonical_id(row),
        name=row.get("full_job_name") or row.get("name") or "",
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
        # The commercial envelope exists ⇔ the row is in a deal stage. Budget
        # is NOT the signal: live jobs 5151/5168 carry none (plan D2).
        has_agreement=row.get("deal_stage") is not None,
    )


def _is_account_node(row: dict, has_children: bool) -> bool:
    """The root workstream a client company gets in mc-2 mig 191.

    No parent, no agreement, under a client company, and at least one child
    (191 only creates one where a project exists). The child condition is
    what keeps a real job with an unfilled `deal_stage` from being mistaken
    for one. Since #302 account nodes flow through `read_projects` like any
    workstream (label `account`, working dir = the existing `1p/<company>/`
    account dir); this predicate is kept for callers that need the shape
    without the label.
    """
    company = row.get("companies") or {}
    if not isinstance(company, dict):
        company = {}
    return (
        row.get("parent_id") is None
        and row.get("deal_stage") is None
        and (company.get("kind") or "client") == "client"
        and has_children
    )


def workstream_rows_to_states(rows: list[dict]) -> tuple[ProjectState, ...]:
    """`projects` rows → ProjectStates (#300, #301).

    One pass builds every state through `_engagement_row_to_state`, then
    **parent_code / label** are filled from the row set (a parent outside
    the non-Archived read leaves `parent_code=None`; that is a parent that
    is itself archived, and the child renders at the top of its company).
    Internal workstreams come through exactly as MC-2 stores them — real
    `mc_status`, `is_internal=True` — and nothing downstream maps or gates
    on either. **Account nodes** come through too (#302), labelled
    `account`; sync gives them the existing `1p/<company>/` dir.
    """
    from dataclasses import replace

    valid = [r for r in rows if _engagement_row_is_valid(r)]
    by_id: dict[str, dict] = {r["id"]: r for r in valid if r.get("id")}
    children_of: dict[str, int] = {}
    for r in valid:
        pid = r.get("parent_id")
        if pid:
            children_of[pid] = children_of.get(pid, 0) + 1

    out: list[ProjectState] = []
    for r in valid:
        has_children = children_of.get(r.get("id"), 0) > 0
        state = _engagement_row_to_state(r)
        parent = by_id.get(r.get("parent_id") or "")
        parent_code = _engagement_canonical_id(parent) if parent else None
        label = derive_label(
            company_kind=state.company_kind,
            parent_code=parent_code,
            has_agreement=state.has_agreement,
            has_children=has_children,
        )
        out.append(replace(state, parent_code=parent_code, label=label))
    return tuple(out)


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
