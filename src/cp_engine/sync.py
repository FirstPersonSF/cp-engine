"""Sync layer — reads source-of-truth state, writes engine-managed regions.

Two backends per spec v02 §4.1:
  - mc-2: reads MC-2's Postgres via Supabase API (for cp-1p, cp-firstpersonsf)
  - github-issues: reads GitHub Issues per project repo (for cp-canonic)

Tenants pick one backend in `.cp-engine.toml [sync]`. Mixed-backend
tenants are not supported in v02.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from cp_engine.config import TenantConfig


@dataclass(frozen=True)
class ProjectState:
    """Source-of-truth state for one project, normalized across backends."""

    code: str
    name: str
    status: str  # one of MC_STATUSES
    is_internal: bool
    owner: str | None
    last_touched: datetime | None
    deadline: datetime | None
    one_line_summary: str | None = None  # regenerated during deepening pass


@dataclass(frozen=True)
class Issue:
    """One tracked GitHub Issue, surfaced in a project CP's tracked-issues table."""

    number: int
    title: str
    status: str
    owner: str | None
    updated: datetime | None


@dataclass(frozen=True)
class SyncResult:
    """Outcome of one sync cycle for a tenant."""

    projects_updated: tuple[str, ...]  # codes
    no_op: bool


def sync_tenant(config: TenantConfig) -> SyncResult:
    """Run one sync cycle for the given tenant.

    1. Read source-of-truth state (per backend)
    2. Filter to active subset
    3. Reconcile into engine-managed regions of project CPs
    4. Re-render master-cp.md, CLAUDE.md
    5. Push to cp-sync branch (caller's responsibility — sync_tenant
       returns the diff; the GitHub Action / cp CLI commits)

    Implementation lands in v0.1.
    """
    raise NotImplementedError("sync.sync_tenant lands in v0.1")
