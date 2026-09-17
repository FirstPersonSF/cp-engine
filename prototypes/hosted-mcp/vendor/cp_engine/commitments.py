"""VENDORED from `cp_engine.commitments` — the owner resolver only.

`commitments_sweep` imports `resolve_commitment_owner` at call time. The real
module imports `clickup_routing` (postgrest) at module level; only
`engagement_number` is needed from it, so it is inlined here.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from cp_engine.mc2_db import Tables

logger = logging.getLogger(__name__)


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


def resolve_commitment_owner(client: Any, code: str) -> dict | None:
    """Resolve a cp code to a commitments owner: ``{"id", "code", "kind"}``.

    Same code grammar as :func:`clickup_routing.resolve_clickup_project` —
    engagement codes carry a trailing number, initiative codes are a bare
    slug on ``initiatives.code`` — but with NO ClickUp gates: every code
    that exists in MC-2 resolves, whether or not ClickUp is enabled.
    """
    number = engagement_number(code)
    if number is not None:
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

    resp = (
        client.table(Tables.INITIATIVES)
        .select("id, code")
        .eq("code", code)
        .execute()
    )
    rows = resp.data or []
    if not rows:
        log.info("commitments: no initiative row for code=%s", code)
        return None
    return {"id": rows[0]["id"], "code": code, "kind": "initiative"}
