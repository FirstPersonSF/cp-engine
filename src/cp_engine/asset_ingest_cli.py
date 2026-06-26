"""CLI glue for 1P asset-ingest (Task C8).

Thin helpers the `cp` asset verbs lean on so the command bodies in `cli.py`
stay declarative. Nothing here re-implements asset logic — it builds the MC-2
client (reusing the same cred resolver `ingest_project_assets` uses internally),
enumerates the ingestable items for `cp ingest-assets --all`, and formats the
run summaries.

Since Task 8, `--all` routes through `active_ingestable_codes` — the widened
set of active client engagements UNION active initiatives (the engagement-only
gate was lifted). See `active_ingestable_codes`.

Scope mapping for `cp ingest-assets --scope <scope>`:
  - `1p`     → all active client engagements (identical to bare `--all`).
  - `fpsf`   → internal initiatives/repos; asset ingest is client-only → no-op.
  - `canonic`→ internal initiatives/repos; asset ingest is client-only → no-op.

"Active client engagement" reuses the canonical definition the rest of the
engine uses: an active-status engagement (`is_active_status`) with
`is_internal=false` and `company_kind == 'client'`; "active initiative" gates on
`is_active_initiative_status`. See `active_ingestable_codes`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from cp_engine.sync import _default_backend_factory

# Scopes that have no client engagements: asset ingest skips them entirely.
INTERNAL_SCOPES = ("fpsf", "canonic")


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


def _is_active_engagement(p) -> bool:
    """The canonical "active client engagement" predicate.

    `source == "engagement"`, active status via `is_active_status` (the single
    source of truth for the active vocabulary, so Holding ever joining the
    active set flows through here for free), `is_internal=False`, and
    `company_kind == "client"`. Kept as the SAME predicate `sync.py` and
    `list-active-projects` use.
    """
    from cp_engine.status import is_active_status

    return (
        p.source == "engagement"
        and is_active_status(p.status)
        and not p.is_internal
        and p.company_kind == "client"
    )


def _is_active_initiative(p) -> bool:
    """An ACTIVE initiative — eligible for asset ingest (Task 8).

    Initiatives carry their OWN active-status vocabulary (`Active` only), so we
    gate on `is_active_initiative_status`, NOT `is_active_status` (which speaks
    the engagement vocabulary). `is_internal`/`company_kind` don't apply — an
    initiative is internal by construction.
    """
    from cp_engine.status import is_active_initiative_status

    return p.source == "initiative" and is_active_initiative_status(p.status)


def active_ingestable_codes(config) -> list[str]:
    """Enumerate cp codes for every item eligible for asset ingest in MC-2.

    The ingestable set = active client engagements PLUS active initiatives
    (Task 8 lifted the engagement-only gate). Enumerate via the sync backend's
    `read_projects` and apply the per-kind predicate. Returns each
    `ProjectState.code` (already the pre-built canonical form — `<co>-<number>`
    for engagements, the slug for initiatives — no hand-rebuilt string).

    Initiatives with no ingest folders configured simply list nothing when the
    per-project run resolves them; widening the SET here is safe and never
    crashes (see `ingest_project_assets`'s graceful no-folders short-circuit).
    """
    backend = _default_backend_factory(config.sync.backend)
    projects = backend.read_projects(config)

    return [
        p.code
        for p in projects
        if _is_active_engagement(p) or _is_active_initiative(p)
    ]


@dataclass
class _ProjectOutcome:
    """One project's slot in a `--all` run."""

    code: str
    created: int = 0
    versioned: int = 0
    skipped: int = 0
    deduped: int = 0
    skipped_unchanged: int = 0
    skipped_shortcuts: int = 0
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
    def total_deduped(self) -> int:
        return sum(o.deduped for o in self.outcomes)

    @property
    def total_skipped_unchanged(self) -> int:
        return sum(o.skipped_unchanged for o in self.outcomes)

    @property
    def total_skipped_shortcuts(self) -> int:
        return sum(o.skipped_shortcuts for o in self.outcomes)

    @property
    def total_failed(self) -> int:
        return sum(o.failed for o in self.outcomes)

    @property
    def any_failures(self) -> bool:
        """True if any per-file failure OR any whole-project error occurred."""
        return any(o.failed or o.failures or o.error for o in self.outcomes)


def fan_out_ingest(
    client, codes: list[str], *, use_cache: bool = True
) -> FanOutResult:
    """Run `ingest_project_assets` for each code, collecting per-project outcomes.

    One project raising (or returning failures) NEVER stops the others — every
    project gets a slot in the result, and whole-project exceptions are captured
    on `.error` so the caller can surface them and exit non-zero. `use_cache`
    threads the CLI `--no-cache` bypass through to every per-project run.
    """
    from cp_engine.asset_ingest import ingest_project_assets

    result = FanOutResult()
    for code in codes:
        outcome = _ProjectOutcome(code=code)
        try:
            run = ingest_project_assets(code, client=client, use_cache=use_cache)
            outcome.created = run.created
            outcome.versioned = run.versioned
            outcome.skipped = run.skipped
            outcome.deduped = run.deduped
            outcome.skipped_unchanged = run.skipped_unchanged
            outcome.skipped_shortcuts = run.skipped_shortcuts
            outcome.failed = run.failed
            outcome.failures = list(run.failures)
        except Exception as exc:  # noqa: BLE001 — collect, keep going
            outcome.error = str(exc)
        result.outcomes.append(outcome)
    return result
