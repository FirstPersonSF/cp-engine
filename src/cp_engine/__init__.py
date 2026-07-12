"""Context Protocol Engine — framework package.

See `docs/specs/cp-engine-spec-v02.md` for the canonical spec.
"""

# NOTE: keep this string in sync with pyproject.toml's [project].version.
# scripts/release.py bumps both atomically — never edit only one.
__version__ = "0.59.0"

from cp_engine.config import (
    CommittedConfigInvalid,
    CommittedConfigMissing,
    ConfigError,
    EngineVersionMismatch,
    LocalConfigInvalid,
    LocalConfigMissing,
    LocalPathNotFound,
    ProjectConfig,
    ProjectsMissingFromLocal,
    SyncConfig,
    TenantConfig,
    load,
)
from cp_engine.init import InitAborted, run_init
from cp_engine.render import (
    MarkerDuplicated,
    MarkerInverted,
    MarkerMissing,
    RenderError,
    count_exceptions_in_window,
    render_claude_md,
    render_exceptions_readme,
    render_linked_repo_md,
    render_master_cp,
    render_project_cp,
    render_repo_md,
    render_weekly_cp,
    splice_managed_region,
)
from cp_engine.status import (
    ACTIVE_STATUSES,
    MC_STATUS_ACTIVE,
    MC_STATUSES,
    is_active_status,
)
from cp_engine.sync import (
    BackendUnavailable,
    Issue,
    LinkedRepo,
    ProjectState,
    SyncError,
    SyncResult,
    UnknownBackend,
    sync_tenant,
)

__all__ = [
    "__version__",
    # status
    "MC_STATUSES",
    "MC_STATUS_ACTIVE",
    "ACTIVE_STATUSES",
    "is_active_status",
    # config
    "load",
    "TenantConfig",
    "ProjectConfig",
    "SyncConfig",
    "ConfigError",
    "CommittedConfigMissing",
    "LocalConfigMissing",
    "CommittedConfigInvalid",
    "LocalConfigInvalid",
    "ProjectsMissingFromLocal",
    "LocalPathNotFound",
    "EngineVersionMismatch",
    # sync
    "ProjectState",
    "LinkedRepo",
    "Issue",
    "sync_tenant",
    "SyncResult",
    "SyncError",
    "UnknownBackend",
    "BackendUnavailable",
    # render
    "render_master_cp",
    "render_weekly_cp",
    "render_project_cp",
    "render_repo_md",
    "render_linked_repo_md",
    "render_claude_md",
    "render_exceptions_readme",
    "count_exceptions_in_window",
    "splice_managed_region",
    "RenderError",
    "MarkerMissing",
    "MarkerDuplicated",
    "MarkerInverted",
    # init
    "run_init",
    "InitAborted",
]
