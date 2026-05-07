"""Context Protocol Engine — framework package.

See `docs/specs/cp-engine-spec-v02.md` for the canonical spec.
"""

__version__ = "0.1.0"

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
from cp_engine.status import (
    ACTIVE_STATUSES,
    MC_STATUS_ACTIVE,
    MC_STATUSES,
    is_active_status,
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
]
