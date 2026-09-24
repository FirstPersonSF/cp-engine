"""VENDORED from `cp_engine.commitments` — the owner resolver only.

`commitments_sweep` imports `resolve_commitment_owner` at call time. The real
module imports `clickup_routing` (postgrest) at module level; only
`engagement_number` is needed from it, so it is inlined here over the
vendored `cp_engine.codes` (a verbatim copy).
"""

from __future__ import annotations

import logging
from typing import Any

from cp_engine.codes import code_number
from cp_engine.mc2_db import Tables

log = logging.getLogger(__name__)


def engagement_number(code: str) -> int | None:
    """The MC-2 job number in a cp code, or None when it is not a code.

    A thin wrapper over `cp_engine.codes.parse_code` kept for its callers
    (`commitments`, `prep_planning`, `close_out`, the hosted shim). One
    grammar for every spelling — short form, canonical slug, display name
    — and digits deeper in a slug (``…-update-2026``) are never the number.
    """
    return code_number(code)


def resolve_commitment_owner(client: Any, code: str) -> dict | None:
    """Resolve a cp code to a commitments owner: ``{"id", "code", "kind"}``.

    Same code grammar as :func:`clickup_routing.resolve_clickup_project`
    (`cp_engine.codes.parse_code`: every workstream carries a job number,
    #301) but with NO ClickUp gates: every code that exists in MC-2
    resolves, whether or not ClickUp is enabled. ``kind`` is always
    ``"project"`` — kept in the dict so callers keep one shape.
    """
    number = engagement_number(code)
    if number is None:
        log.info("commitments: %r is not a workstream code", code)
        return None
    resp = (
        client.table(Tables.PROJECTS)
        .select("id, number")
        .eq("number", number)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        log.info("commitments: no project row for code=%s", code)
        return None
    return {"id": rows[0]["id"], "code": code, "kind": "project"}
