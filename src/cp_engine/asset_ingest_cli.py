"""CLI glue for 1P asset-ingest (Task C8).

Thin helpers the `cp` asset verbs lean on so the command bodies in `cli.py`
stay declarative. Nothing here re-implements asset logic — it builds the MC-2
client (reusing the same cred resolver `ingest_project_assets` uses internally),
enumerates active client projects for `cp ingest-assets --all`, and formats the
run summaries.

Scope mapping for `cp ingest-assets --scope <scope>`:
  - `1p`     → all active client engagements (identical to bare `--all`).
  - `fpsf`   → internal initiatives/repos; asset ingest is client-only → no-op.
  - `canonic`→ internal initiatives/repos; asset ingest is client-only → no-op.

"Active client project" = an MC-2 `projects` row with `is_internal=false`,
`mc_status in ('Deal','Open')`, owned by a company whose `kind == 'client'`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Scopes that have no client engagements: asset ingest skips them entirely.
INTERNAL_SCOPES = ("fpsf", "canonic")

# Explicit column list (never `*`) per Drew's global Supabase rule — `projects`
# carries multi-megabyte JSONB cache columns we must not pull. The nested
# `companies(code, kind)` embed gives us the client-scope guard + the code prefix.
_ACTIVE_PROJECT_COLUMNS = "number, mc_status, is_internal, companies(code, kind)"


def build_mc2_client():
    """Build a Supabase client using the same cred resolution the glue uses.

    Mirrors `ingest_project_assets`'s internal `_resolve_creds` so the CLI and
    glue always agree on which creds win (env first, then mc-2/backend/.env via
    `_load_supabase_creds`).
    """
    from supabase import create_client

    from cp_engine import config as cp_config
    from cp_engine.sync_mc2 import _load_supabase_creds

    url, key = _load_supabase_creds(cp_config.load(Path.cwd()))
    return create_client(url, key)


def _company_embed(row: dict) -> dict:
    """Lift the to-one `companies` embed, defending against the list shape.

    PostgREST returns a to-one embed as either a dict or a single-element list;
    match the guard in `resolve_project_folders`.
    """
    companies = row.get("companies") or {}
    if isinstance(companies, list):
        companies = companies[0] if companies else {}
    return companies if isinstance(companies, dict) else {}


def active_client_project_codes(client) -> list[str]:
    """Enumerate cp codes for every active *client* project in MC-2.

    Active client = `is_internal=false` AND `mc_status in ('Deal','Open')` AND
    the owning company's `kind == 'client'`. Each code is built as
    `<company.code>-<number>` so the human-readable code surfaces in summaries;
    `resolve_project_folders` parses out the number regardless of prefix, so a
    missing company code degrades to the bare number rather than dropping the
    project.
    """
    rows = (
        client.table("projects")
        .select(_ACTIVE_PROJECT_COLUMNS)
        .eq("is_internal", False)
        .in_("mc_status", ["Deal", "Open"])
        .execute()
        .data
        or []
    )

    codes: list[str] = []
    for row in rows:
        company = _company_embed(row)
        if (company.get("kind") or "") != "client":
            continue
        number = row.get("number")
        if number is None:
            continue
        prefix = (company.get("code") or "").strip().lower()
        codes.append(f"{prefix}-{number}" if prefix else str(number))
    return codes


@dataclass
class _ProjectOutcome:
    """One project's slot in a `--all` run."""

    code: str
    created: int = 0
    versioned: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None  # whole-project error (resolve/run blew up)


@dataclass
class FanOutResult:
    """Aggregate of a `cp ingest-assets --all` run."""

    outcomes: list[_ProjectOutcome] = field(default_factory=list)

    @property
    def total_created(self) -> int:
        return sum(o.created for o in self.outcomes)

    @property
    def total_versioned(self) -> int:
        return sum(o.versioned for o in self.outcomes)

    @property
    def total_skipped(self) -> int:
        return sum(o.skipped for o in self.outcomes)

    @property
    def total_failed(self) -> int:
        return sum(o.failed for o in self.outcomes)

    @property
    def any_failures(self) -> bool:
        """True if any per-file failure OR any whole-project error occurred."""
        return any(o.failed or o.failures or o.error for o in self.outcomes)


def fan_out_ingest(client, codes: list[str]) -> FanOutResult:
    """Run `ingest_project_assets` for each code, collecting per-project outcomes.

    One project raising (or returning failures) NEVER stops the others — every
    project gets a slot in the result, and whole-project exceptions are captured
    on `.error` so the caller can surface them and exit non-zero.
    """
    from cp_engine.asset_ingest import ingest_project_assets

    result = FanOutResult()
    for code in codes:
        outcome = _ProjectOutcome(code=code)
        try:
            run = ingest_project_assets(code, client=client)
            outcome.created = run.created
            outcome.versioned = run.versioned
            outcome.skipped = run.skipped
            outcome.failed = run.failed
            outcome.failures = list(run.failures)
        except Exception as exc:  # noqa: BLE001 — collect, keep going
            outcome.error = str(exc)
        result.outcomes.append(outcome)
    return result
