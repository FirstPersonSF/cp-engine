"""Spine ingestion inbox (spine estimate-binding, Phase 3).

The webhook no longer writes spine *substance* directly from a transcript.
Instead it writes a *proposed card* into the ``spine_inbox`` table: a
raw-faithful first-pass distillation of the meeting + a best-guess estimate
work item + a guessed type. A human then *frames* the card — supplies a
directing brief — and *promotes* it: a directed re-distillation under that
framing becomes a new live `SubstanceVersion` bound to an estimate work item.

This is the "human directs / LLM distills / human confirms" loop. The card is
the react-to draft; the framed promotion is the distilled spine memory.

Live table ``public.spine_inbox`` (migration 064):
  id text pk = ``<project_code>/inbox/<source_ref>``
  project_id uuid, project_code text, source_ref text,
  raw_distillation text, guessed_est_item_id text, guessed_type text,
  status text (proposed|framed|promoted|dismissed) default proposed,
  framing text, created_at, updated_at.

GLOBAL RULE: never ``.select("*")`` — always explicit columns.
"""

from __future__ import annotations

from dataclasses import dataclass

_INBOX_TABLE = "spine_inbox"

# Columns we read back from spine_inbox (never "*").
_CARD_COLUMNS = (
    "id, project_id, project_code, source_ref, raw_distillation, "
    "guessed_est_item_id, guessed_type, status, framing"
)


@dataclass(frozen=True)
class InboxCard:
    """One proposed/framed/promoted ingestion card."""

    id: str
    project_id: str
    project_code: str
    source_ref: str
    raw_distillation: str
    guessed_est_item_id: str | None
    guessed_type: str | None
    status: str
    framing: str | None


def proposed_card(
    *,
    project_id: str,
    project_code: str,
    source_ref: str,
    raw_distillation: str,
    guessed_est_item_id: str | None = None,
    guessed_type: str | None = None,
) -> InboxCard:
    """Build a fresh ``proposed`` card. id = ``<project_code>/inbox/<source_ref>``."""
    return InboxCard(
        id=f"{project_code}/inbox/{source_ref}",
        project_id=project_id,
        project_code=project_code,
        source_ref=source_ref,
        raw_distillation=raw_distillation,
        guessed_est_item_id=guessed_est_item_id,
        guessed_type=guessed_type,
        status="proposed",
        framing=None,
    )


def card_to_row(card: InboxCard) -> dict:
    """Map a card to a ``spine_inbox`` row (created_at/updated_at left to the DB)."""
    return {
        "id": card.id,
        "project_id": card.project_id,
        "project_code": card.project_code,
        "source_ref": card.source_ref,
        "raw_distillation": card.raw_distillation,
        "guessed_est_item_id": card.guessed_est_item_id,
        "guessed_type": card.guessed_type,
        "status": card.status,
        "framing": card.framing,
    }


def row_to_card(row: dict) -> InboxCard:
    """Map a ``spine_inbox`` row to an InboxCard (extra columns are ignored)."""
    return InboxCard(
        id=row["id"],
        project_id=row["project_id"],
        project_code=row["project_code"],
        source_ref=row["source_ref"],
        raw_distillation=row.get("raw_distillation") or "",
        guessed_est_item_id=row.get("guessed_est_item_id"),
        guessed_type=row.get("guessed_type"),
        status=row.get("status") or "proposed",
        framing=row.get("framing"),
    )
