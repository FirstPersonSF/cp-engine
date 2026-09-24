"""Resolve a cp code to its MC-2 ClickUp routing row — the ONE implementation.

Until arch-phase-2 (2026-07-03) this logic existed twice with "KEEP IN SYNC"
comments — ``cp_engine.ingest._resolve_proposal_project`` and
``webhook/clickup_propose._resolve_project`` — and had already diverged on
error handling and absent-``enable_clickup`` semantics. Both are now thin
wrappers over :func:`resolve_clickup_project`.

Reconciled divergences (documented so the history isn't mysterious):

- **Absent ``enable_clickup`` key**: only possible with test mocks that omit
  the column (PostgREST always returns selected columns). Callers choose via
  ``missing_enable_clickup_ok``: the ingest path passes True (its historical
  mock-tolerant behavior); the webhook keeps False (absent = disabled).

Since #301 every workstream is a `projects` row with a job number, so the
code grammar is `cp_engine.codes.parse_code` and there is one lookup.
"""
from __future__ import annotations

from cp_engine.codes import code_number
from cp_engine.mc2_db import Tables
from cp_engine.mc2_bindings import _clickup_list_id, fetch_binding_rows
import logging
from typing import Any

log = logging.getLogger(__name__)


def _list_id_from_bindings(client: Any, *, project_id: str) -> str | None:
    """The owner's ClickUp list id from its ``''`` binding (read-flip: the
    flat ``clickup_list_id`` columns are being retired)."""
    grouped = fetch_binding_rows(client, project_ids=[project_id])
    return _clickup_list_id(grouped.get(project_id))


def resolve_clickup_project(
    client: Any,
    code: str,
    *,
    missing_enable_clickup_ok: bool = False,
) -> dict | None:
    """Resolve a cp code to an MC-2 ClickUp routing dict, or None.

    Any spelling `cp_engine.codes.parse_code` accepts (``ggl-5136``,
    ``ggl-5136-go-safety-website``, ``1pi-9005-mission-control``); the job
    number is the lookup key.

    Returns ``{"id", "clickup_list_id", "code", "kind"}``; ``kind`` is
    always ``"project"`` (kept so every caller keeps one dict shape). None
    means: not a code, no row, or ClickUp routing disabled for this code.
    Skip reasons are logged at INFO.
    """
    number = engagement_number(code)
    if number is None:
        log.info("clickup-routing: %r is not a workstream code", code)
        return None

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


def engagement_number(code: str) -> int | None:
    """The MC-2 job number in a cp code, or None when it is not a code.

    A thin wrapper over `cp_engine.codes.parse_code` kept for its callers
    (`commitments`, `prep_planning`, `close_out`, the hosted shim). One
    grammar for every spelling — short form, canonical slug, display name
    — and digits deeper in a slug (``…-update-2026``) are never the number.
    """
    return code_number(code)


def _clickup_enabled(row: dict, missing_ok: bool) -> bool:
    if "enable_clickup" not in row:
        return missing_ok
    return bool(row.get("enable_clickup"))
