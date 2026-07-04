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
from cp_engine.mc2_bindings import _clickup_list_id, fetch_binding_rows
import logging
from typing import Any

from postgrest.exceptions import APIError

log = logging.getLogger(__name__)


def _list_id_from_bindings(
    client: Any, *, project_id: str | None = None, initiative_id: str | None = None
) -> str | None:
    """The owner's ClickUp list id from its ``''`` binding (read-flip: the
    flat ``clickup_list_id`` columns are being retired)."""
    owner_id = project_id or initiative_id
    grouped = fetch_binding_rows(
        client,
        project_ids=[project_id] if project_id else (),
        initiative_ids=[initiative_id] if initiative_id else (),
    )
    return _clickup_list_id(grouped.get(owner_id))


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
    number = engagement_number(code)

    if number is not None:
        resp = (
            client.table(Tables.PROJECTS)
            .select("id, number, enable_clickup")
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
            "clickup_list_id": _list_id_from_bindings(client, project_id=row["id"]),
            "code": code,
            "kind": "project",
        }

    # Initiative — slug code. ClickUp columns rolled out incrementally on
    # initiatives; a PostgREST APIError here means the column is missing,
    # not that the network is down. Anything else propagates.
    try:
        resp = (
            client.table(Tables.INITIATIVES)
            .select("id, code, enable_clickup")
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
        "clickup_list_id": _list_id_from_bindings(client, initiative_id=row["id"]),
        "code": code,
        "kind": "initiative",
    }


def engagement_number(code: str) -> int | None:
    """Extract the MC-2 project number from a cp engagement code.

    Two canonical shapes:
      - short form ``<co>-<number>`` ("ggl-5168") — the tail is the number;
      - full slug ``<co>-<number>-<name-slug>`` ("ggl-5136-go-safety-website",
        the slugified full_job_name that became the canonical id in v0.35) —
        the number is the SECOND dash-segment.

    The second-segment rule deliberately ignores digits deeper in the slug
    (a year like "…-update-2026" is not the project number). Initiative
    slugs ("mission-control") have no numeric segment → None.
    """
    segments = code.split("-")
    if len(segments) >= 2 and segments[1].isdigit():
        return int(segments[1])
    tail = segments[-1]
    return int(tail) if tail.isdigit() else None


def _clickup_enabled(row: dict, missing_ok: bool) -> bool:
    if "enable_clickup" not in row:
        return missing_ok
    return bool(row.get("enable_clickup"))
