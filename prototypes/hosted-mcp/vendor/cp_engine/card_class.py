"""What is a CARD, and what merely attaches to one (#179, step 1).

Drew, 2026-08-11:

    Cards should be reserved for major activities or deliverables, and all of
    the other things should be attached to them. Collecting email feedback is
    really valuable, but it doesn't need a card — it informs a card.

Measured on the live tenant the day this was written: **39 cards, 230
attachments** out of 269 peer elements.

WHY THIS MODULE EXISTS
----------------------
"Is this work?" was being decided in at least five places, each with its own
hand-maintained list of layer strings:

    spine_lint.py:137,294       ("deliverables", "output")
    seal_sweep.py               DELIVERABLE_LAYERS
    stub_sweep.py               SOURCE_LAYERS
    spine.py                    FRAMING_LAYERS
    mc-2 roles.ts               CARD_ROLES / LINE_ROLES / PROJECT_CONTEXT_LAYERS

That duplication is not cosmetic — it is what broke in #172 (`Output` was an
alias nobody folded into `Deliverables`, so `spine_stats` reported 0 of
sap-5174's 2 deliverables) and again in #178 (work items and context elements
BOTH carry a layer, so layer alone cannot tell them apart). Every future check
should import from here instead of growing a sixth list.

THE MODEL
---------
Three card kinds, and everything else attaches:

    ENGAGEMENT   the project itself — one per project. Holds the material that
                 belongs to no single piece of work: briefs, agreements, SOWs,
                 retrospectives, syntheses about the whole engagement.
    ACTIVITY     a scoped piece of work with a beginning and an end. An
                 interview PROGRAMME is one; a weekly team call is not.
    DELIVERABLE  a thing we hand over, which accrues versions.

Everything else — sources, feedback, emails, notes, meeting records, decisions
awaiting ratification — is an ATTACHMENT. It keeps its substance; it stops
competing for attention with the work it informs.

    LINK         an attachment-shaped card minted ONLY because a work item has
                 nowhere to hold a source. Not content and not a mistake; the
                 one kind the stub sweep must not simply retire (#179 opt 3).

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
It does not guess. `classify()` reads an explicit `card_kind` when the row
carries one, and only falls back to layer inference for rows written before
that field existed. The fallback is a MIGRATION AID with a known end date, not
the model — see `classify_is_inferred`. Two layers are known to straddle and
the fallback cannot resolve them:

  - `Email` holds both scheduling notes and real client feedback ("Janet
    Feedback on r3 Decks"). Transport, not class.
  - `Activity` holds both genuine activities and 14 interview WRITE-UPS on
    sap-5174, which are contents of an activity, not activities.

Rows on those layers come back inferred, so a caller can choose to ask rather
than assume.
"""

from __future__ import annotations

import re
from enum import Enum

# --- the vocabulary ---------------------------------------------------------


class CardKind(str, Enum):
    """A card is a unit of work. Everything else attaches to one."""

    ENGAGEMENT = "engagement"
    ACTIVITY = "activity"
    DELIVERABLE = "deliverable"
    # Authored, durable, deliberately unbound — a Stakeholder dossier, a
    # Retrospective, a synthesis nobody has routed yet. NOT a defect (#179).
    REFERENCE = "reference"
    # Capture. The ingest's own wrapper around a rag_asset it already points
    # at: body under 200 chars, no thought in it.
    ATTACHMENT = "attachment"
    # A card minted to carry a pointer for a WORK ITEM (#179 option 3).
    #
    # Byte-identical to ATTACHMENT in body and shape, and NOT the same thing.
    # A work item — an estimate deliverable or activity, an initiative
    # milestone — has no `spine_substance` row and therefore no `sources`
    # array, so routing a document there mints a card to hold the link,
    # deliberately. That card is doing a job nothing else can do; an
    # ATTACHMENT is a document that should have been attached to an element
    # that already existed.
    #
    # Stored rather than derived. `stub_sweep.carries_a_work_item_link`
    # answers the same question from `serves` + `sources`, but that derivation
    # cannot tell "minted for a work item" from "minted for a slot somebody
    # later deleted", and a heuristic of exactly that family already misfired
    # on live data (#269, the `_authored/sow` placeholders the sweep proposed
    # retiring). The mint knows which it is at the moment it writes the row;
    # this records the answer instead of reconstructing it.
    LINK = "link"

    @property
    def is_card(self) -> bool:
        """A unit of WORK. Reference is not work, but it is not capture either.

        #179's three classes are Work (engagement/activity/deliverable),
        Reference, and Stream (attachment). `is_card` answers the Work
        question, so Reference is False here — see `is_stream` for the
        distinction the sweeps actually need.
        """
        return self in (
            CardKind.ENGAGEMENT, CardKind.ACTIVITY, CardKind.DELIVERABLE,
        )

    @property
    def is_stream(self) -> bool:
        """Capture, not content. The only class `stub_sweep` should touch.

        LINK is deliberately NOT stream. It has a stream-shaped body, but
        retiring it drops a pointer with nothing to catch it — the sweep's
        proposals are the whole reason to tell the two apart. `stub_sweep`
        has its own narrower rule for when a link-carrier becomes retirable
        (its work item gained a bound element, so the pointer has a home).
        """
        return self is CardKind.ATTACHMENT

    @property
    def is_link_carrier(self) -> bool:
        """Minted to hold a pointer a work item could not hold (#179)."""
        return self is CardKind.LINK


