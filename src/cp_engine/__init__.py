"""Context Protocol Engine — framework package.

See `docs/specs/cp-engine-spec-v02.md` for the canonical spec.
"""

__version__ = "0.2.3"

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
    render_claude_md,
    render_master_cp,
    render_project_cp,
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
    "render_claude_md",
    "splice_managed_region",
    "RenderError",
    "MarkerMissing",
    "MarkerDuplicated",
    "MarkerInverted",
    # init
    "run_init",
    "InitAborted",
]
