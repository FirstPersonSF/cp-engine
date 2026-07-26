"""Stage A — propose commitments from a meeting's Fathom action items.

Successor to ``clickup_propose.py`` (commitments consolidation, cp-engine
#38): action items now land as ``proposed`` rows in MC-2's
``public.commitments`` instead of ``clickup_task_proposals``. The review
gate survives as the ``date_status='proposed'`` state, ratified (or not)
by the weekly Slack dates loop and the MC-2 commitments UI.

Ingest hygiene (cp-engine #114):

- **Owner resolution** — Fathom diarization labels ("Speaker 1",
  "Speaker 2", …) are resolved against the meeting's ``participants``
  list before insert. A resolvable label stores the attendee's real
  name/email; an unresolvable one stores owner NULL plus an
  ``[owner unresolved: …]`` annotation on the description. The raw
  diarization label is NEVER stored as an owner.
- **Off-project flagging** — each item's text is screened against the
  tenant's active-project roster (the #88 cross-project detection
  roster). An item mentioning ANOTHER tracked project's code, name, or
  company gets an ``[off-project? → <code>]`` annotation so the review
  gate can reject/re-route cheaply. Detection only — nothing is
  auto-routed to the other project.

Both annotations are display-only: ``cp_hash`` is computed from the
CLEAN description, so idempotency and the sprint-file-ask identity are
unchanged.

Best-effort: any failure here is logged + swallowed. Proposing commitments
must never break the primary auto-ingest contract.

Design: cp/docs/plans/2026-07-07-commitments-consolidation-design.md
"""

from __future__ import annotations

import logging
import re

from cp_engine import mc2_db
from cp_engine.commitments import resolve_commitment_owner, write_commitment
from cp_engine.ingest import _content_hash
from cp_engine.mc2_db import Tables

import observability

log = logging.getLogger("cp-engine-webhook")

# Fathom diarization placeholder: "Speaker 1", "speaker  2", "Speaker #3".
_DIARIZATION_RE = re.compile(r"^\s*speaker\s*#?\s*\d+\s*$", re.IGNORECASE)


def _normalize_participants(participants) -> list[dict]:
    """Normalize Fathom's ``participants`` into ``[{"name", "email"}]``.

    Items may be dicts (``name`` / ``email`` fields) or bare strings
    (mirrors the defensive parsing in ``pipeline._speaker_names``).
    Anything else is ignored.
    """
    out: list[dict] = []
    for p in participants or []:
        if isinstance(p, dict):
            name = str(p.get("name") or "").strip()
            email = str(p.get("email") or "").strip() or None
            if name or email:
                out.append({"name": name or None, "email": email})
        elif isinstance(p, str) and p.strip():
            out.append({"name": p.strip(), "email": None})
    return out


def _match_participant_by_name(name: str, participants: list[dict]) -> dict | None:
    """Exact-then-unique-substring display-name match (case-insensitive).

    Exact normalized equality wins. Otherwise a one-directional
    containment ("Drew" ↔ "Drew Fiero") matches ONLY when exactly one
    participant qualifies — ambiguity means no match, never a guess.
    """
    needle = name.strip().lower()
    if len(needle) < 3:
        return None
    exact = [
        p for p in participants
        if p.get("name") and p["name"].strip().lower() == needle
    ]
    if len(exact) == 1:
        return exact[0]
    fuzzy = [
        p for p in participants
        if p.get("name")
        and (needle in p["name"].strip().lower() or p["name"].strip().lower() in needle)
    ]
    if len(fuzzy) == 1:
        return fuzzy[0]
    return None


def resolve_action_owner(
    assignee, participants
) -> tuple[str | None, str | None, str | None]:
    """Resolve an action item's assignee against the attendee list.

    Returns ``(owner_name, owner_email, unresolved_label)``:

    - a real (non-diarization) assignee passes through untouched, with a
      best-effort email enrichment when the display name uniquely
      matches an attendee;
    - a diarization label ("Speaker N") resolves via the assignee email
      (Fathom sometimes knows the email even when diarization failed) or
      a unique display-name match; the label itself is NEVER returned as
      an owner name;
    - an unresolvable label yields ``(None, None, label)`` — the caller
      annotates the description with the label so the information isn't
      lost, but the owner columns stay NULL.
    """
    name = str(assignee.get("name") or "").strip() if isinstance(assignee, dict) else ""
    email = (
        str(assignee.get("email") or "").strip() if isinstance(assignee, dict) else ""
    ) or None
    ppl = _normalize_participants(participants)

    if not name or not _DIARIZATION_RE.match(name):
        # Real name (or no name at all): pass through. When the email is
        # missing, enrich it from a unique attendee display-name match.
        if name and not email:
            match = _match_participant_by_name(name, ppl)
            if match:
                return match["name"] or name, match["email"], None
        return name or None, email, None

    # Diarization label. An email identifies the person even when the
    # display name didn't; prefer the attendee's real name for it.
    if email:
        match = next(
            (
                p for p in ppl
                if p["email"] and p["email"].lower() == email.lower()
            ),
            None,
        )
        if match and match["name"]:
            return match["name"], email, None
        return None, email, None  # email kept; label dropped

    match = _match_participant_by_name(name, ppl)
    if match:
        return match["name"], match["email"], None
    return None, None, name


