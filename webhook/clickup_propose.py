"""Stage A — propose ClickUp tasks from a meeting's Fathom action items.

Runs inside the auto-ingest webhook after the transcript->plan->commit
stages. Reads the inline ``fathom_meetings.action_items`` JSONB, resolves
each project code to its ClickUp list, and writes ``pending`` rows into
``clickup_task_proposals``. It NEVER calls ClickUp — proposals sit behind
the dashboard's yes/no review gate.

Best-effort: any failure here is logged + swallowed. Proposing ClickUp
tasks must never break the primary auto-ingest contract.

Design: cp/docs/plans/2026-05-21-clickup-tasks-design.md
"""

from __future__ import annotations

import logging
import os

from cp_engine.clickup_routing import resolve_clickup_project
from cp_engine.ingest import _content_hash
from cp_engine import mc2_db
from cp_engine.mc2_db import Tables

import observability

log = logging.getLogger("cp-engine-webhook")


def _supabase_client():
    """Return a Supabase client, or None if env/package is unavailable."""
    client = mc2_db.get_client(required=False)
    if client is None:
        log.warning(
            "clickup-propose skipped: Supabase unavailable "
            "(env not set or supabase package not installed)"
        )
    return client


def _resolve_project(client, code: str) -> dict | None:
    """Resolve a cp project code to its MC-2 ClickUp routing row.

    Thin wrapper over :func:`cp_engine.clickup_routing.resolve_clickup_project`
    — the single implementation, also wrapped by
    ``cp_engine.ingest._resolve_proposal_project`` (ends the "KEEP IN SYNC"
    duplication, arch-phase-2). Behavior notes vs the old local copy: absent
    ``enable_clickup`` still means disabled here, and initiative lookups now
    swallow only ``postgrest.APIError`` (missing ClickUp columns) — genuine
    network failures propagate instead of being silently treated as
    "no routing".
    """
    return resolve_clickup_project(client, code, missing_enable_clickup_ok=False)


def _existing_descriptions(client, meeting_id: str) -> set[str]:
    """Descriptions already proposed for this meeting (pending or approved).

    Rejected proposals are intentionally excluded — a rejected item that
    re-appears in Fathom's action items should NOT be re-proposed.
    """
    resp = (
        client.table(Tables.CLICKUP_TASK_PROPOSALS)
        .select("description")
        .eq("meeting_id", meeting_id)
        .in_("status", ["pending", "approved", "rejected"])
        .execute()
    )
    return {r["description"] for r in (resp.data or []) if r.get("description")}


def _build_proposal_row(*, meeting_id: str, project: dict, item: dict) -> dict:
    """Shape one Fathom action_item into a clickup_task_proposals row.

    The `cp_ask_hash` column carries the cp ask's 8-char content-hash
    forward to the dashboard, which stamps `[cp:hash=...]` into the
    ClickUp task description at creation time. Task 1.7's ClickUp-close
    webhook reads that hash back from a closed task and routes a
    close-ask plan to the project that owns it.

    The hash recipe is `_content_hash(project_code, "record-ask",
    description)` — EXACTLY the same as ingest._content_hash for
    record-ask items. Re-ingest of the same meeting is therefore an
    idempotent no-op: _write_ask's dedupe sees the matching hash and
    skips the row.

    Owner column: ``clickup_task_proposals`` (post-migration 081) carries
    BOTH ``project_id`` (FK projects) and ``initiative_id`` (FK initiatives)
    with a ``num_nonnulls(...) == 1`` CHECK — exactly one owner. We write
    whichever matches ``project["kind"]`` (defaulting to ``project`` for
    callers that predate the kind field). Writing ``project_id`` for an
    initiative would FK-crash on insert.
    """
    description = (item.get("description") or "").strip()
    assignee = item.get("assignee") or {}
    row = {
        "meeting_id": meeting_id,
        "clickup_list_id": project["clickup_list_id"],
        "description": description,
        "assignee_email": assignee.get("email"),
        "recording_playback_url": item.get("recording_playback_url"),
        "cp_ask_hash": _content_hash(project["code"], "record-ask", description),
        "status": "pending",
    }
    if project.get("kind") == "initiative":
        row["initiative_id"] = project["id"]
    else:
        row["project_id"] = project["id"]
    return row


def propose_clickup_tasks(meeting_id: str, project_codes: list[str]) -> dict:
    """Insert ``pending`` ClickUp task proposals for a meeting.

    For each Fathom action item on the meeting, route it to the (single)
    project whose code resolves to a ClickUp list and insert one
    ``pending`` row. Dedups against existing proposals for the meeting.

    Returns a summary dict for observability:
        {"proposed": int, "skipped_duplicate": int, "no_list": [codes]}

    Never raises.
    """
    summary = {"proposed": 0, "skipped_duplicate": 0, "no_list": []}
    try:
        client = _supabase_client()
        if client is None:
            return summary

        # Fathom's inline action_items JSONB lives on the meeting row.
        resp = (
            client.table(Tables.FATHOM_MEETINGS)
            .select("id, action_items")
            .eq("id", meeting_id)
            .single()
            .execute()
        )
        action_items = (resp.data or {}).get("action_items") or []
        if not action_items:
            log.info("clickup-propose: no action items for meeting=%s", meeting_id)
            return summary

        # Resolve routing once per project code. A single-project
        # auto-ingest has one code; multi-project (account / sprint
        # planning) action items can't be attributed per project from
        # Fathom's payload, so we route to the first resolvable project.
        resolved = None
        for code in project_codes:
            candidate = _resolve_project(client, code)
            if candidate is None:
                continue
            if not candidate.get("clickup_list_id"):
                summary["no_list"].append(code)
                continue
            resolved = candidate
            break

        if resolved is None:
            log.info(
                "clickup-propose: no routable ClickUp list for meeting=%s codes=%s",
                meeting_id, project_codes,
            )
            return summary

        already = _existing_descriptions(client, meeting_id)
        rows: list[dict] = []
        for item in action_items:
            if not isinstance(item, dict):
                continue
            description = (item.get("description") or "").strip()
            if not description or description in already:
                if description:
                    summary["skipped_duplicate"] += 1
                continue
            already.add(description)  # guard against dups within one payload
            rows.append(_build_proposal_row(
                meeting_id=meeting_id, project=resolved, item=item
            ))

        if rows:
            client.table(Tables.CLICKUP_TASK_PROPOSALS).insert(rows).execute()
            summary["proposed"] = len(rows)
            log.info(
                "clickup-propose: %d proposal(s) for meeting=%s project=%s",
                len(rows), meeting_id, resolved["code"],
            )
    except Exception as exc:  # noqa: BLE001 — must never break auto-ingest
        log.warning("clickup-propose failed for meeting=%s: %s", meeting_id, exc)
        observability.capture(exc, area="clickup_propose")

    return summary
