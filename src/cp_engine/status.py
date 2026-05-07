"""Project status canonical vocabulary.

Mirrors `mc-2/frontend/src/lib/status.ts` and `mc-2/backend/src/status.py`.
This third copy exists so the framework can render status-aware output
(active-subset filtering, color maps, master-CP rendering) without
depending on MC-2's repo at runtime — relevant for `cp-canonic` which
uses the github-issues sync backend, not MC-2.

Drift between the three copies is a CI failure. See spec v02 §3.2.
"""

MC_STATUSES = ("Deal", "Open", "Holding", "Closed", "Archived")

# Per-status active flag. True means a project in this status is "active"
# in the partners'-review sense. Per-status (not a flat set) so adding a
# future status doesn't require touching every consumer.
MC_STATUS_ACTIVE: dict[str, bool] = {
    "Deal": True,
    "Open": True,
    "Holding": False,
    "Closed": False,
    "Archived": False,
}

# Active subset = Deal ∪ Open. Combine with is_internal=False (or whatever
# the sync backend's equivalent boolean is) for the full Context Protocol
# active-projects filter.
ACTIVE_STATUSES: tuple[str, ...] = tuple(s for s, active in MC_STATUS_ACTIVE.items() if active)


def is_active_status(status: str | None) -> bool:
    """Return True if `status` is in the active subset (Deal or Open)."""
    return status is not None and MC_STATUS_ACTIVE.get(status, False)
