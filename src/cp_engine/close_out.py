"""Close-out reads shared by `cxp wrap`: working-dir resolution (live OR
parked), the live spine rows and the open commitments of a finished job.

The `cxp close` checklist verb these were written for was retired in
architecture step 5a (no use on record); `cxp wrap` still reads through them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any



@dataclass(frozen=True)
class CloseElement:
    """The slice of a spine element the close-out checklist reasons about."""

    key: str  # est_item_id (the addressable key MCP verbs resolve)
    title: str
    layer: str
    body_len: int
    serves: tuple[str, ...] = ()
    scope: str | None = None  # "account" when already promoted


def element_from_row(row: dict[str, Any]) -> CloseElement:
    """Build a CloseElement from a live `spine_substance` MC-2 row."""
    return CloseElement(
        key=str(row.get("est_item_id") or row.get("id") or ""),
        title=str(row.get("framing") or row.get("est_item_id") or row.get("id") or ""),
        layer=str(row.get("layer") or "Deliverables"),
        body_len=len(str(row.get("body") or "").strip()),
        serves=tuple(str(x) for x in (row.get("serves") or [])),
        scope=row.get("scope"),
    )


# ──────────────────────────────────────────────────────────────────────
#  Working-dir resolution (live OR parked)
# ──────────────────────────────────────────────────────────────────────


def find_close_workdir(tenant_root: Path, code: str) -> tuple[Path, bool]:
    """Resolve a project's working dir, INCLUDING inactive parking lots.

    `find_spine_dir` deliberately skips `inactive/` bins — but close-out
    targets are usually already parked. Returns ``(path, parked)`` where
    ``parked`` is True when the dir was found under an ``inactive/`` bin
    (per-account `1p/<company>/inactive/<dir>/`, or scope-level
    `<scope>/inactive/<account-or-dir>/`).

    Raises SpineDirNotFound when nothing matches anywhere.
    """
    from cp_engine.spine import SpineDirNotFound, find_spine_dir
    from cp_engine.sync import _SCOPE_DIRS, _find_project_dir, _inactive_bins

    try:
        return find_spine_dir(tenant_root, code), False
    except SpineDirNotFound:
        pass

    # Every `inactive/` bin at any depth (#302): the scope-level bin
    # (`1p/inactive/<account>/<dir>/` — whole parked accounts), the
    # per-account bins (`1p/<company>/inactive/<dir>/`) and any bin under a
    # program. Each is walked recursively.
    for bin_dir in _inactive_bins(tenant_root):
        hit = _find_project_dir(bin_dir, code)
        if hit is not None:
            return hit, True

    raise SpineDirNotFound(
        f"No working dir for '{code}' under {', '.join(_SCOPE_DIRS)}/ "
        f"(searched live dirs and inactive/ bins)"
    )


# ──────────────────────────────────────────────────────────────────────
#  Live reads (status, spine, commitments)
# ──────────────────────────────────────────────────────────────────────


def fetch_live_spine_rows(client: Any, code: str) -> list[dict]:
    """One live `spine_substance` row per element, #113-deduped.

    `code` must be the DIR-SLUG (`spine_substance.project_code` — e.g.
    `ibx-5153-ai-campaign`, or the bare initiative slug), which is exactly
    the resolved working dir's name — pass `workdir.name`, not the short
    engagement code.
    """
    from cp_engine.mc2_db import Tables
    from cp_engine.project_sources import _one_live_per_element

    rows = (
        client.table(Tables.SPINE_SUBSTANCE)
        .select(
            "id, est_item_id, framing, layer, status, version_label, "
            "version_date, body, serves, scope, archived"
        )
        .eq("project_code", code)
        .eq("status", "live")
        .execute()
        .data
    ) or []
    return _one_live_per_element([r for r in rows if not r.get("archived")])


def fetch_open_commitments(client: Any, code: str) -> list[dict] | None:
    """The project's open commitments, or None when the code doesn't resolve
    to a commitments owner (standalone repos can't own commitments)."""
    from cp_engine.commitments import list_commitments, resolve_commitment_owner

    owner = resolve_commitment_owner(client, code)
    if owner is None:
        return None
    return list_commitments(client, owner, status="open")
