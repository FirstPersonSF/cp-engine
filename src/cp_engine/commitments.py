"""Write dated commitments into MC-2's ``public.commitments`` table.

A commitment is who-owes-what-to-whom-by-when on a project or initiative
(mc-2 mig 097). This module is the cp-engine store access path — used by
the auto-ingest webhook (Fathom action items), the ``set-milestone`` /
``set-client-ask-task`` ingest verbs (which historically wrote
``clickup_task_proposals`` rows; design:
cp/docs/plans/2026-07-07-commitments-consolidation-design.md), and the
session-facing cp-sources MCP verbs (create/list/resolve, cp-engine #76).

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
import re
import time
from datetime import date, datetime, timezone
from typing import Any

from cp_engine.clickup_routing import engagement_number
from cp_engine.mc2_db import Tables

log = logging.getLogger(__name__)

# Fathom diarization labels — never a person's name (mirrors the webhook's
# resolve_action_owner guard, which predates this module-level resolution).
_DIARIZATION_RE = re.compile(r"^speaker\s*\d+$", re.IGNORECASE)
_EMAILISH_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PAREN_RE = re.compile(r"\s*\([^)]*\)")

# entities kinds that are PEOPLE. The registry also carries expense buckets
# and vendor orgs ("American Express Company") — matching a person name
# against those would be nonsense, so they're excluded at fetch time.
_PERSON_KINDS = ("staff", "freelancer")

# Per-process cache of the person roster: (fetched_at, rows). The roster
# changes rarely; a 5-minute TTL keeps webhook fan-outs from re-reading it
# per commitment while staying fresh enough for same-day entity edits.
_PEOPLE_CACHE: list | None = None
_PEOPLE_CACHE_AT: float = 0.0
_PEOPLE_CACHE_TTL = 300.0

# Directions (mirrors the mc-2 CHECK constraint).
US_TO_THEM = "us_to_them"
THEM_TO_US = "them_to_us"
INTERNAL = "internal"
DIRECTIONS = frozenset({US_TO_THEM, THEM_TO_US, INTERNAL})

# Session-facing read columns — never SELECT * (mirrors the mc-2 router's
# explicit-column discipline; excludes the dates-loop internals the model
# doesn't act on, keeps date_status so ratification state is visible).
LIST_COLUMNS = (
    "id, description, owner_email, owner_name, direction, due_date, "
    "date_status, status, source_kind, source_meeting_id, created_at, updated_at"
)


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


def _entity_people(client: Any) -> list[dict]:
    """The person roster from ``entities`` (staff + freelancers), cached.

    Archived rows are included but sorted last so an active duplicate of
    the same name wins (e.g. the two "Eric Seanor" rows). Never raises —
    a roster fetch failure returns [] and the caller keeps raw values.
    """
    global _PEOPLE_CACHE, _PEOPLE_CACHE_AT
    now = time.monotonic()
    if _PEOPLE_CACHE is not None and now - _PEOPLE_CACHE_AT < _PEOPLE_CACHE_TTL:
        return _PEOPLE_CACHE
    try:
        resp = (
            client.table(Tables.ENTITIES)
            .select("name, email, archived_at")
            .in_("kind", list(_PERSON_KINDS))
            .execute()
        )
        rows = [r for r in (resp.data or []) if (r.get("name") or "").strip()]
        rows.sort(key=lambda r: r.get("archived_at") is not None)
        _PEOPLE_CACHE, _PEOPLE_CACHE_AT = rows, now
    except Exception as exc:  # noqa: BLE001 — resolution is enrichment, never a gate
        log.warning("commitments: entities roster fetch failed: %s", exc)
        return _PEOPLE_CACHE or []
    return _PEOPLE_CACHE


def _clean_owner_name(raw: str) -> str:
    """Normalize a transcript-shaped display name to a plain person name.

    Strips parentheticals ("Marcello Grande (He/Him/His)"), splits glued
    CamelCase ("GeoffAhmann"), collapses whitespace. Returns "" for
    diarization labels — "Speaker 1" is not a name.
    """
    name = _PAREN_RE.sub("", raw or "").strip()
    if not name or _DIARIZATION_RE.match(name):
        return ""
    if " " not in name:
        # GeoffAhmann → Geoff Ahmann; leaves "drew"/"Marcello" untouched.
        name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def resolve_owner_identity(
    client: Any,
    owner_name: str | None,
    owner_email: str | None,
) -> tuple[str | None, str | None]:
    """Canonicalize an owner against the ``entities`` person roster (#157).

    Owner strings arrive from four writers with no shared convention —
    Zoom display names, Fathom diarization labels, bare first names,
    emails-used-as-names — and the commitments filter keys on the raw
    strings, so every variant becomes a distinct "person". This resolves
    to the registry spelling wherever a confident match exists:

    1. email match on ``entities.email`` (case-insensitive) → canonical
       ``(entity.name, email)``;
    2. cleaned-name exact match (case-insensitive) → canonical name +
       the entity's email when it has one;
    3. unique containment ("kelly" ⊂ "Kelly Anderson", ≥3 chars) among
       ACTIVE people only — ambiguity keeps the raw name, never guesses;
    4. no match → cleaned name + email as given (client-side people are
       legitimately not in the registry).

    A name that is literally an email address migrates to the email slot
    rather than staying a display name. Never raises.
    """
    email = (owner_email or "").strip().lower() or None
    raw_name = (owner_name or "").strip()
    if raw_name and _EMAILISH_RE.match(raw_name):
        email = email or raw_name.lower()
        raw_name = ""
    name = _clean_owner_name(raw_name)

    try:
        people = _entity_people(client)
        if email:
            for p in people:
                if (p.get("email") or "").strip().lower() == email:
                    return p["name"].strip(), email
        if name:
            needle = name.lower()
            exact = [
                p for p in people
                if p["name"].strip().lower() == needle
            ]
            if exact:
                hit = exact[0]
                return (
                    hit["name"].strip(),
                    email or (hit.get("email") or "").strip().lower() or None,
                )
            if len(needle) >= 3:
                active = [p for p in people if p.get("archived_at") is None]
                fuzzy = [
                    p for p in active
                    if needle in p["name"].strip().lower()
                ]
                if len(fuzzy) == 1:
                    hit = fuzzy[0]
                    return (
                        hit["name"].strip(),
                        email or (hit.get("email") or "").strip().lower() or None,
                    )
    except Exception as exc:  # noqa: BLE001 — see docstring: never a gate
        log.warning("commitments: owner resolution failed: %s", exc)

    return name or None, email


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

    # #157: canonicalize the owner against the entities person roster so
    # every writer (ingest verbs, webhook, MCP) stores one spelling per
    # person. Unresolvable owners (client-side people) pass through.
    owner_name, owner_email = resolve_owner_identity(
        client, owner_name, owner_email
    )

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


def list_commitments(client: Any, owner: dict, status: str = "open") -> list[dict]:
    """List one project's/initiative's commitments, due-date ascending
    (undated last, matching the mc-2 router's ordering).

    ``owner`` is a resolve dict (``{"id", "code", "kind"}``); its ``kind``
    picks the scope column. ``status='all'`` disables the status filter.
    """
    column = "initiative_id" if owner.get("kind") == "initiative" else "project_id"
    q = client.table(Tables.COMMITMENTS).select(LIST_COLUMNS).eq(column, owner["id"])
    if status != "all":
        q = q.eq("status", status)
    resp = q.order("due_date", nullsfirst=False).execute()
    return resp.data or []


def find_open_commitment(
    client: Any, owner: dict, key: str
) -> tuple[dict | None, str | None]:
    """Resolve ``key`` to exactly ONE open commitment: ``(row, error)``.

    ``key`` is a commitment id (exact) or a case-insensitive substring of
    the description — the same exact-then-substring discipline as the spine
    element resolvers. Ambiguity is an error, not a guess: the caller gets
    the candidates back so a human (or the model) can re-key by id.
    """
    rows = list_commitments(client, owner, status="open")
    matches = [r for r in rows if r.get("id") == key]
    if not matches:
        needle = key.strip().lower()
        if needle:
            matches = [
                r for r in rows if needle in (r.get("description") or "").lower()
            ]
    if not matches:
        return None, f"no open commitment in {owner['code']!r} matches {key!r}"
    if len(matches) > 1:
        candidates = "; ".join(
            f"{r['id'][:8]}… \"{(r.get('description') or '')[:60]}\""
            for r in matches[:5]
        )
        return None, (
            f"{len(matches)} open commitments match {key!r} — "
            f"pass an id instead: {candidates}"
        )
    return matches[0], None


def close_commitment(client: Any, commitment_id: str, outcome: str) -> None:
    """Set an open commitment's status to ``done`` or ``dropped``.

    Commitments are never deleted — a dropped row is the archive, and its
    ``cp_hash`` keeps a re-ingest of the same meeting from resurrecting it.
    The table has no auto-update trigger; ``updated_at`` is set explicitly
    (mirrors the mc-2 router).
    """
    client.table(Tables.COMMITMENTS).update(
        {
            "status": outcome,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", commitment_id).execute()