# The standing element that IS the engagement. Present on every project with a
# real spine (verified 2026-08-11: ibx-5153, ibx-5192, sap-5171, sap-5174,
# mission-control — one each), and already the anchor canon hangs off under
# spec v04 §2. This promotes an element that already exists; it invents nothing.
ENGAGEMENT_IDS: frozenset[str] = frozenset({"_authored/inputs-briefing"})

# Layers whose material belongs to the ENGAGEMENT rather than to any single
# activity or deliverable — briefs, agreements, SOWs, retrospectives. Mirrors
# spine.FRAMING_LAYERS and roles.ts's PROJECT_CONTEXT_LAYERS, which
# independently arrived at the same set.
#
# THESE ARE ATTACHMENTS, NOT CARDS. A project has ONE engagement; these hang
# off it. Classifying them as engagement CARDS produced 26 engagement cards
# across 5 projects on the first live run (13 Brief + 11 Agreement + 2
# Retrospective) — which is the accumulation this whole effort exists to stop,
# reintroduced one layer up. `engagement_attaches_here` is the useful fact
# about them, and it is not the same as being a card.
ENGAGEMENT_LEVEL_LAYERS: frozenset[str] = frozenset(
    {"brief", "agreement", "timeline", "retrospective"}
)

# `Output` is a pre-#172 alias for Deliverables. mig 137 folded the 29 rows
# that carried it, but historical rows and un-migrated tenants still can.
_DELIVERABLE_LAYERS: frozenset[str] = frozenset({"deliverables", "output", "drafts"})

_ACTIVITY_LAYERS: frozenset[str] = frozenset({"activity", "activities"})

# Layers that STRADDLE — inference cannot resolve them, so callers are told.
AMBIGUOUS_LAYERS: frozenset[str] = frozenset({"email", "activity", "activities"})


def _norm(value) -> str:
    """Layer values drift between CamelCase (`SourceMaterial`, the code
    vocabulary) and spaced Title Case (`Source material`, the live DB shape).
    Strip to letters so both compare equal — same normalisation spine_lint
    settled on."""
    return re.sub(r"[^a-z]", "", str(value or "").lower())


# A capture card is the ingest's wrapper around a document, not a thought.
# Two signals, and they agreed on all 374 live context rows (2026-09-15):
#
#   * body under 200 chars — every Stream body measured 93–175, every
#     Reference body 98–96,889 with a median in the thousands; and
#   * it points at the asset it wraps — either a `sources` entry (126 of 129)
#     or a `rag_asset:` line in the body itself (the 3 without sources).
#
# Requiring the length AND one pointer is what keeps a short authored note out
# of Stream: a 150-char thought with no rag_asset stays Reference. Measured:
# zero false positives in either direction.
_CAPTURE_BODY_MAX = 200


# A STANDING ELEMENT that nobody has filled in yet — the SOW slot, the
# inputs-briefing slot — is scaffolding the tenant creates deliberately. It is
# short and it carries a source, so `_is_capture`'s two signals both fire on
# it, and before this check it classified as Stream. That is wrong in a way
# that COSTS something: `stub_sweep` proposes retiring Stream, and these slots
# exist precisely to persist until somebody writes into them.
#
# Measured 2026-09-15 across all 20 live rows carrying the marker: 16 unfilled
# (every one ≤ 378 chars AND carrying the instructional line) and 4 filled
# (every one ≥ 550 chars, none carrying it). The two signals agree on all 20,
# so both are required — the marker alone would swallow the filled SOW at
# 2,864 chars and the working brief at 5,491.
_PLACEHOLDER_MARKER = "_Standing element"
_PLACEHOLDER_INSTRUCTIONS = "What belongs here:"


def is_unfilled_placeholder(row: dict) -> bool:
    """True when this row is a standing-element slot awaiting its content.

    Not capture and not authored content — a third thing. Kept public because
    the sweeps need to tell it apart from a stub: both are short, but a stub
    should be retired and a placeholder must not be.
    """
    body = str(row.get("body") or "")
    return (_PLACEHOLDER_MARKER in body
            and _PLACEHOLDER_INSTRUCTIONS in body)


def _is_capture(row: dict) -> bool:
    """True when this context row is the ingest's wrapper, not authored content."""
    body = str(row.get("body") or "")
    if len(body) >= _CAPTURE_BODY_MAX:
        return False
    # Asked BEFORE the source check: an unfilled slot is short and carries a
    # source, so it would otherwise read as capture.
    if is_unfilled_placeholder(row):
        return False
    sources = row.get("sources")
    if isinstance(sources, (list, tuple)) and len(sources) > 0:
        return True
    return "rag_asset:" in body


