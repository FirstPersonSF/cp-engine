"""Resolve a cp code to its MC-2 ClickUp routing row — the ONE implementation.

Until arch-phase-2 (2026-07-03) this logic existed twice with "KEEP IN SYNC"
comments — ``cp_engine.ingest._resolve_proposal_project`` and
``webhook/clickup_propose._resolve_project`` — and had already diverged on
error handling and absent-``enable_clickup`` semantics. Both are now thin
wrappers over :func:`resolve_clickup_project`.

Reconciled divergences (documented so the history isn't mysterious):

- **Initiative lookup errors**: unified on ``postgrest.APIError`` (a missing
  ClickUp column on ``initiatives`` — the case this guard exists for). The
  webhook previously swallowed bare ``Exception``, which also hid genuine
  network failures; those now propagate (silent-failure fix).
- **Absent ``enable_clickup`` key**: only possible with test mocks that omit
  the column (PostgREST always returns selected columns). Callers choose via
  ``missing_enable_clickup_ok``: the ingest path passes True (its historical
  mock-tolerant behavior); the webhook keeps False (absent = disabled).
"""
from __future__ import annotations

from cp_engine.mc2_db import Tables
import logging
from typing import Any

from postgrest.exceptions import APIError

log = logging.getLogger(__name__)


def resolve_clickup_project(
    client: Any,
    code: str,
    *,
    missing_enable_clickup_ok: bool = False,
) -> dict | None:
    """Resolve a cp code to an MC-2 ClickUp routing dict, or None.

    Engagement codes are ``<company>-<number>`` (e.g. ``ggl-5136``); the
    number is always the trailing segment. Initiative codes are a bare slug
    stored directly on ``initiatives.code`` (e.g. ``mission-control``).

    Returns ``{"id", "clickup_list_id", "code", "kind"}`` with ``kind`` in
    ``{"project", "initiative"}`` — the ``kind`` stamp drives owner-column
    selection on ``clickup_task_proposals`` (``project_id`` vs
    ``initiative_id`` under a num_nonnulls==1 CHECK), so both branches MUST
    keep stamping it.

    None means: no row, or ClickUp routing disabled/unavailable for this
    code. Skip reasons are logged at INFO.
    """
    tail = code.rsplit("-", 1)[-1]
    number = int(tail) if tail.isdigit() else None

    if number is not None:
        resp = (
            client.table(Tables.PROJECTS)
            .select("id, number, clickup_list_id, enable_clickup")
            .eq("number", number)
            .execute()
        )
        rows = resp.data or []
        if not rows:
            log.info("clickup-routing: no project row for code=%s", code)
            return None
        row = rows[0]
        if not _clickup_enabled(row, missing_enable_clickup_ok):
            log.info("clickup-routing: ClickUp disabled for code=%s", code)
            return None
        return {
            "id": row["id"],
            "clickup_list_id": row.get("clickup_list_id"),
            "code": code,
            "kind": "project",
        }

    # Initiative — slug code. ClickUp columns rolled out incrementally on
    # initiatives; a PostgREST APIError here means the column is missing,
    # not that the network is down. Anything else propagates.
    try:
        resp = (
            client.table(Tables.INITIATIVES)
            .select("id, code, clickup_list_id, enable_clickup")
            .eq("code", code)
            .execute()
        )
    except APIError as exc:
        log.info("clickup-routing: initiative ClickUp lookup unavailable (%s)", exc)
        return None
    rows = resp.data or []
    if not rows:
        log.info("clickup-routing: no initiative row for code=%s", code)
        return None
    row = rows[0]
    if not _clickup_enabled(row, missing_enable_clickup_ok):
        log.info("clickup-routing: ClickUp disabled for initiative code=%s", code)
        return None
    return {
        "id": row["id"],
        "clickup_list_id": row.get("clickup_list_id"),
        "code": code,
        "kind": "initiative",
    }


def _clickup_enabled(row: dict, missing_ok: bool) -> bool:
    if "enable_clickup" not in row:
        return missing_ok
    return bool(row.get("enable_clickup"))
