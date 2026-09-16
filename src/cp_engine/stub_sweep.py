"""``cp stub-sweep`` — empty Source-material cards, and where their provenance belongs (#178).

82 of the tenant's live elements are Source-material cards under 200 chars.
Their bodies are boilerplate the ingest wrote:

    Ingested document: **Marcello Grande** (doc)

    rag_asset: 1fb5e23e-0cfe-4d85-87e8-903d46c48a33

and the SAME rag_asset is already in the card's `sources` array. The card is a
wrapper around a pointer it duplicates. Provenance has a home — the `sources`
array, surfaced as "Documents & sources" — and a document that has been read
should be a SOURCE on the card that used it, not a peer card beside it.

WHAT THE ISSUE GOT WRONG, AND WHY THIS IS NOT A DELETE
------------------------------------------------------
#178 says "attach where they informed something, retire where they informed
nothing". But **79 of the 82 carry a `serves` binding** — the routing
judgement naming which activity or slot the document belongs to. That is real
human input, and it is what `seal_sweep`'s chain walk reads to find what fed a
deliverable. Collapsing the cards naively would throw all of it away.

So the move is a TRANSFER, not a deletion: attach the stub's rag_asset to the
element it serves, then retire the stub. The provenance ends up on the card
that actually used the document, which is what the issue wanted.

That only works when the `serves` target IS a spine element. Measured
2026-08-11: 65 stubs serve a real element (attachable), 14 serve a bare
estimate slot with nothing to attach to. Those are REPORTED, never guessed at
— inventing a target would be the same mistake as manufacturing a feeds edge.

Read-only, like every sweep here. Retiring a card and moving its provenance is
a real mutation; this makes the decision cheap and leaves it to a human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Bodies this short on Source material are the ingest's boilerplate, not a
# thought. The observed range is 93–~200 chars; the threshold matches #178's
# measurement so the two agree on what "stub" means.
STUB_BODY_MAX = 200

# The exact shape ingest writes. A card whose body is ONLY this (plus its
# rag_asset line) carries nothing a human added — if someone has written into
# it, the extra prose pushes it over STUB_BODY_MAX and it is not a stub.
_BOILERPLATE_RE = re.compile(
    r"^\s*ingested\s+(?:document|file|source)\s*:", re.IGNORECASE)

# RETAINED as the fallback only. `_is_stream` below asks `card_class` first —
# an element that is not a card IS Stream, whatever its layer string says.
#
# #179: "is this work?" was inferred from layer strings in four places, each
# with its own hand-maintained list, and that inference is what broke in #172
# (`Output` vs `Deliverables`). `card_kind` is the explicit answer; these
# strings only cover rows written before the field existed, which is exactly
# the case `classify()` already handles as its own fallback.
SOURCE_LAYERS = ("source material", "sourcematerial", "source")


def _norm(v: Any) -> str:
    return str(v or "").strip().lower()


def _is_stream(row: dict) -> bool:
    """True when this element is capture, not work (#179).

    Asks `card_class.classify()` first — an element whose `card_kind` says
    `attachment` IS Stream, and that is an explicit answer rather than a guess
    at a layer string. Falls back to SOURCE_LAYERS only when the row predates
    the field AND `classify()` itself had to infer, so a row that classifies
    confidently as a card is never re-litigated by a string list.
    """
    from cp_engine.card_class import classify, classify_is_inferred

    if not classify_is_inferred(row):
        # #179: `is_stream` is the Stream question specifically. `not is_card`
        # would also sweep up REFERENCE — the 245 authored, deliberately
        # unbound elements (Stakeholders, Retrospectives, unrouted synthesis)
        # that are correct as they are and are NOT stubs to be collapsed.
        return classify(row).is_stream
    # `classify()` guessed too — prefer the narrow historical list over its
    # broader inference, since this sweep's contract is Source material only.
    return _norm(row.get("layer")) in SOURCE_LAYERS


def _is_work(row: dict) -> bool:
    """True when this row is WORK — the only thing that can date a project.

    Distinct from `not _is_stream(row)`, which was the old spelling and which
    now also admits LINK carriers and REFERENCE. Reference was always excluded
    by the inference fallback below; LINK would not have been (#179 opt 3).
    """
    from cp_engine.card_class import classify, classify_is_inferred

    if not classify_is_inferred(row):
        return classify(row).is_card
    # Pre-field rows: the old spelling is the best available answer.
    return not _is_stream(row)


def _is_sweepable(row: dict) -> bool:
    """True when this row belongs in the sweep's candidate set at all (#179).

    Stream is sweepable, and so is LINK — but for different reasons, which is
    why this is not `_is_stream`.

    A link-carrier is NOT stream (`CardKind.is_stream` is False for it):
    retiring it drops a pointer nothing else holds, so it must never be
    proposed for retirement on the strength of being capture-shaped. But it
    still has to ENTER the sweep, because the sweep is where
    `link_has_a_home` notices that its work item has since gained a bound
    element and the carrier has become free. Excluding it here would make the
    self-clearing rule unreachable — the carriers would simply disappear from
    the report and never be revisited.

    `find_stubs` admits both; the RENDER decides what to propose for each.
    """
    from cp_engine.card_class import classify, classify_is_inferred

    if not classify_is_inferred(row) and classify(row).is_link_carrier:
        return True
    return _is_stream(row)


@dataclass
class Stub:
    """An empty Source-material card and the element its provenance belongs on."""

    est_item_id: str
    framing: str
    body_len: int
    sources: list[dict] = field(default_factory=list)
    # Resolved `serves` targets that ARE live spine elements:
    # (id, framing, target_is_sound).
    targets: list[tuple[str, str, bool]] = field(default_factory=list)
    # `serves` entries that resolve to nothing — bare estimate slots.
    unresolved: list[str] = field(default_factory=list)
    has_edges: bool = False
    # est_item_id of the work-class element now bound to this carrier's work
    # item, when exactly one is (see `link_has_a_home`). "" when none is.
    _link_home: str = ""
    # The row's STORED card_kind, when it had one. `link` is authoritative for
    # `carries_a_work_item_link`; anything else falls back to derivation.
    _card_kind: str = ""
    # The DOCUMENT's arrival date (rag_assets.created_at) and its target's
    # version_date — used by `postdates_target`.
    #
    # `_doc_date` is deliberately NOT the stub row's `version_date` (#274).
    # That is when the STUB was created, which a later backfill sets to the
    # backfill date: ibx-5153 minted 47 stubs in a four-minute window on
    # 2026-08-17 for documents that had arrived on 2026-06-17, and every one
    # was reported as a bulk route. Empty when the source's date could not be
    # resolved — the check then stays silent rather than guessing.
    _doc_date: str = ""
    _target_date: str = ""
    # The project's earliest WORK element date, for `looks_foundational`
    # (#275). "" when the project has no dated work element to compare to.
    _first_work_date: str = ""

    @property
    def postdates_target(self) -> bool:
        """The DOCUMENT arrived after the work it supposedly fed.

        A source cannot have informed a session that happened before it
        arrived. Real on ibx-5192: 8 use-case briefs ingested 2026-07-21 were
        routed to a 2026-06-27 debrief, 24 days earlier. They belonged to the
        07-22 metrics-inventory synthesis, which is literally the synthesis of
        those briefs.

        MEASURE THE DOCUMENT, NOT THE CARD (#274). This compared the STUB's
        `version_date` until 2026-09-16, which is when the card was written,
        not when the document landed. A backfill therefore flagged everything
        it touched: ibx-5153's 47 stubs were all stamped 2026-08-17 for
        documents from 2026-06-17 — the workshop's own agenda, preread, bet
        board and transcript, routed to that workshop, reported as a bulk
        route. Re-measured against real arrival dates, 43 of 50 were correct.
        Acting on the old flag would have detached them.

        Silent when `_doc_date` is unknown: an unresolved source date is not
        evidence of anything.
        """
        return bool(self._doc_date and self._target_date
                    and self._doc_date > self._target_date)

    @property
    def looks_foundational(self) -> bool:
        """The document predates the project's own work — it is founding
        material, and a specific dated session is the wrong home for it (#275).

        THE GAP THIS CLOSES. `postdates_target` catches a document that arrived
        AFTER the work it claims to have fed. It cannot see the opposite error,
        because the dates are fine: on ibx-5153, 17 stubs were routed to the
        07-03 "Campaign Opportunities Report walkthrough" and every one of them
        PREDATED it, so the guard was correctly silent. They were the Apr 23
        kickoff briefing, the 4/10 and 4/23 input briefs, the first pitch and an
        analyst report — the project's founding material, filed against a
        client feedback session three months later.

        Worse, the right home was already documented: `_authored/inputs-briefing`
        names several of those documents in its own prose while carrying none of
        them as sources. The knowledge existed; the wiring pointed elsewhere.

        The signal is that the document predates the project's EARLIEST work
        element. A source that was in hand before any work existed cannot have
        been produced by, or for, one particular later session — it is context
        the whole engagement stands on.

        DELIBERATELY A QUESTION, NOT A VERDICT. Rendered as `ℹ`, not `⚠`, and
        worded as a prompt. #274's lesson is that a confidently-phrased flag
        gets acted on in bulk: "almost certainly a bulk route" is what made a
        70-stub migration look actionable when 43 of 50 were correct. A false
        positive here must be cheap to dismiss.
        """
        if not (self._doc_date and self._first_work_date):
            return False
        # Equal dates are the normal case for a kickoff: the brief and the
        # first activity land the same day. Only strictly-earlier is a signal.
        return self._doc_date < self._first_work_date

    @property
    def unsound_targets(self) -> list[tuple[str, str, bool]]:
        """Targets that spine-lint would flag — unlayered or misfiled.

        Measured 2026-08-11: 34 of 65 attachable stubs route to a card with
        `layer: null`. Moving real provenance onto a broken card buries it,
        so those are named rather than silently proposed.
        """
        return [t for t in self.targets if not t[2]]

    @property
    def attachable(self) -> bool:
        """Its provenance has somewhere to go."""
        return bool(self.targets) and bool(self.sources)

    @property
    def orphan(self) -> bool:
        """Routed nowhere resolvable — nothing to attach to."""
        return not self.targets

    @property
    def link_has_a_home(self) -> bool:
        """This carrier's work item has since gained a bound WORK element, so
        the pointer it holds now has somewhere to live (#179 option 3).

        The self-clearing half of the carrier rule. A link-carrier is not
        retirable merely for being one — retiring it would drop the link. It
        becomes retirable when the thing it points AT acquires an element that
        can hold a source: re-route the document there and the card is free.

        Computed in `find_stubs` from the same work-item -> bound-element
        index the frontend's routing uses (`routeDocument.boundElementIndex`),
        so the sweep proposes a move exactly when routing would now make it.
        Measured 2026-09-16: 0 of the tenant's 11 residue carriers resolve
        today, which is the point — the backlog clears itself as elements
        appear rather than needing a sweep to re-judge it.
        """
        return bool(self._link_home)

    @property
    def carries_a_work_item_link(self) -> bool:
        """It was minted to hold a pointer for a WORK ITEM, and is doing that
        job — not a Source-material card somebody created by mistake (#179).

        THE DISTINCTION THIS DRAWS. A work item (an estimate deliverable or
        activity, or an initiative milestone) has no `spine_substance` row, so
        it has no `sources` array to attach a document to. Routing a document
        there therefore MINTS a card to carry the link — deliberately, and
        `routeTarget.ts` keeps the mint alive for exactly this reason.

        The card that results looks identical to one created by the bug this
        sweep exists to clean up: a Source-material stub whose whole body is
        "Ingested document: **X**". Measured 2026-09-16: of 42 live Stream
        elements carrying a binding, 24 point at a real spine element (those
        never needed a card) and **18 point at a work item** (those are
        link-carriers).

        Calling all 42 backlog overstates the problem by more than half, and —
        worse — invites someone to "migrate" a card whose only job is to hold
        a pointer nothing else can hold.

        STORED FIRST (#179 option 3). `card_kind='link'` is written by the
        mint at the moment it makes this exact decision, so a row that carries
        it needs no derivation. The `serves`-based signal below stays as the
        fallback for rows written before the kind existed — the same
        migration-aid shape `classify()` uses for layer inference, and it has
        the same weakness: it cannot tell "minted for a work item" from
        "minted for a slot somebody later deleted".

        The fallback signal is a `serves` target that resolves to NO live
        element. That is the same condition as `orphan`, read the other way
        round: `orphan` says "nothing to attach to", which is true; this says
        WHY, which is the part that decides whether to act.
        """
        if self._card_kind == "link":
            return True
        return bool(self.unresolved) and bool(self.sources)


def find_stubs(
    rows: list[dict],
    relations: list[dict] | None = None,
    *,
    body_max: int = STUB_BODY_MAX,
    source_dates: dict[str, str] | None = None,
) -> list[Stub]:
    """Empty Source-material cards with their resolved `serves` targets.

    `rows` are live spine_substance rows (STUB_SWEEP_COLUMNS shape). Pure —
    no I/O — so the classification is testable without a database.

    `source_dates` maps a `rag_asset` id to the date that document ARRIVED
    (`rag_assets.created_at`, ISO day). The caller fetches it; without it
    `postdates_target` stays silent rather than falling back to the stub's own
    `version_date`, which is what made the check wrong (#274).
    """
    source_dates = source_dates or {}
    relations = relations or []
    by_id = {r.get("est_item_id"): r for r in rows if r.get("est_item_id")}

    # The earliest dated WORK element in the project (#275). A Source-material
    # card is capture, not work, so it cannot set this floor — otherwise the
    # founding documents would define the very date they are tested against.
    #
    # Asked as "is it WORK" rather than "is it not stream" (#179 option 3): a
    # LINK carrier is not stream, but it is not work either — it is a pointer
    # minted on the day a document was routed. Letting it set the floor would
    # reintroduce #275 exactly, with the carrier dating the work it points at.
    work_dates = [
        str(r.get("version_date") or "")
        for r in rows
        if _is_work(r) and r.get("version_date")
    ]
    first_work_date = min(work_dates) if work_dates else ""

    # Work item -> the ONE work-class element bound to it (#179 option 3, the
    # self-clearing half of `link_has_a_home`). Mirrors the frontend's
    # `routeDocument.boundElementIndex`, and for the same two reasons:
    # work-class only (resolving onto another capture card just moves the
    # problem) and unambiguous only (several bound elements is a guess about
    # which deliverable a document belongs to, and this sweep's contract is to
    # propose only what the data can settle).
    from cp_engine.card_class import classify

    _bound: dict[str, list[str]] = {}
    for r in rows:
        if not classify(r).is_card:
            continue
        self_id = r.get("est_item_id")
        if not self_id:
            continue
        for slot in r.get("serves") or []:
            if not slot or slot == self_id:
                continue
            seen = _bound.setdefault(slot, [])
            if self_id not in seen:
                seen.append(self_id)
    link_homes = {k: v[0] for k, v in _bound.items() if len(v) == 1}

    edged: set[str] = set()
    for e in relations:
        for side in ("from_item_id", "to_item_id"):
            if e.get(side):
                edged.add(e[side])

    out: list[Stub] = []
    for row in rows:
        eid = row.get("est_item_id")
        if not eid or not _is_sweepable(row):
            continue
        body = row.get("body") or ""
        if len(body) > body_max:
            continue
        # Only the ingest's own boilerplate qualifies. A short hand-written
        # note is thin, but it is somebody's thought — not this sweep's call.
        if body and not _BOILERPLATE_RE.match(body):
            continue

        targets: list[tuple[str, str, bool]] = []
        unresolved: list[str] = []
        target_date = ""
        for slot in row.get("serves") or []:
            target = by_id.get(slot)
            if target is not None and slot != eid:
                # A target with no layer is one spine-lint already flags as
                # unfilable. Provenance moved onto it is provenance buried.
                sound = target.get("layer") is not None
                targets.append((slot, target.get("framing") or slot, sound))
                if not target_date:
                    target_date = str(target.get("version_date") or "")
            else:
                unresolved.append(slot)

        out.append(
            Stub(
                est_item_id=eid,
                framing=row.get("framing") or eid,
                body_len=len(body),
                sources=list(row.get("sources") or []),
                targets=targets,
                unresolved=unresolved,
                has_edges=eid in edged,
                _card_kind=str(row.get("card_kind") or "").strip().lower(),
                _link_home=next(
                    (link_homes[u] for u in unresolved if u in link_homes), ""
                ),
                _doc_date=_earliest_source_date(
                    row.get("sources") or [], source_dates
                ),
                _target_date=target_date,
                _first_work_date=first_work_date,
            )
        )

    out.sort(key=lambda s: (s.orphan, s.framing.lower()))
    return out



def _earliest_source_date(
    sources: list, source_dates: dict[str, str]
) -> str:
    """The arrival date of the stub's OLDEST source, or "" if unknown.

    Earliest rather than latest: a card wrapping several documents has fed its
    target from the moment the first one landed, so the earliest is the date
    that could exonerate the routing. Picking the latest would flag a card
    whose bulk of material predates the work.
    """
    dates = [
        source_dates[sid]
        for src in sources
        if isinstance(src, dict) and (sid := src.get("id")) in source_dates
    ]
    return min(dates) if dates else ""



def verify_transfer(
    stubs: list[Stub],
    target_key: str,
    target_sources: list[dict],
    *,
    relations: list[dict] | None = None,
) -> str:
    """Is it safe to retire the stubs routed to `target_key`?

    The check the transfer procedure requires, which every operator otherwise
    reinvents as a SQL query — and reinvents INCOMPLETELY. Both halves of #276
    are failures of a hand-rolled verification:

    - A list built from the printed titles moved one source of a four-source
      card and retired it, stranding three. Near-missed on ibx-5153.
    - A list built from a source-provenance query retired a card carrying a
      typed edge, destroying it. That one was NOT a near miss.

    So this answers both questions at once. A verifier that checked only
    sources would have returned green on the call that destroyed the edge,
    which is why edges are not a separate mode.

    Returns a report. `SAFE TO RETIRE` appears only when every source of every
    stub is already on the target AND no stub carries an active typed edge.
    """
    relations = relations or []
    edged: set[str] = set()
    for e in relations:
        if (e.get("status") or "active") != "active":
            continue
        for side in ("from_item_id", "to_item_id"):
            if e.get(side):
                edged.add(e[side])

    on_target = {
        str(src.get("id"))
        for src in target_sources
        if isinstance(src, dict) and src.get("id")
    }

    routed = [
        st for st in stubs
        if any(tid == target_key for tid, _name, _sound in st.targets)
    ]
    if not routed:
        return f"No stubs route to {target_key!r}."

    missing: list[tuple[str, str]] = []
    blocked: list[str] = []
    for st in routed:
        for src in st.sources:
            if not isinstance(src, dict):
                continue
            sid = str(src.get("id") or "")
            if sid and sid not in on_target:
                missing.append(
                    (st.est_item_id, str(src.get("title") or sid))
                )
        if st.est_item_id in edged:
            blocked.append(st.est_item_id)

    pointers = sum(
        len([x for x in st.sources if isinstance(x, dict)]) for st in routed
    )
    out = [
        f"{len(routed)} stub(s) · {pointers} source pointer(s) routed to "
        f"{target_key}",
        "",
    ]
    if missing:
        out.append(
            f"✗ {len(missing)} source(s) are NOT on the target yet — "
            "attach these before retiring, or their only pointer dies with "
            "the card:")
        for eid, title in missing:
            out.append(f"    {eid}")
            out.append(f"      · {title}")
        out.append("")
    if blocked:
        out.append(
            f"✗ {len(blocked)} stub(s) carry an ACTIVE typed edge, which "
            "retiring DELETES and cannot restore — retire these singly, with "
            "the edge in view:")
        for eid in blocked:
            out.append(f"    {eid}")
        out.append("")
    if not missing and not blocked:
        out.append(
            f"✓ SAFE TO RETIRE — all {pointers} source pointer(s) are on the "
            "target, and no stub carries a typed edge.")
    return "\n".join(out)


def render_sweep(stubs: list[Stub], *, code: str) -> str:
    """The review surface: what to move where, and what cannot move."""
    if not stubs:
        return f"{code} — no empty Source-material cards."

    attachable = [s for s in stubs if s.attachable]
    orphans = [s for s in stubs if s.orphan]
    edged = [s for s in stubs if s.has_edges]

    out: list[str] = []
    if attachable:
        out.append(
            f"{len(attachable)} stub(s) whose provenance has somewhere to go — "
            "attach the source to what it served, then retire the card:")
        out.append("")
        for s in attachable:
            titles = [
                str(src.get("title") or src.get("id"))
                for src in s.sources if isinstance(src, dict)
            ]
            out.append(f"  {s.framing}  ({s.body_len} chars)")
            out.append(f"    {s.est_item_id}")
            # ONE LINE PER SOURCE when a card wraps more than one (#276). The
            # joined form read as a single document, so a transfer list built
            # from the printed titles moved one of four and retired the card —
            # the other three lost their only pointer. Near-missed on
            # ibx-5153's `carol-s-our-ai-story-narrative-prose` (4 documents),
            # caught by a hand-written verification query, not by this output.
            if len(titles) > 1:
                out.append(f"    sources ({len(titles)}):")
                for t in titles:
                    out.append(f"      · {t}")
            else:
                out.append(f"    source: {titles[0] if titles else '(no title)'}")
            for tid, tname, sound in s.targets:
                flag = "" if sound else "  ⚠ UNLAYERED TARGET"
                out.append(f"    → serves: {tname}{flag}")
                out.append(f"        {tid}")
            if s.postdates_target:
                out.append(
                    f"    ⚠ DOC ARRIVED AFTER the work it serves "
                    f"(ingested {s._doc_date} > {s._target_date}) — it cannot "
                    "have fed it. Check where it actually belongs before "
                    "migrating, or the target claims provenance it never had")
            if s.looks_foundational and not s.postdates_target:
                out.append(
                    f"    ℹ LOOKS FOUNDATIONAL — ingested {s._doc_date}, "
                    f"before this project's first work ({s._first_work_date}), "
                    "yet routed to one dated session. Founding material "
                    "usually belongs on the standing Brief. Is `Inputs & "
                    "Briefing` its home?")
            if s.has_edges:
                out.append("    ⚠ has typed edges — retiring cascades them; "
                           "check what points here first")
        out.append("")

        stale = [s for s in attachable if s.postdates_target]
        if stale:
            out.append(
                f"  ⚠ {len(stale)} of these are NEWER than the work they "
                "serve. On ibx-5192 that was 16 of 16 on one card — a bulk "
                "route, not curation. Migrating them faithfully preserves the "
                "mistake in a more permanent form.")
            out.append("")

        blocked = [s for s in attachable if s.unsound_targets]
        if blocked:
            out.append(
                f"  ⚠ {len(blocked)} of these route to an UNLAYERED card — "
                "one spine-lint already flags as unfilable. Moving real "
                "provenance onto a broken card buries it. Fix the "
                "destination's layer first (#177), then migrate.")
            out.append("")

    if orphans:
        out.append(
            f"{len(orphans)} stub(s) routed to a bare estimate slot — nothing "
            "to attach to, so nothing is proposed:")
        out.append("")
        for s in orphans:
            where = (f"serves {', '.join(s.unresolved)}" if s.unresolved
                     else "serves nothing")
            out.append(f"  {s.framing} ({where})")
            out.append(f"    {s.est_item_id}")
        out.append("")
        carriers = [s for s in orphans if s.carries_a_work_item_link]
        freed = [s for s in carriers if s.link_has_a_home]
        if freed:
            # The self-clearing half (#179 option 3). These are the only
            # carriers this sweep may propose moving: the work item they point
            # at has GAINED a bound element, so the document has a real home
            # and the card is redundant rather than load-bearing.
            out.append(
                f"  ✅ {len(freed)} carrier(s) are now FREE — the work item "
                "they point at has since gained a bound element, so the "
                "document can move there and the card can be retired:")
            out.append("")
            for s in freed:
                out.append(f"    {s.framing}")
                out.append(f"      {s.est_item_id}")
                out.append(f"      → re-route its source(s) to: {s._link_home}")
            out.append("")
        held = [s for s in carriers if not s.link_has_a_home]
        if held:
            out.append(
                f"  ℹ {len(held)} of these carry a WORK-ITEM LINK and are "
                "doing their job (#179). A work item — an estimate "
                "deliverable or activity, an initiative milestone — has no "
                "spine row and so no `sources` array; routing a document "
                "there mints a card to hold the pointer, deliberately. They "
                "look identical to a mistaken Source-material stub and are "
                "not one. Retiring them would drop the link with nothing to "
                "catch it. They become movable on their own once the work "
                "item gains a bound element — this sweep will say so.")
            out.append("")
        out.append(
            "  The rest need a judgement the data cannot make: either the "
            "slot should be a real element, or the document belongs on a card "
            "that already exists. Inventing a target would be a guess.")
        out.append("")

    # Cards and source POINTERS are different counts whenever a card wraps
    # several documents, and the difference is what a transfer list gets built
    # from (#276). Reported only when they diverge, so the common case stays
    # one number.
    pointers = sum(
        len([x for x in st.sources if isinstance(x, dict)]) for st in stubs
    )
    out.append(
        f"{len(stubs)} empty Source-material card(s)"
        + (f" · {pointers} source pointers" if pointers != len(stubs) else "")
        + f" · {len(attachable)} attachable · {len(orphans)} orphaned"
        + (f" · {len(edged)} carry typed edges" if edged else ""))
    out.append(
        "Read-only. Move a source with `add_element_source` on `cp-hosted`, "
        "then retire the stub — the card is a wrapper around a pointer it "
        "already duplicates, but its `serves` routing is real, so transfer "
        "before you retire.")
    return "\n".join(out)
