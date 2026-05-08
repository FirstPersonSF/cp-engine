"""MC-2 sync backend — reads project state from MC-2's Postgres via Supabase.

Used by `cp-1p` and `cp-firstpersonsf` (the FPSF tenants whose source of
truth is MC-2's `projects` table).

Auth: reads `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` from the environment.
The service key is required (not the anon key) because the engine reads
across all rows for the master CP — RLS would otherwise filter the result.

This backend reads project-level state for the master CP only. Project
CPs' engine-managed regions are not populated by this backend in v0.1
(see spec v02 §4.2 / Decision A: defer to v0.2 when github-issues backend
adds per-issue tracking).

# Project identity

The canonical project identifier in cp-engine is the **company-prefixed
project number**, e.g. `ggl-5188`. Format: `<companies.code lowercased>-<projects.number>`.
Per spec v02 §3.3.

When a project has no `company_id` (legacy data, ~30/57 rows in May 2026)
the prefix is omitted and the ID is just the lowercased number — `5026`.
This is honest about the data state; once `company_id` is backfilled in
MC-2, those projects will pick up their prefix on next sync.

`projects.code` is NOT used as the identifier. The legacy `code` column
holds inconsistent display slugs (mix of casing, underscores, number-only
rows, even duplicates) and is not load-bearing for the engine.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from supabase import Client, create_client

from cp_engine.config import TenantConfig
from cp_engine.status import MC_STATUSES
from cp_engine.state import ProjectState
from cp_engine.sync import BackendUnavailable


# Columns we read. Explicit list (never `*`) per Drew's global rule about
# Supabase performance. `cached_messages` and `cached_analysis` on the
# projects table can be megabytes per row — never select them.
#
# `companies(code)` is a PostgREST embed: pulls the related companies row's
# `code` field via the `company_id` foreign key.
_PROJECT_COLUMNS = (
    "number, full_job_name, name, mc_status, account_manager, "
    "is_internal, updated_at, companies(code)"
)


class MC2Backend:
    """Reads project state from MC-2's Postgres.

    Stateless aside from the lazily-constructed Supabase client.
    """

    _client: Client | None = None

    def read_projects(self, config: TenantConfig) -> tuple[ProjectState, ...]:
        client = self._get_client()

        # Filter at SQL: skip Archived (master CP doesn't surface them at
        # all). is_internal filtering happens in the renderer based on what
        # each tenant wants displayed — keep the filter close to display.
        resp = (
            client.schema("public")
            .table("projects")
            .select(_PROJECT_COLUMNS)
            .neq("mc_status", "Archived")
            .order("updated_at", desc=True)
            .execute()
        )

        rows = resp.data or []
        return tuple(_row_to_state(row) for row in rows if _row_is_valid(row))

    def _get_client(self) -> Client:
        if self._client is not None:
            return self._client

        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_KEY")
        if not url or not key:
            raise BackendUnavailable(
                "MC-2 backend requires SUPABASE_URL and SUPABASE_SERVICE_KEY in the "
                "environment. For local dev, copy mc-2/backend/.env.example. For the "
                "GitHub Action, configure repo secrets."
            )

        self._client = create_client(url, key)
        return self._client


# ──────────────────────────────────────────────────────────────────────
#  Row → ProjectState transformation (pure; unit-testable)
# ──────────────────────────────────────────────────────────────────────


def _row_is_valid(row: dict) -> bool:
    """Defensive: skip rows MC-2 should never produce but technically could.

    `number` is NOT NULL on the schema and unique in practice — but defend
    anyway. Skipped rows are dropped silently (no exception); better to
    surface 99 of 100 projects than 0.
    """
    if row.get("number") is None:
        return False
    if row.get("mc_status") not in MC_STATUSES:
        # Includes the case where the post-migration code is in place but
        # this row somehow still has the old vocab. Skip; it'd render wrong.
        return False
    return True


def _row_to_state(row: dict) -> ProjectState:
    """Transform one DB row into a ProjectState.

    The canonical project ID is derived from `number` (always present) and
    `companies.code` (often present, sometimes NULL for legacy rows).
    """
    return ProjectState(
        code=_canonical_id(row),
        # Prefer full_job_name (the trigger-maintained one), fall back to
        # name. Both can be None; we settle on "" if absolutely nothing.
        name=row.get("full_job_name") or row.get("name") or "",
        status=row["mc_status"],
        is_internal=bool(row.get("is_internal", False)),
        owner=row.get("account_manager") or None,
        last_touched=_parse_iso(row.get("updated_at")),
        deadline=None,  # MC-2 doesn't track deadlines on projects
        one_line_summary=None,  # set by the deepening pass, not by sync
    )


def _canonical_id(row: dict) -> str:
    """Build `<prefix>-<number>` (or just `<number>` when prefix unavailable).

    Examples:
        {number: 5188, companies: {code: "GGL"}}  -> "ggl-5188"
        {number: 5026, companies: None}           -> "5026"
        {number: 5026, companies: {code: ""}}     -> "5026"
    """
    number = row["number"]
    company = row.get("companies") or {}
    prefix = (company.get("code") or "").strip().lower() if isinstance(company, dict) else ""
    return f"{prefix}-{number}" if prefix else str(number)


def _parse_iso(s: str | None) -> datetime | None:
    """Parse a Supabase ISO-8601 timestamp string to datetime.

    Supabase returns timestamps like `2026-05-07T16:14:34.123456+00:00`.
    `datetime.fromisoformat` handles those directly on Python 3.11+.
    """
    if not s:
        return None
    parsed = datetime.fromisoformat(s)
    # Supabase always returns tz-aware timestamps for `timestamp with time zone`
    # columns. Defensive: if it ever returned naive, treat as UTC.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
