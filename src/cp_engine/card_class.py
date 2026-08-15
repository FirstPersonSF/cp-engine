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
    ATTACHMENT = "attachment"

    @property
    def is_card(self) -> bool:
        return self is not CardKind.ATTACHMENT


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
        # Context never occupies a slot, so it is never a card — including the
        # Activity-layer write-ups that the layer rule would have promoted.
        return CardKind.ATTACHMENT

    # No placement recorded (pre-mig-070 rows, or a caller passing a partial
    # dict). Fall back to the original layer rule.
    if layer in {_norm(x) for x in _ACTIVITY_LAYERS}:
        return CardKind.ACTIVITY
    return CardKind.ATTACHMENT


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
    if classify(row) is not CardKind.ATTACHMENT:
        return False
    return _norm(row.get("layer")) in {_norm(x) for x in ENGAGEMENT_LEVEL_LAYERS}


def is_card(row: dict) -> bool:
    """Shorthand: does this element get a card?"""
    return classify(row).is_card
