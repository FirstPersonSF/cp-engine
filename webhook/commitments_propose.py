"""Stage A — propose commitments from a meeting's Fathom action items.

Successor to ``clickup_propose.py`` (commitments consolidation, cp-engine
#38): action items now land as ``proposed`` rows in MC-2's
``public.commitments`` instead of ``clickup_task_proposals``. The review
gate survives as the ``date_status='proposed'`` state, ratified (or not)
by the weekly Slack dates loop and the MC-2 commitments UI.

Best-effort: any failure here is logged + swallowed. Proposing commitments
must never break the primary auto-ingest contract.

Design: cp/docs/plans/2026-07-07-commitments-consolidation-design.md
"""

from __future__ import annotations

import logging

from cp_engine import mc2_db
from cp_engine.commitments import resolve_commitment_owner, write_commitment
from cp_engine.ingest import _content_hash
from cp_engine.mc2_db import Tables

import observability

log = logging.getLogger("cp-engine-webhook")


def propose_commitments(meeting_id: str, project_codes: list[str]) -> dict:
    """Insert ``proposed`` commitments for a meeting's Fathom action items.

    Routes all items to the FIRST resolvable project code (multi-project
    ingests can't attribute action items per project from Fathom's payload
    — same semantics the ClickUp propose path had). Fathom action items
    carry no due date, so rows land undated — the weekly dates loop flags
    them as "needs a date", which is the design intent, not a gap.

    The ``cp_hash`` recipe is ``_content_hash(code, "record-ask",
    description)`` — EXACTLY the sprint-file ask recipe, so a commitment
    and its sprint-file bullet share an identity and re-ingest of the same
    meeting is an idempotent no-op.

    Returns a summary dict for observability:
        {"proposed": int, "skipped_duplicate": int, "unresolved": [codes]}

    Never raises.
    """
    summary: dict = {"proposed": 0, "skipped_duplicate": 0, "unresolved": []}
    try:
        client = mc2_db.get_client(required=False)
        if client is None:
            log.warning(
                "commitments-propose skipped: Supabase unavailable "
                "(env not set or supabase package not installed)"
            )
            return summary

        resp = (
            client.table(Tables.FATHOM_MEETINGS)
            .select("id, action_items")
            .eq("id", meeting_id)
            .single()
            .execute()
        )
        action_items = (resp.data or {}).get("action_items") or []
        if not action_items:
            log.info("commitments-propose: no action items for meeting=%s", meeting_id)
            return summary

        owner = None
        for code in project_codes:
            owner = resolve_commitment_owner(client, code)
            if owner is not None:
                break
            summary["unresolved"].append(code)

        if owner is None:
            log.info(
                "commitments-propose: no resolvable owner for meeting=%s codes=%s",
                meeting_id, project_codes,
            )
            return summary

        for item in action_items:
            if not isinstance(item, dict):
                continue
            description = (item.get("description") or "").strip()
            if not description:
                continue
            assignee = item.get("assignee") or {}
            outcome = write_commitment(
                client,
                owner=owner,
                description=description,
                cp_hash=_content_hash(owner["code"], "record-ask", description),
                source_kind="meeting_ingest",
                owner_email=assignee.get("email"),
                owner_name=assignee.get("name"),
                source_meeting_id=meeting_id,
            )
            summary["proposed" if outcome == "inserted" else "skipped_duplicate"] += 1

        if summary["proposed"]:
            log.info(
                "commitments-propose: %d commitment(s) for meeting=%s project=%s",
                summary["proposed"], meeting_id, owner["code"],
            )
    except Exception as exc:  # noqa: BLE001 — must never break auto-ingest
        log.warning("commitments-propose failed for meeting=%s: %s", meeting_id, exc)
        observability.capture(exc, area="commitments_propose")

    return summary
