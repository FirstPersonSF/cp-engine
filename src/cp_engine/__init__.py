"""Context Protocol Engine — framework package.

See `docs/specs/cp-engine-spec-v02.md` for the canonical spec.
"""

__version__ = "0.1.0"

from cp_engine.status import (
    MC_STATUS_ACTIVE,
    MC_STATUSES,
    ACTIVE_STATUSES,
    is_active_status,
)

__all__ = [
    "__version__",
    "MC_STATUSES",
    "MC_STATUS_ACTIVE",
    "ACTIVE_STATUSES",
    "is_active_status",
]