def screen_off_project(
    text: str, *, ingest_codes: list[str], roster: list | None
) -> str | None:
    """Screen one action item's text against the tenant roster (#114).

    Reuses the #88 cross-project detection roster (``ProjectState`` rows
    from ``plan_from_account_meeting.list_active_all``) with deterministic
    matching — no LLM call on this path. Returns the lowercased code of
    the OTHER tracked project the text points at, or None.

    Match order (most to least specific):

    1. an explicit project code in the text (``ibx-5153``);
    2. another project's/initiative's name as a whole phrase
       ("Mission Control");
    3. another company's name — only when that company has exactly ONE
       active project (ambiguous company mentions propose nothing rather
       than propose wrongly, mirroring the #88 guard philosophy).

    Codes in ``ingest_codes`` (the meeting's own tag fan-out) and their
    companies are never treated as foreign.
    """
    if not roster or not text:
        return None
    t = text.lower()
    ingest = {str(c or "").strip().lower() for c in ingest_codes}

    own_companies = {
        (p.company_name or "").strip().lower()
        for p in roster
        if (p.code or "").strip().lower() in ingest
    }
    own_companies.discard("")

    candidates = [
        p for p in roster if (p.code or "").strip().lower() not in ingest
    ]

    # 1 — explicit code mention.
    for p in candidates:
        code = (p.code or "").strip().lower()
        if code and re.search(rf"\b{re.escape(code)}\b", t):
            return code

    # 2 — project/initiative name as a whole phrase.
    for p in candidates:
        name = (p.name or "").strip().lower()
        if len(name) >= 4 and re.search(rf"\b{re.escape(name)}\b", t):
            return (p.code or "").strip().lower()

    # 3 — company name, only when unambiguous.
    by_company: dict[str, list] = {}
    for p in candidates:
        comp = (p.company_name or "").strip().lower()
        if comp:
            by_company.setdefault(comp, []).append(p)
    for comp, plist in by_company.items():
        if comp in own_companies:
            continue
        if len(plist) == 1 and re.search(rf"\b{re.escape(comp)}\b", t):
            return (plist[0].code or "").strip().lower()
    return None


def propose_commitments(
    meeting_id: str, project_codes: list[str], roster: list | None = None
) -> dict:
    """Insert ``proposed`` commitments for a meeting's Fathom action items.

    Routes all items to the FIRST resolvable project code (multi-project
    ingests can't attribute action items per project from Fathom's payload
    — same semantics the ClickUp propose path had). Fathom action items
    carry no due date, so rows land undated — the weekly dates loop flags
    them as "needs a date", which is the design intent, not a gap.

    ``roster`` is the #88 cross-project detection roster (best-effort;
    None turns off-project screening OFF for this run). Items matching
    another tracked project get an ``[off-project? → <code>]`` suffix;
    diarization-label owners are resolved against the meeting's
    ``participants`` (see module docstring).

    The ``cp_hash`` recipe is ``_content_hash(code, "record-ask",
    description)`` — EXACTLY the sprint-file ask recipe, computed on the
    CLEAN description (annotations excluded), so a commitment and its
    sprint-file bullet share an identity and re-ingest of the same
    meeting is an idempotent no-op.

    Returns a summary dict for observability:
        {"proposed": int, "skipped_duplicate": int, "unresolved": [codes],
         "owner_unresolved": int, "off_project_flagged": int}

    Never raises.
    """
    summary: dict = {
        "proposed": 0,
        "skipped_duplicate": 0,
        "unresolved": [],
        "owner_unresolved": 0,
        "off_project_flagged": 0,
    }
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
            .select("id, action_items, participants")
            .eq("id", meeting_id)
            .single()
            .execute()
        )
        meeting = resp.data or {}
        action_items = meeting.get("action_items") or []
        participants = meeting.get("participants") or []
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

            owner_name, owner_email, unresolved_label = resolve_action_owner(
                assignee, participants
            )

            off_code = screen_off_project(
                description, ingest_codes=project_codes, roster=roster
            )

            # Annotations are display-only; the hash stays on the clean
            # description so idempotency + sprint-ask identity hold.
            flags: list[str] = []
            if unresolved_label:
                summary["owner_unresolved"] += 1
                flags.append(f"[owner unresolved: {unresolved_label}]")
            if off_code:
                summary["off_project_flagged"] += 1
                flags.append(f"[off-project? → {off_code}]")
            stored_description = " ".join([description, *flags])

            outcome = write_commitment(
                client,
                owner=owner,
                description=stored_description,
                cp_hash=_content_hash(owner["code"], "record-ask", description),
                source_kind="meeting_ingest",
                owner_email=owner_email,
                owner_name=owner_name,
                source_meeting_id=meeting_id,
            )
            summary["proposed" if outcome == "inserted" else "skipped_duplicate"] += 1

        if summary["proposed"]:
            log.info(
                "commitments-propose: %d commitment(s) for meeting=%s project=%s "
                "(owner_unresolved=%d off_project_flagged=%d)",
                summary["proposed"], meeting_id, owner["code"],
                summary["owner_unresolved"], summary["off_project_flagged"],
            )
    except Exception as exc:  # noqa: BLE001 — must never break auto-ingest
        log.warning("commitments-propose failed for meeting=%s: %s", meeting_id, exc)
        observability.capture(exc, area="commitments_propose")

    return summary
