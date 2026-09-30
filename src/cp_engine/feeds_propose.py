"""Propose the feeds edge at routing time (#174).

Routing and feeding are different acts. Routing writes `serves` — "this
belongs to that work item". Feeding is an edge — "this shaped that
deliverable" (`informs` / `derives_from` / `responds_to`), and `seal_sweep`
only treats an element as consumed by a round when an edge, or a
source -> activity -> deliverable chain, says so. Only routing ever happened
automatically, so feeding stayed a hand-drawn edge almost nobody drew.

WHAT THIS PROPOSES, AND FROM WHAT EVIDENCE
------------------------------------------
When a source is routed to a work item that already leads to a deliverable,
propose `informs` from the source to that deliverable. Two shapes qualify:

  - DIRECT: the source `serves` the deliverable's own slot. The human routed
    it to the deliverable; the edge only records what routing already said.
  - CHAIN:  the source `serves` an ACTIVITY that has an ACTIVE
    `informs`/`derives_from` edge to a deliverable. Both hops are human
    judgements — the routing and the activity -> deliverable edge — so the
    proposal inherits evidence rather than inventing it. (#174's 2026-08-11
    correction dropped this design because that second hop had ONE edge
    tenant-wide; there are now 16, which is what makes it buildable.)

A bare estimate slot with no deliverable behind it proposes nothing. Guessing
a deliverable for it would manufacture the edge, which is the failure the
re-scope warned about: a wrong feeds edge later teaches the seal sweep to
absorb live work.

REVIEW-GATED, NEVER CONFIRMED
-----------------------------
Every row is written `status='proposed'`, `source='auto_ingest'` — the gate
mig 117 built and mc-2's Suggestions inbox already reads (Confirm / Dismiss,
`POST /api/spine/{code}/relations/confirm|dismiss`). Nothing here writes an
`active` edge. Confidence is deliberately held BELOW the inbox's 0.8
"Confirm all" threshold, so routing-derived edges are confirmed one at a
time, not swept in by a batch button meant for high-certainty guesses.

THE #270 GUARD
--------------
A document cannot have fed work that finished before it arrived. Measured on
ibx-5192: 14 documents ingested 07-20..07-23 were routed to a 06-27 debrief —
a bulk route, not curation — and that debrief feeds both live decks. Without
this guard the rule would turn one bad routing into 28 plausible-looking
edges. So a pair is skipped when the source's evidence date is after the
deliverable's latest version, or (chain) after the activity it was routed to.
The comparison is `stub_sweep.postdates` — one spelling of the rule.

The evidence date is the DOCUMENT'S arrival (earliest `rag_assets.created_at`
among the source's `sources`), per #274 — not the card's `version_date`,
which a backfill stamps with the backfill date. Only when no source date is
known does it fall back to `version_date`, and that fallback errs toward
SKIPPING (a backfilled card looks newer than its document), never toward
proposing. A source with no date at all is skipped and counted, not guessed.

Never re-proposed: any existing edge between the pair — active, proposed or
DISMISSED, of any feeds kind — suppresses the proposal. A dismissal is a
human "no", and mig 117 keeps the row precisely so it stays one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

# The kind this proposes. `informs` — "shaped but didn't generate" — is the
# weakest claim that is still consumption evidence to `seal_sweep`. Routing
# does not tell us a deliverable was BUILT from a source (`derives_from`).
PROPOSED_KIND = "informs"

# Existing edges of these kinds between the pair mean the question has been
# asked already (or answered, if absorbed). Checked in ANY status.
_PAIR_KINDS = frozenset({"informs", "derives_from", "responds_to", "absorbed_by"})

# The activity -> deliverable hop the chain walks. Same kinds seal_sweep's
# `FEEDS_KINDS` treats as feeding, minus `responds_to`: an activity does not
# "respond to" a deliverable in the sense of producing it.
_CHAIN_KINDS = frozenset({"informs", "derives_from"})

# Below mc-2 SuggestionsView's HIGH_CONFIDENCE (0.8) on purpose — see header.
CONFIDENCE_DIRECT = 0.6
CONFIDENCE_CHAIN = 0.5

# A stakeholder's `serves` means relevance, not work (#179 step 4) — mirrors
# hosted `_RELEVANCE_SERVES_LAYERS`. A person does not feed a deliverable.
_RELEVANCE_LAYERS = frozenset({"stakeholders", "stakeholder"})

# The deliverable layers seal_sweep accepts. A target whose `card_kind` says
# deliverable but whose layer says otherwise is the #177 misclassification
# shape — measured 2026-09-30, all 6 direct routings that survived the date
# guard pointed at two such cards on ibx-5192 ("Clarification on the
# deliverable for Mehul", layer Note; "Kick off and direction for Geoff",
# layer Activity): meeting notes sitting on a deliverable slot, while the real
# deck is a different card. An edge there points at the wrong card, so it is
# HELD BACK and reported — fix the classification first, then re-route.
_DELIVERABLE_LAYER_NAMES = frozenset({"deliverables", "output"})

# Columns the reader needs. No `body`: authored bodies run to ~100k chars and
# nothing here reads them. NEVER select('*').
FEEDS_PROPOSE_COLUMNS = (
    "id, est_item_id, framing, layer, card_kind, placement, status, archived, "
    "version_label, version_date, serves, sources, project_id"
)


def _letters(value: Any) -> str:
    return re.sub(r"[^a-z]", "", str(value or "").lower())


def _is_deliverable(row: dict) -> bool:
    from cp_engine.seal_sweep import _is_deliverable as seal_is_deliverable

    return seal_is_deliverable(row)


def _is_activity(row: dict) -> bool:
    from cp_engine.card_class import CardKind, classify

    return not _is_deliverable(row) and classify(row) is CardKind.ACTIVITY


def _is_source(row: dict) -> bool:
    """Capture or reference — anything that is not itself a unit of work."""
    from cp_engine.card_class import classify

    if _letters(row.get("layer")) in _RELEVANCE_LAYERS:
        return False
    return not classify(row).is_card


@dataclass(frozen=True)
class FeedsProposal:
    """One proposed `informs` edge: source -> deliverable."""

    from_item_id: str
    to_item_id: str
    #: The activity the source reached the deliverable through; "" = direct.
    via: str
    via_framing: str
    source_framing: str
    deliverable_framing: str
    evidence_date: str
    confidence: float

    @property
    def note(self) -> str:
        """The one-line why stored on the edge and shown in the inbox."""
        if self.via:
            why = f"routed to {self.via_framing}, which feeds this deliverable"
        else:
            why = "routed to this deliverable's slot"
        return f"Proposed at routing (#174): {why}."


@dataclass(frozen=True)
class Skipped:
    from_item_id: str
    to_item_id: str
    via: str
    #: postdates | undated | edge_exists | absorbed | target_misfiled
    reason: str
    detail: str = ""


def _evidence_date(row: dict, source_dates: dict[str, str]) -> str:
    from cp_engine.stub_sweep import earliest_source_date

    doc = earliest_source_date(list(row.get("sources") or []), source_dates)
    if doc:
        return doc
    return str(row.get("version_date") or "")[:10]


def propose_feeds(
    rows: list[dict],
    relations: list[dict],
    *,
    source_dates: dict[str, str] | None = None,
    only: Iterable[str] | None = None,
) -> tuple[list[FeedsProposal], list[Skipped]]:
    """The proposals the routing in `rows` implies, and what was held back.

    `rows` are ONE project's live, unarchived spine_substance rows
    (FEEDS_PROPOSE_COLUMNS shape). `relations` are that project's
    spine_relations rows in EVERY status — a dismissed pair must suppress.
    `source_dates` maps a rag_asset id to its arrival day. `only` limits the
    proposals to these source est_item_ids — the routing-time scope, where one
    element was just re-routed and nothing else should be re-litigated.

    Pure: no I/O.
    """
    source_dates = source_dates or {}
    only_set = set(only) if only is not None else None

    by_eid: dict[str, dict] = {}
    alias: dict[str, str] = {}
    for r in rows:
        eid = r.get("est_item_id")
        if not eid or r.get("archived") or (r.get("status") or "live") != "live":
            continue
        by_eid[eid] = r
        if r.get("id"):
            alias[str(r["id"])] = eid

    def _eid(endpoint: Any) -> str:
        # Endpoints are est_item_ids by contract (mig 117); a stray substance
        # id is mapped rather than silently missed.
        e = str(endpoint or "")
        return alias.get(e, e)

    # activity -> [deliverable], from ACTIVE human edges only.
    feeds_from_activity: dict[str, list[str]] = {}
    # (from, to) pairs already asked, in any status.
    asked: set[tuple[str, str]] = set()
    absorbed: set[str] = set()
    for e in relations:
        kind = e.get("kind")
        src, dst = _eid(e.get("from_item_id")), _eid(e.get("to_item_id"))
        if not src or not dst:
            continue
        status = e.get("status") or "active"
        if kind in _PAIR_KINDS:
            asked.add((src, dst))
        if kind == "absorbed_by" and status == "active":
            absorbed.add(src)
        if (
            kind in _CHAIN_KINDS
            and status == "active"
            and src in by_eid
            and dst in by_eid
            and _is_activity(by_eid[src])
            and _is_deliverable(by_eid[dst])
        ):
            seen = feeds_from_activity.setdefault(src, [])
            if dst not in seen:
                seen.append(dst)

    proposals: list[FeedsProposal] = []
    skipped: list[Skipped] = []
    emitted: set[tuple[str, str]] = set()

    for eid, row in by_eid.items():
        if only_set is not None and eid not in only_set:
            continue
        if not _is_source(row):
            continue
        ev_date = _evidence_date(row, source_dates)

        # (deliverable, via-activity) targets this routing implies.
        targets: list[tuple[str, str]] = []
        for slot in row.get("serves") or []:
            slot = str(slot or "")
            target = by_eid.get(slot)
            if not slot or slot == eid or target is None:
                continue  # bare estimate slot: nothing to propose
            if _is_deliverable(target):
                targets.append((slot, ""))
            elif slot in feeds_from_activity:
                targets.extend((d, slot) for d in feeds_from_activity[slot])
        # Direct first, so a source routed both ways keeps the stronger reason.
        targets.sort(key=lambda t: bool(t[1]))

        for dlv, via in targets:
            if dlv == eid or (eid, dlv) in emitted:
                continue
            if (eid, dlv) in asked:
                skipped.append(Skipped(eid, dlv, via, "edge_exists"))
                continue
            if eid in absorbed:
                skipped.append(Skipped(eid, dlv, via, "absorbed"))
                continue
            if not ev_date:
                skipped.append(Skipped(eid, dlv, via, "undated"))
                continue
            from cp_engine.stub_sweep import postdates

            dlv_row = by_eid[dlv]
            if _letters(dlv_row.get("layer")) not in _DELIVERABLE_LAYER_NAMES:
                skipped.append(Skipped(
                    eid, dlv, via, "target_misfiled",
                    f"kind deliverable, layer {dlv_row.get('layer')!r} (#177)"))
                continue
            dlv_date = str(dlv_row.get("version_date") or "")[:10]
            if postdates(ev_date, dlv_date):
                skipped.append(Skipped(
                    eid, dlv, via, "postdates",
                    f"arrived {ev_date} > deliverable {dlv_date}"))
                continue
            if via:
                act_date = str(by_eid[via].get("version_date") or "")[:10]
                if postdates(ev_date, act_date):
                    skipped.append(Skipped(
                        eid, dlv, via, "postdates",
                        f"arrived {ev_date} > activity {act_date}"))
                    continue
            emitted.add((eid, dlv))
            proposals.append(FeedsProposal(
                from_item_id=eid,
                to_item_id=dlv,
                via=via,
                via_framing=(by_eid[via].get("framing") or via) if via else "",
                source_framing=row.get("framing") or eid,
                deliverable_framing=dlv_row.get("framing") or dlv,
                evidence_date=ev_date,
                confidence=CONFIDENCE_CHAIN if via else CONFIDENCE_DIRECT,
            ))

    proposals.sort(key=lambda p: (p.to_item_id, bool(p.via), p.source_framing.lower()))
    return proposals, skipped


def fetch_inputs(
    client, project_id: str
) -> tuple[list[dict], list[dict], dict[str, str]]:
    """(rows, relations, source_dates) for one project. Read-only.

    Relations are read in EVERY status: a dismissed proposal must keep the
    pair from being proposed again. Source dates are best-effort — a failed
    lookup leaves them empty, which only makes the guard fall back to the
    card's own date (the conservative direction).
    """
    from cp_engine.mc2_db import Tables

    rows = (
        client.table(Tables.SPINE_SUBSTANCE)
        .select(FEEDS_PROPOSE_COLUMNS)
        .eq("project_id", project_id)
        .eq("status", "live")
        .execute()
        .data
    ) or []
    rows = [r for r in rows if not r.get("archived")]
    relations = (
        client.table(Tables.SPINE_RELATIONS)
        .select("kind, from_item_id, to_item_id, status")
        .eq("project_id", project_id)
        .execute()
        .data
    ) or []
    source_dates: dict[str, str] = {}
    asset_ids = sorted({
        str(src["id"])
        for r in rows
        for src in (r.get("sources") or [])
        if isinstance(src, dict) and src.get("id")
    })
    if asset_ids:
        try:
            for a in (
                client.table(Tables.RAG_ASSETS)
                .select("id, created_at")
                .in_("id", asset_ids)
                .execute()
                .data
            ) or []:
                created = str(a.get("created_at") or "")
                if a.get("id") and created:
                    source_dates[str(a["id"])] = created[:10]
        except Exception:  # noqa: BLE001 — advisory, see docstring
            pass
    return rows, relations, source_dates


def write_proposals(
    client,
    proposals: list[FeedsProposal],
    *,
    project_id: str,
    project_code: str,
    created_by: str,
) -> dict[str, Any]:
    """Insert each proposal as a `status='proposed'` edge. Never `active`.

    `created_by` must be the caller's verified email on the hosted path — the
    INSERT policy is `created_by = auth.jwt()->>'email'`. A 23505 on the
    mig-117 unique constraint means the pair already has this kind of edge,
    which is the same outcome as proposing it, not a failure.
    """
    from cp_engine.mc2_db import Tables

    written: list[dict] = []
    already = 0
    errors: list[str] = []
    for p in proposals:
        try:
            client.table(Tables.SPINE_RELATIONS).insert({
                "project_id": project_id,
                "project_code": project_code,
                "kind": PROPOSED_KIND,
                "from_item_id": p.from_item_id,
                "to_item_id": p.to_item_id,
                "status": "proposed",
                "source": "auto_ingest",
                "confidence": p.confidence,
                "note": p.note,
                "created_by": created_by,
            }).execute()
            written.append({
                "from_item_id": p.from_item_id,
                "to_item_id": p.to_item_id,
                "via": p.via or None,
                "confidence": p.confidence,
            })
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "23505" in msg or "duplicate key" in msg.lower():
                already += 1
                continue
            errors.append(f"{p.from_item_id} -> {p.to_item_id}: {msg[:200]}")
    return {"proposed": written, "already": already, "errors": errors}


def propose_on_route(
    client,
    *,
    project_id: str,
    project_code: str,
    est_item_ids: Iterable[str],
    created_by: str,
) -> dict[str, Any]:
    """The routing-time hook: after `serves` changes on these elements,
    propose the feeds edges the new routing implies. One call for every
    routing surface (hosted `set_spine_element`, mc-2 `patch_substance`).

    Callers treat it as NON-FATAL: the routing has already committed, and a
    failed proposal must never turn it into an error.
    """
    ids = [e for e in est_item_ids if e]
    if not ids:
        return {"proposed": [], "skipped": [], "already": 0, "errors": []}
    rows, relations, source_dates = fetch_inputs(client, project_id)
    proposals, skipped = propose_feeds(
        rows, relations, source_dates=source_dates, only=ids
    )
    out = write_proposals(
        client, proposals,
        project_id=project_id, project_code=project_code, created_by=created_by,
    )
    out["skipped"] = [
        {"from_item_id": s.from_item_id, "to_item_id": s.to_item_id,
         "reason": s.reason, "detail": s.detail}
        for s in skipped
    ]
    return out


def render_report(
    proposals: list[FeedsProposal], skipped: list[Skipped], *, code: str
) -> str:
    """The read-only CLI surface: what routing implies, and what it held back."""
    out: list[str] = []
    if proposals:
        out.append(
            f"{code} — {len(proposals)} feeds edge(s) the current routing implies:")
        out.append("")
        for p in proposals:
            how = f"via {p.via_framing}" if p.via else "direct"
            out.append(f"  [{how}] {p.source_framing}  ({p.evidence_date})")
            out.append(f"    → informs {p.deliverable_framing}")
            out.append(f"      {p.from_item_id} -> {p.to_item_id}")
        out.append("")
    else:
        out.append(f"{code} — the current routing implies no new feeds edge.")
        out.append("")
    by_reason: dict[str, int] = {}
    for s in skipped:
        by_reason[s.reason] = by_reason.get(s.reason, 0) + 1
    if by_reason:
        parts = ", ".join(f"{n} {r}" for r, n in sorted(by_reason.items()))
        out.append(f"Held back: {parts}.")
        post = [s for s in skipped if s.reason == "postdates"]
        if post:
            out.append(
                f"  {len(post)} arrived AFTER the work they were routed to, "
                "or the deliverable it leads to (#270) — re-route before "
                "anything is wired.")
        mis = [s for s in skipped if s.reason == "target_misfiled"]
        if mis:
            out.append(
                f"  {len(mis)} point at a card whose kind says deliverable but "
                "whose layer does not (#177) — fix the classification first.")
    out.append(
        "Read-only. Routing proposes these automatically from now on, as "
        "`proposed` edges in the Suggestions inbox; nothing here writes.")
    return "\n".join(out)
