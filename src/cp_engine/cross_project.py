"""Cross-project routing proposals — MC-2 ``public.cross_project_proposals``.

A meeting tagged to ONE project often carries substantive items that
belong to OTHER active projects (cp-engine #88). The single-project
ingest classifier annotates such items (see
``plan_from_transcript._apply_cross_project_annotations``); each valid
annotation becomes a PENDING row here (mc-2 mig 112). The posture is
propose-and-confirm — same review-gate philosophy as commitments:

- the item still writes to the TAGGED project's sprint file, suffixed
  ``[cross-project? → <code>]``;
- a human accepts or dismisses the proposal (attention digest buttons);
- on accept, cp-engine writes the item to the TARGET project's current
  sprint file with provenance ``[cross-routed from <code> · meeting
  <id>]`` — the ONLY path that ever writes to the target.

Idempotency mirrors commitments: ``cp_hash`` (target-scoped content
hash) pre-check + unique index; dismissed rows count as duplicates on
purpose, so a re-ingested meeting can't resurrect a dismissed proposal.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from cp_engine.commitments import resolve_commitment_owner
from cp_engine.ingest import _content_hash

log = logging.getLogger(__name__)

TABLE = "cross_project_proposals"

# Explicit columns — never SELECT *.
LIST_COLUMNS = (
    "id, meeting_id, source_code, target_code, verb, text, payload, "
    "confidence, status, cp_hash, created_at"
)

VERBS = frozenset({"inbound", "asks", "decisions", "risks"})


def proposal_hash(target_code: str, verb: str, text: str) -> str:
    """Target-scoped content hash: the same proposal re-detected from a
    re-ingest (or from a second meeting covering the same ground) dedupes
    against the routing decision already on file."""
    return _content_hash(target_code, f"xproject-{verb}", text)


def proposal_already_present(client: Any, cp_hash: str) -> bool:
    """True if a proposal with this hash exists in ANY status (dismissed
    rows stay dead). Best-effort: lookup failure lets the insert proceed;
    the unique index is the backstop."""
    try:
        resp = (
            client.table(TABLE)
            .select("id")
            .eq("cp_hash", cp_hash)
            .limit(1)
            .execute()
        )
        return bool(resp.data)
    except Exception as exc:  # noqa: BLE001 — never block the insert
        log.warning(
            "cross-project dedupe lookup failed for hash=%s: %s; allowing insert",
            cp_hash, exc,
        )
        return False


def write_proposal(
    client: Any,
    *,
    meeting_id: str | None,
    source_code: str,
    target_code: str,
    verb: str,
    text: str,
    confidence: str | None,
    item: dict | None = None,
) -> str:
    """Insert one pending proposal; returns ``"inserted"``, ``"duplicate"``
    or ``"unresolvable"`` (target code not in MC-2 — the guard against
    garbled/invented codes; nothing is written)."""
    if verb not in VERBS:
        raise ValueError(f"unknown cross-project verb: {verb!r}")

    owner = resolve_commitment_owner(client, target_code)
    if owner is None:
        log.info(
            "cross-project: target %s unresolvable; dropping proposal", target_code
        )
        return "unresolvable"

    cp_hash = proposal_hash(target_code, verb, text)
    if proposal_already_present(client, cp_hash):
        log.info(
            "cross-project: hash=%s already present; skipping (%s → %s)",
            cp_hash, source_code, target_code,
        )
        return "duplicate"

    row = {
        "meeting_id": meeting_id,
        "source_code": source_code,
        "target_code": target_code,
        "verb": verb,
        "text": text,
        "payload": item or {},
        "confidence": confidence,
        "status": "pending",
        "cp_hash": cp_hash,
    }
    if owner.get("kind") == "initiative":
        row["target_initiative_id"] = owner["id"]
    else:
        row["target_project_id"] = owner["id"]

    client.table(TABLE).insert(row).execute()
    return "inserted"


def list_pending(client: Any, limit: int = 25) -> list[dict]:
    """Pending proposals, oldest first (review queue order)."""
    resp = (
        client.table(TABLE)
        .select(LIST_COLUMNS)
        .eq("status", "pending")
        .order("created_at", desc=False)
        .limit(limit)
        .execute()
    )
    return resp.data or []


def get_by_hash(client: Any, cp_hash: str) -> dict | None:
    """One proposal by its cp_hash (the key carried in digest buttons)."""
    resp = (
        client.table(TABLE)
        .select(LIST_COLUMNS)
        .eq("cp_hash", cp_hash)
        .limit(1)
        .execute()
    )
    rows = resp.data or []
    return rows[0] if rows else None


def decide(
    client: Any,
    proposal_id: str,
    status: str,
    routed_commit_sha: str | None = None,
) -> None:
    """Stamp a proposal accepted/dismissed (+ the cp-tenant commit that
    wrote the routed item, on accept)."""
    if status not in ("accepted", "dismissed"):
        raise ValueError(f"invalid decision status: {status!r}")
    patch: dict = {
        "status": status,
        "decided_at": datetime.now(timezone.utc).isoformat(),
    }
    if routed_commit_sha:
        patch["routed_commit_sha"] = routed_commit_sha
    client.table(TABLE).update(patch).eq("id", proposal_id).execute()


def routed_item_text(proposal: dict) -> str:
    """The text written to the TARGET project's sprint file on accept:
    the clean item text plus the cross-routed provenance marker."""
    meeting_id = (proposal.get("meeting_id") or "")[:8]
    provenance = f"[cross-routed from {proposal['source_code']}"
    if meeting_id:
        provenance += f" · meeting {meeting_id}"
    provenance += "]"
    return f"{proposal['text']} {provenance}"


def build_routed_plan(proposal: dict) -> dict:
    """A one-item single-project ingest plan that writes the accepted
    proposal into the target project's current sprint file (executed by
    ``ingest.execute_plan``; hash-dedup applies as usual)."""
    item = dict(proposal.get("payload") or {})
    item["text"] = routed_item_text(proposal)
    return {"projects": {proposal["target_code"]: {proposal["verb"]: [item]}}}
