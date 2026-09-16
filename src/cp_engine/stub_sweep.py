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

    edged: set[str] = set()
    for e in relations:
        for side in ("from_item_id", "to_item_id"):
            if e.get(side):
                edged.add(e[side])

    out: list[Stub] = []
    for row in rows:
        eid = row.get("est_item_id")
        if not eid or not _is_stream(row):
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
                _doc_date=_earliest_source_date(
                    row.get("sources") or [], source_dates
                ),
                _target_date=target_date,
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
            titles = ", ".join(
                str(src.get("title") or src.get("id"))
                for src in s.sources if isinstance(src, dict)) or "(no title)"
            out.append(f"  {s.framing}  ({s.body_len} chars)")
            out.append(f"    {s.est_item_id}")
            out.append(f"    source: {titles}")
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
        out.append(
            "  These need a judgement the data cannot make: either the slot "
            "should be a real element, or the document belongs on a card that "
            "already exists. Inventing a target would be a guess.")
        out.append("")

    out.append(
        f"{len(stubs)} empty Source-material card(s) · {len(attachable)} "
        f"attachable · {len(orphans)} orphaned"
        + (f" · {len(edged)} carry typed edges" if edged else ""))
    out.append(
        "Read-only. Move a source with `add_element_source` on `cp-hosted`, "
        "then retire the stub — the card is a wrapper around a pointer it "
        "already duplicates, but its `serves` routing is real, so transfer "
        "before you retire.")
    return "\n".join(out)
