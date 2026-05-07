"""Tenant configuration — merges committed `.cp-engine.toml` with the
gitignored `.cp-engine.local.toml`.

See spec v02 §5 for the schema. Implementation lands in v0.1; this module
is the contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectConfig:
    """One project tracked by a tenant."""

    code: str
    github: str  # "owner/repo"
    local_path: Path | None  # from .cp-engine.local.toml; None means user skipped


@dataclass(frozen=True)
class SyncConfig:
    """How the tenant syncs source-of-truth state into engine-managed regions."""

    backend: str  # "mc-2" or "github-issues"
    cron: str
    mc_2_supabase_project_ref: str | None = None


@dataclass(frozen=True)
class TenantConfig:
    """Merged view of a tenant's committed + local configuration."""

    name: str
    display: str
    engine_version: str
    sync: SyncConfig
    projects: tuple[ProjectConfig, ...]


def load(tenant_root: Path) -> TenantConfig:
    """Load and merge `.cp-engine.toml` (committed) with `.cp-engine.local.toml`.

    Fails loudly if a project is in the committed file but missing from
    local (per spec §5.4).
    """
    raise NotImplementedError("config.load lands in v0.1")
