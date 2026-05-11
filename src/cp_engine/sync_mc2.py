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

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from supabase import Client, create_client

from cp_engine.config import TenantConfig
from cp_engine.status import MC_STATUSES
from cp_engine.state import (
    PersonHours,
    PersonRollup,
    ProjectAllocation,
    ProjectState,
    WeeklyAllocations,
)
from cp_engine.sync import BackendUnavailable


# Columns we read for engagement projects. Explicit list (never `*`) per
# Drew's global rule about Supabase performance. `cached_messages` and
# `cached_analysis` on the projects table can be megabytes per row.
_ENGAGEMENT_COLUMNS = (
    "number, full_job_name, name, mc_status, account_manager, "
    "is_internal, deal_stage, budget, dropbox_folder_url, updated_at, "
    "companies(code, name, kind)"
)

# Columns for standalone repo rows.
_REPO_COLUMNS = (
    "id, repo_name, status, description, owner, updated_at, "
    "github_orgs!inner(name), "
    "companies!inner(code, name, kind)"
)

_VALID_REPO_STATUSES = {"Active", "Holding", "Inactive"}


class MC2Backend:
    """Reads project + repo state from MC-2's Postgres."""

    _client: Client | None = None

    def read_projects(self, config: TenantConfig) -> tuple[ProjectState, ...]:
        client = self._get_client(config)

        # Stream A: engagement projects
        engagement_rows = (
            client.schema("public")
            .table("projects")
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

        # Stream B: standalone repos (no engagement link)
        # PostgREST: `is.null` for "project_id IS NULL"
        repo_rows = (
            client.schema("public")
            .table("repos")
            .select(_REPO_COLUMNS)
            .is_("project_id", "null")
            .neq("status", "Inactive")
            .order("updated_at", desc=True)
            .execute()
            .data
            or []
        )
        repos = tuple(
            _repo_row_to_state(row) for row in repo_rows if _repo_row_is_valid(row)
        )

        return engagements + repos

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
            .table("sprint_allocations")
            .select(
                "hours, "
                "entities!inner(name), "
                "projects!inner(number, is_internal, companies(code))"
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

        url, key = _load_supabase_creds(config)
        self._client = create_client(url, key)
        return self._client


# ──────────────────────────────────────────────────────────────────────
#  Supabase credential loading
# ──────────────────────────────────────────────────────────────────────


_SUPABASE_KEYS = ("SUPABASE_URL", "SUPABASE_SERVICE_KEY")


def _load_supabase_creds(config: TenantConfig) -> tuple[str, str]:
    """Resolve `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` for the MC-2 client.

    Order of preference:
      1. `os.environ` — preserves CI/GitHub Actions and explicit shell exports.
      2. `<mc-2 clone>/backend/.env` — the canonical local-dev location.
         The clone path comes from `TenantConfig.local_repos["mc-2"]`
         (per-machine, gitignored).

    On fallback, prints a one-line note to stderr so the implicit dependency
    stays visible. Raises `BackendUnavailable` if both sources lack the keys,
    naming both paths it tried.
    """
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if url and key:
        return url, key

    env_file = _mc2_env_file(config)
    file_creds = _read_dotenv(env_file, _SUPABASE_KEYS) if env_file else {}
    url = url or file_creds.get("SUPABASE_URL")
    key = key or file_creds.get("SUPABASE_SERVICE_KEY")
    if url and key:
        print(f"Loaded SUPABASE_* from {env_file}", file=sys.stderr)
        return url, key

    tried = "environment"
    if env_file:
        tried += f" and {env_file}"
    else:
        tried += " (no MC-2 clone configured in [local-repos] of .cp-engine.local.toml)"
    raise BackendUnavailable(
        "MC-2 backend requires SUPABASE_URL and SUPABASE_SERVICE_KEY. "
        f"Tried: {tried}. For local dev, copy mc-2/backend/.env.example "
        "and ensure [local-repos].\"mc-2\" points to your clone. For the "
        "GitHub Action, configure repo secrets."
    )


def _mc2_env_file(config: TenantConfig) -> Path | None:
    """Return `<mc-2 clone>/backend/.env` if the clone is configured, else None."""
    clone = config.local_repos.get("mc-2")
    if clone is None:
        return None
    return Path(clone) / "backend" / ".env"


def _read_dotenv(path: Path, keys: tuple[str, ...]) -> dict[str, str]:
    """Parse a dotenv file, returning only the requested keys.

    Trivial parser: `KEY=value` per line, skips comments and blanks, strips
    surrounding single or double quotes. Doesn't handle multi-line values or
    `export` prefixes — MC-2's .env doesn't use them.
    """
    if not path.is_file():
        return {}
    wanted = set(keys)
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k not in wanted:
            continue
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        out[k] = v
    return out


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
    )


def _engagement_canonical_id(row: dict) -> str:
    """`<prefix>-<number>` or just `<number>` if no company_id."""
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
