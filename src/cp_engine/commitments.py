"""Write dated commitments into MC-2's ``public.commitments`` table.

A commitment is who-owes-what-to-whom-by-when on a project or initiative
(mc-2 mig 097). This module is the cp-engine write path — used by the
auto-ingest webhook (Fathom action items) and the ``set-milestone`` /
``set-client-ask-task`` ingest verbs, which historically wrote
``clickup_task_proposals`` rows (design:
cp/docs/plans/2026-07-07-commitments-consolidation-design.md).

The review-gate concept survives as state: every row lands with
``date_status='proposed'``; the weekly Slack dates loop ratifies dates
(proposed → agreed after two unchanged posts) and stamps ``slipped`` on
past-due open rows.

Idempotency: callers pass ``cp_hash`` (the same 8-char content-hash recipe
as sprint-file asks, ``ingest._content_hash``); a pre-check plus the
partial unique index on ``commitments.cp_hash`` make re-ingest a no-op.
Dropped rows count as duplicates on purpose — a dropped commitment that
re-appears in a re-ingested meeting must not resurrect.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from cp_engine.clickup_routing import engagement_number
from cp_engine.mc2_db import Tables

log = logging.getLogger(__name__)

# Directions (mirrors the mc-2 CHECK constraint).
US_TO_THEM = "us_to_them"
THEM_TO_US = "them_to_us"
INTERNAL = "internal"


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


def commitment_already_present(client: Any, cp_hash: str) -> bool:
    """True if a commitment with this hash exists in ANY status.

    Unlike the old proposal dedup (which let rejected rows re-enter),
    dropped commitments stay dead: the row is the archive, and a re-ingest
    of the same meeting must not resurrect what a human dropped.

    Best-effort: a failed lookup returns False so the insert proceeds —
    never silently drop a real commitment; the partial unique index on
    ``cp_hash`` is the backstop against actual duplicates.
    """
    try:
        resp = (
            client.table(Tables.COMMITMENTS)
            .select("id")
            .eq("cp_hash", cp_hash)
            .limit(1)
            .execute()
        )
        return bool(resp.data)
    except Exception as exc:  # noqa: BLE001 — never block the insert
        log.warning(
            "commitments dedupe lookup failed for hash=%s: %s; allowing insert",
            cp_hash, exc,
        )
        return False


def _valid_due_date(raw: str | None) -> str | None:
    """Return the ISO date string if parseable, else None.

    Plan dates are LLM-authored free text; only a real ISO date belongs in
    the ``due_date`` column (callers fold unparseable dates into the
    description instead, so the information isn't lost)."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip()).isoformat()
    except ValueError:
        return None


def write_commitment(
    client: Any,
    *,
    owner: dict,
    description: str,
    cp_hash: str,
    source_kind: str,
    direction: str = INTERNAL,
    owner_email: str | None = None,
    owner_name: str | None = None,
    due_date: str | None = None,
    work_item_id: str | None = None,
    work_item_kind: str | None = None,
    spine_element_id: str | None = None,
    source_meeting_id: str | None = None,
) -> str:
    """Insert one commitment row; returns ``"inserted"`` or ``"duplicate"``.

    ``owner`` is a :func:`resolve_commitment_owner` dict — its ``kind``
    picks the owner column (``project_id`` vs ``initiative_id`` under the
    num_nonnulls==1 CHECK). ``due_date`` must already be ISO (or None);
    use :func:`_valid_due_date` at the call site for free-text dates.
    """
    if commitment_already_present(client, cp_hash):
        log.info(
            "commitments: hash=%s already present; skipping (%s)",
            cp_hash, owner["code"],
        )
        return "duplicate"

    row = {
        "description": description,
        "owner_email": owner_email,
        "owner_name": owner_name,
        "direction": direction,
        "due_date": due_date,
        "date_status": "proposed",
        "work_item_id": work_item_id,
        "work_item_kind": work_item_kind,
        "spine_element_id": spine_element_id,
        "status": "open",
        "source_kind": source_kind,
        "source_meeting_id": source_meeting_id,
        "cp_hash": cp_hash,
    }
    if owner.get("kind") == "initiative":
        row["initiative_id"] = owner["id"]
    else:
        row["project_id"] = owner["id"]

    client.table(Tables.COMMITMENTS).insert(row).execute()
    return "inserted"