def classify(row: dict) -> CardKind:
    """This element's card kind.

    Prefers an explicit `card_kind` on the row. Falls back to layer inference
    for rows written before the field existed — see `classify_is_inferred` to
    tell the two apart.
    """
    explicit = str(row.get("card_kind") or "").strip().lower()
    if explicit:
        try:
            return CardKind(explicit)
        except ValueError:
            # An unrecognised value is data drift, not a crash. Fall through to
            # inference rather than inventing a kind — the #172 lesson: a
            # vocabulary that can only validate against itself drifts silently.
            pass

    if (row.get("est_item_id") or "") in ENGAGEMENT_IDS:
        return CardKind.ENGAGEMENT

    layer = _norm(row.get("layer"))
    if layer in {_norm(x) for x in _DELIVERABLE_LAYERS}:
        return CardKind.DELIVERABLE

    # PLACEMENT resolves the Activity straddle that layer alone cannot.
    #
    # Measured 2026-08-15 on all 271 live rows: every `placement='item'` row is
    # a uuid-keyed estimate slot — "Kickoff meeting with the larger team",
    # "Post-Mehul debrief" — i.e. the work itself. Every sap-5174 interview
    # write-up ("Jeanne Dion Interview", "Michelle Craig Interview") is
    # `placement='context'` and `_authored/*`-keyed: the CONTENTS of an
    # activity, which is exactly the distinction this module's docstring says
    # the layer fallback could not make.
    #
    # So placement is asked FIRST, before the ambiguous-layer check: it is a
    # structural fact (derived from key shape, per `substance.derive_placement`)
    # rather than a topic tag, and structure beats topic.
    placement = str(row.get("placement") or "").strip().lower()
    if placement == "item":
        return CardKind.ACTIVITY
    if placement == "context":
        # Context never occupies a slot, so it is never WORK — including the
        # Activity-layer write-ups that the layer rule would have promoted.
        # But context is two things (#179): the ingest's own wrapper around a
        # document (Stream) and authored, durable thinking nobody has routed
        # (Reference). Measured 2026-09-15 across all 374 live context rows:
        # 129 Stream, 245 Reference, and the two independent signals below
        # agree on every one.
        return CardKind.ATTACHMENT if _is_capture(row) else CardKind.REFERENCE

    # No placement recorded (pre-mig-070 rows, or a caller passing a partial
    # dict). Fall back to the original layer rule.
    if layer in {_norm(x) for x in _ACTIVITY_LAYERS}:
        return CardKind.ACTIVITY
    return CardKind.ATTACHMENT if _is_capture(row) else CardKind.REFERENCE


def classify_is_inferred(row: dict) -> bool:
    """True when `classify()` guessed rather than read an explicit kind.

    Two reasons a caller cares: it can show the guess as provisional, and the
    migration can count how much of the tenant still needs a human decision.
    A row on a straddling layer (`Email`, `Activity`) is ALWAYS inferred even
    if the layer maps cleanly — the mapping is what cannot be trusted there.
    """
    explicit = str(row.get("card_kind") or "").strip().lower()
    if explicit:
        try:
            CardKind(explicit)
            return False
        except ValueError:
            return True
    return True


def is_ambiguous(row: dict) -> bool:
    """The row sits on a layer that holds more than one card kind.

    `Email` carries both scheduling notes and real client feedback; `Activity`
    carries both genuine activities and the 14 sap-5174 interview write-ups.
    Only meaningful while `card_kind` is unset — an explicit kind settles it.

    A recorded `placement` ALSO settles it: placement is structural (item =
    occupies an estimate slot, context = does not), so a row that carries one
    is no longer guessing about card-ness even on a straddling layer. Only a
    row with a straddling layer AND no placement is genuinely ambiguous.
    """
    if not classify_is_inferred(row):
        return False
    if str(row.get("placement") or "").strip().lower() in {"item", "context"}:
        return False
    return _norm(row.get("layer")) in {_norm(x) for x in AMBIGUOUS_LAYERS}


def attaches_to_engagement(row: dict) -> bool:
    """This attachment belongs to the PROJECT, not to any one activity.

    Briefs, agreements, SOWs and retrospectives are engagement-level material:
    they answer "what is this project" rather than "what fed this deliverable".
    They are attachments (a project has one engagement card, not thirteen), but
    they hang off the engagement rather than off a work card — which is what
    gives the 163 otherwise-homeless elements a home.
    """
    # #179 split the old single non-work class in two. Engagement-level
    # material is USUALLY Reference (a Brief, an Agreement, a Retrospective —
    # authored and substantial), but an ingested SOW wrapper is Stream and
    # hangs off the engagement just the same. The question here is "does this
    # belong to the engagement rather than to a work card", which is about the
    # LAYER, not about which of the two non-work classes it landed in.
    if classify(row).is_card:
        return False
    return _norm(row.get("layer")) in {_norm(x) for x in ENGAGEMENT_LEVEL_LAYERS}


def is_card(row: dict) -> bool:
    """Shorthand: does this element get a card?"""
    return classify(row).is_card
