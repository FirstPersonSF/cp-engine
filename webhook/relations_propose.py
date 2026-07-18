"""Propose a spine relationship edge at frame-promote (mig 117).

When a human promotes an inbox card into a NEW authored element, that element
is very often a *reaction to* an existing deliverable — the inbound email that
replies to what we shipped, the meeting that walks a client through a draft.
The distiller already guessed which estimate item the card is about
(``guessed_est_item_id``); at promote time BOTH endpoints finally have real,
stable est_item_ids, so we can propose the edge without any id hallucination.

This is the frame-promote arm of the relationship layer's auto-proposal (the
handoff's #6). It writes a ``responds_to`` PROPOSAL (``status='proposed'``,
``source='auto_ingest'``) into ``public.spine_relations`` — the review gate:
the human confirms it in the project's Suggestions inbox, exactly like a
commitment or a cross-project proposal.

Best-effort: any failure here is logged + swallowed. Proposing an edge must
never break a successful promote (the element is already durably in the repo).

Design: mc-2 Design-reference/Claude Design/design_handoff_relationship_layer/.
"""

from __future__ import annotations

import logging

from cp_engine.mc2_db import Tables

import observability

log = logging.getLogger("cp-engine-webhook")

# Element kinds that read as an inbound reaction to something we produced —
# the only cards worth proposing a `responds_to` edge FROM. A promoted
# deliverable/output is the thing being reacted TO, not the reactor, so those
# don't seed a proposal here (they'd be the target of someone else's edge).
_REACTION_KINDS = {"email", "feedback", "note", "source", "activity", "synthesis"}


def propose_responds_to_edge(
    client,
    *,
    project_id: str,
    project_code: str,
    new_est_item_id: str,
    guessed_est_item_id: str | None,
    kind: str | None,
) -> dict:
    """Propose ``new_est_item_id responds_to guessed_est_item_id``.

    Fires only when the promote created a DISTINCT new element (``new`` !=
    ``guessed``), the distiller matched a target the card is about, and the new
    element's kind reads as a reaction. Idempotent: the mig-117 unique
    constraint (project_id, kind, from, to) means a re-promote of the same card
    can't double-propose, and a re-proposal of an already-confirmed or
    -dismissed edge is a no-op (the existing row wins).

    Returns a summary dict for observability.
    """
    summary = {"proposed": 0, "reason": None}
    try:
        if not guessed_est_item_id:
            summary["reason"] = "no guessed target"
            return summary
        if guessed_est_item_id == new_est_item_id:
            summary["reason"] = "guess is the element itself"
            return summary
        if (kind or "").strip().lower() not in _REACTION_KINDS:
            summary["reason"] = f"kind {kind!r} is not a reaction"
            return summary

        # Don't propose a duplicate of an edge that already exists in ANY
        # lifecycle state (active/proposed/dismissed) — a dismissed guess must
        # stay dead, and an active edge needs no proposal.
        existing = (
            client.table(Tables.SPINE_RELATIONS)
            .select("id, status")
            .eq("project_id", project_id)
            .eq("kind", "responds_to")
            .eq("from_item_id", new_est_item_id)
            .eq("to_item_id", guessed_est_item_id)
            .execute()
            .data
            or []
        )
        if existing:
            summary["reason"] = f"edge already exists ({existing[0].get('status')})"
            return summary

        client.table(Tables.SPINE_RELATIONS).insert(
            {
                "project_id": project_id,
                "project_code": project_code,
                "kind": "responds_to",
                "from_item_id": new_est_item_id,
                "to_item_id": guessed_est_item_id,
                "note": (
                    "Promoted from an inbox card the distiller matched to this "
                    "item — reads as a reaction to it."
                ),
                "source": "auto_ingest",
                "status": "proposed",
                "confidence": 0.7,
                "created_by": "Auto-ingest",
            }
        ).execute()
        summary["proposed"] = 1
        log.info(
            "spine-relations: proposed %s responds_to %s for %s",
            new_est_item_id,
            guessed_est_item_id,
            project_code,
        )
    except Exception as exc:  # noqa: BLE001 — never fail a successful promote
        log.warning(
            "spine-relations: edge proposal failed for %s (%s -> %s): %s",
            project_code,
            new_est_item_id,
            guessed_est_item_id,
            exc,
        )
        observability.capture(exc, area="spine_relations_propose")
        summary["reason"] = f"error: {exc}"
    return summary
