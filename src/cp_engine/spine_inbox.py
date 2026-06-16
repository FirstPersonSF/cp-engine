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

import json
import re
from dataclasses import dataclass
from typing import Callable

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


# ---- Task 3.2: build a proposed card from a transcript ----------------------


_PROPOSE_PROMPT = """\
You are distilling a project meeting into a faithful first-pass record for a
human to react to. Do NOT interpret, editorialize, or impose a framing yet —
just capture what was actually said and decided, densely and accurately.

You are also given a list of the project's estimate work items. Pick the ONE
whose name best matches what this meeting was primarily about, or null if none
clearly fit.

Estimate work items (name — kind):
{item_list}

Respond with ONLY a JSON object, no fences, no prose:
{{"distillation": "<a faithful 150-350 word distillation of the meeting>",
  "matched_item_name": "<exact name from the list above, or null>"}}

# Transcript
{transcript}
"""


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip().lower()


def _match_item(matched_name, estimate):
    """Resolve an LLM-picked item name to its (id, kind), or (None, None).

    Case-insensitive, whitespace-collapsed exact match against the estimate's
    item names — a coarse heuristic, intentional. Returns the first match.
    """
    if estimate is None or not matched_name:
        return None, None
    target = _normalize(str(matched_name))
    if not target:
        return None, None
    for it in estimate.all_items():
        if _normalize(it.name) == target:
            return it.id, it.kind
    return None, None


def _parse_distiller_json(raw: str) -> dict:
    """Parse the distiller's JSON, tolerating an accidental ```json fence."""
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("distiller did not return a JSON object")
    return obj


def build_inbox_card_from_transcript(
    client,
    *,
    project_id: str,
    project_code: str,
    source_ref: str,
    transcript: str,
    estimate=None,
    distiller: Callable[..., str],
    model: str,
    api_key: str | None = None,
) -> InboxCard:
    """Distill a transcript into a *proposed* ``spine_inbox`` card and upsert it.

    ONE LLM call (cost discipline): a raw-faithful first-pass distillation plus
    a best-guess estimate work item picked from the project's estimate item
    names. The matched name is resolved to ``guessed_est_item_id``; the type
    guess defaults to the matched item's kind, or ``"source"`` when nothing
    matched (a coarse guess the human refines at frame time).

    Writes ONLY to ``spine_inbox`` — never spine_substance. Returns the card.
    """
    items = estimate.all_items() if estimate is not None else ()
    item_list = (
        "\n".join(f"- {it.name} — {it.kind}" for it in items) or "(none)"
    )
    prompt = _PROPOSE_PROMPT.format(item_list=item_list, transcript=transcript)

    raw = distiller(prompt, model=model, api_key=api_key)
    obj = _parse_distiller_json(raw)
    distillation = str(obj.get("distillation") or "").strip()
    matched_id, matched_kind = _match_item(obj.get("matched_item_name"), estimate)
    guessed_type = matched_kind or "source"

    card = proposed_card(
        project_id=project_id,
        project_code=project_code,
        source_ref=source_ref,
        raw_distillation=distillation,
        guessed_est_item_id=matched_id,
        guessed_type=guessed_type,
    )
    client.table(_INBOX_TABLE).upsert(
        [card_to_row(card)], on_conflict="id"
    ).execute()
    return card
