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
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Callable

from cp_engine.authored_element import (
    authored_est_item_id,
    build_create_rows,
    canon_layer,
    slugify,
)
from cp_engine.authored_mirror import write_authored_element
from cp_engine.mc2_db import Tables
from cp_engine.substance import (
    SubstanceVersion,
    WorkItemSubstance,
    add_version,
    is_skipped_spine_dir,
    parse_substance,
    render_substance,
)

_INBOX_TABLE = Tables.SPINE_INBOX
_SUBSTANCE_TABLE = Tables.SPINE_SUBSTANCE

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


def load_card(client, card_id: str) -> InboxCard | None:
    """Load one inbox card by id, or None if it doesn't exist. Explicit columns."""
    rows = (
        client.table(_INBOX_TABLE)
        .select(_CARD_COLUMNS)
        .eq("id", card_id)
        .execute()
        .data
        or []
    )
    return row_to_card(rows[0]) if rows else None


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
    # A meeting re-homed to THIS project must not keep offering itself for
    # framing under its old job — retire the actionable cards it left behind.
    # (Card ids embed the project, so re-ingest alone never touches them.)
    retire_stale_cards(client, source_ref=source_ref, keep_project_id=project_id)
    return card


def retire_stale_cards(client, *, source_ref: str, keep_project_id: str) -> int:
    """Dismiss actionable (proposed|framed) cards for ``source_ref`` in every
    project EXCEPT ``keep_project_id``.

    The re-route path: a meeting tagged to the wrong job leaves a card in that
    job's Frame & promote inbox; when it re-ingests under the right job, the
    stale cards auto-dismiss. Promoted cards are untouched — a human confirmed
    that substance, so retiring it is a human call (dismiss it in the UI).
    Returns the number of cards dismissed.
    """
    rows = (
        client.table(_INBOX_TABLE)
        .select("id, project_id, status")
        .eq("source_ref", source_ref)
        .neq("project_id", keep_project_id)
        .in_("status", ["proposed", "framed"])
        .execute()
        .data
        or []
    )
    for r in rows:
        client.table(_INBOX_TABLE).update({"status": "dismissed"}).eq(
            "id", r["id"]
        ).execute()
    return len(rows)


# ---- Task 3.3: frame + promote (directed re-distill → version) --------------


_FRAME_PROMPT = """\
You are distilling project material into the single buildable memory for ONE
work item, UNDER a human's explicit framing brief. Stay faithful to the source
material — do not invent — but organize and emphasize it to serve the framing.
Write 200-450 words of dense, buildable prose (no preamble, no headers, no
meta-commentary). Output ONLY the distilled body.

## Framing brief (the human's directing intent)
{framing}

## Raw material
{raw}
"""


def _slugify(text: str) -> str:
    """Lowercase, hyphenated, ascii-ish slug for a phase/item file path."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "item"


def _iter_substance_files(spine_root: Path):
    """Yield every substance ``.md`` under ``spine/<phase>/`` (skips ``_context``,
    ``_authored``, and ``*.snapshots`` dirs), same scoping as the Phase-2 mirror
    via the shared `is_skipped_spine_dir` predicate. ``_authored/`` files are a
    generated DB→disk mirror of MC-2-owned rows and must NEVER be treated as a
    real disk substance candidate (e.g. by the promote path)."""
    if not spine_root.is_dir():
        return
    for md in sorted(spine_root.glob("*/*.md")):
        parts = md.relative_to(spine_root).parts
        if is_skipped_spine_dir(parts):
            continue
        yield md


def _target_path(spine_root: Path, *, phase, name, est_item_id) -> Path:
    """The substance file path for an item: ``spine/<phase-slug>/<item-slug>.md``.

    Slugs derive from the human-readable phase/name when given (so files stay
    legible, matching the existing tree), falling back to the stable est_item_id
    when not — the file PATH need only be stable; the mirror keys on est_item_id.
    """
    phase_slug = _slugify(phase) if phase else "unbound"
    item_slug = _slugify(name) if name else _slugify(est_item_id)
    return spine_root / phase_slug / f"{item_slug}.md"


def promote_card(
    card: InboxCard,
    *,
    framing: str,
    est_item_id: str,
    kind: str,
    project_dir: Path,
    sources,
    distiller: Callable[..., str],
    model: str,
    client=None,
    name: str | None = None,
    phase: str | None = None,
    today=None,
    flip_card: bool = True,
) -> Path:
    """Frame + promote a proposed card into a directed-distilled live version.

    Runs a DIRECTED distillation: the prompt carries the human ``framing`` brief
    + the card's raw material and asks for a faithful distillation UNDER that
    framing. The result becomes a new ``live`` `SubstanceVersion` (v1 if the
    item has no file yet, else next v-number with the prior live demoted to
    ``superseded`` via `add_version`). Writes the substance markdown at
    ``spine/<phase-slug>/<item-slug>.md`` with frontmatter binding
    ``est_item_id``/``est_item_kind``/``phase``. If ``client`` is given AND
    ``flip_card`` is true, the card's ``spine_inbox`` status flips to
    ``promoted`` (the webhook passes ``flip_card=False`` to defer the flip
    until after a successful git push). Returns the written Path.

    TARGETING (one substance file per ``est_item_id`` within a project): the
    binding key is ``est_item_id``, not the slug-derived path. So we FIRST scan
    for an existing substance file already bound to ``est_item_id`` and, if one
    exists, target *that file* — regardless of what slug the card's name would
    derive. Only when NO file binds the id yet do we derive a fresh
    ``_target_path`` and create v1. If MORE THAN ONE existing file binds the
    same id (a pre-existing corrupt state), raise ValueError — that's the only
    genuinely-ambiguous case left.

    CREATE-DON'T-VERSION on source divergence (issue #44): versioning semantics
    (v_{n+1} supersedes v_n) are only correct when the new body is an UPDATE OF
    THE SAME ARTIFACT. When the targeted file already exists and BOTH its live
    version's ``sources`` and the incoming ``sources`` are non-empty but
    DIFFERENT sets, this promotion is a *different artifact serving the same
    work item* (e.g. a second interview distilled against a container item like
    "1:1 stakeholder interviews") — appending a version would silently
    supersede, and thereby hide, the prior artifact. Instead we create a NEW
    authored element (``est_item_id = _authored/<slugified framing>``, slug
    collisions suffixed ``-2``, ``-3``, …) with ``serves=[est_item_id]`` binding
    it to the work item, upsert its v1-live rows into ``spine_substance``
    (origin='authored', exactly what the shared create path emits), and mirror
    it to ``spine/_authored/<slug>.md``. The bound work-item card is left
    untouched. Same-source re-promotes (a true re-distill) and promotes where
    either side has no sources keep versioning exactly as before. The create
    path requires ``client`` (authored elements are MC-2-owned rows; a file
    alone under ``_authored/`` would never sync) and raises ValueError without
    one.
    """
    today_iso = today if isinstance(today, str) else (today or date.today()).isoformat()
    spine_root = project_dir / "spine"

    # Route by binding key, not by slug: find any existing file bound to this id.
    bound = []
    for md in _iter_substance_files(spine_root):
        try:
            other = parse_substance(md)
        except Exception:
            continue
        if other.est_item_id == est_item_id:
            bound.append(md)

    if len(bound) > 1:
        raise ValueError(
            f"est_item_id {est_item_id!r} is bound by {len(bound)} substance "
            f"files ({', '.join(str(p) for p in bound)}); the one-file-per-item "
            f"invariant is already violated on disk. Refusing to promote into an "
            f"ambiguous binding — reconcile the duplicate files first."
        )

    target = (
        bound[0]
        if bound
        else _target_path(spine_root, phase=phase, name=name, est_item_id=est_item_id)
    )

    body = distiller(
        _FRAME_PROMPT.format(framing=framing, raw=card.raw_distillation),
        model=model,
        api_key=None,
    ).strip()
    sources_tuple = tuple(str(s) for s in (sources or []))

    if target.exists():
        existing = parse_substance(target)
        prior_sources = set(existing.live_version().sources)
        incoming_sources = set(sources_tuple)
        if prior_sources and incoming_sources and prior_sources != incoming_sources:
            # A different artifact serving the same work item (issue #44):
            # create a new authored element instead of superseding the card.
            return _promote_as_new_serving_element(
                card,
                framing=framing,
                work_item_est_id=est_item_id,
                kind=kind,
                body=body,
                sources=sources_tuple,
                project_dir=project_dir,
                client=client,
                flip_card=flip_card,
                today_iso=today_iso,
            )
        n = max(int(v.label[1:]) for v in existing.versions) + 1
        version = SubstanceVersion(
            label=f"v{n}", date=today_iso, status="live",
            framing=framing, sources=sources_tuple, body=body,
        )
        item = add_version(existing, version)
        # add_version preserves the existing item's binding/phase/extra; only
        # versions change. (phase/kind stay as the file already declared them.)
        # One repair: files promoted before layer stamping existed carry no
        # layer (mirrors to a NULL row the UI can't file) — stamp on re-promote.
        if item.layer is None:
            item = replace(item, layer=canon_layer(kind))
    else:
        version = SubstanceVersion(
            label="v1", date=today_iso, status="live",
            framing=framing, sources=sources_tuple, body=body,
        )
        item = WorkItemSubstance(
            est_item_id=est_item_id, est_item_kind=kind, phase=phase,
            binding="live", layer=canon_layer(kind),
            versions=(version,), path=target,
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_substance(item))

    if client is not None and flip_card:
        client.table(_INBOX_TABLE).update({"status": "promoted"}).eq(
            "id", card.id
        ).execute()

    return target


def _authored_slug_taken(client, card: InboxCard, spine_root: Path, slug: str) -> bool:
    """True if ``_authored/<slug>`` already exists for this project — as a
    mirror file on disk OR as spine_substance rows in MC-2 (scoped by
    project_id, the stable uuid, not the mutable project_code)."""
    if (spine_root / "_authored" / f"{slug}.md").exists():
        return True
    rows = (
        client.table(_SUBSTANCE_TABLE)
        .select("id")
        .eq("project_id", card.project_id)
        .eq("est_item_id", authored_est_item_id(slug))
        .limit(1)
        .execute()
        .data
        or []
    )
    return bool(rows)


def _promote_as_new_serving_element(
    card: InboxCard,
    *,
    framing: str,
    work_item_est_id: str,
    kind: str,
    body: str,
    sources: tuple[str, ...],
    project_dir: Path,
    client,
    flip_card: bool,
    today_iso: str,
) -> Path:
    """Create a NEW authored element serving ``work_item_est_id`` (issue #44).

    The promote's sources diverged from the bound card's live sources, so this
    distillation is a distinct artifact that must not supersede the card. The
    element takes the authored identity ``_authored/<slugified framing>``
    (framing is the human title, e.g. "Interview with Paul Wu"; collisions
    suffix ``-2``, ``-3``, …) and ``serves=[work_item_est_id]`` — the same rows
    the shared create path (`build_create_rows` → MCP `create_spine_element`)
    emits: binding='live' (serves non-empty), placement='context', layer from
    ``kind``, origin='authored', v1 live.

    Rows go to ``spine_substance`` FIRST (authored elements are MC-2-owned; the
    disk file is only a generated mirror), then the mirror file is written at
    ``spine/_authored/<slug>.md`` via `write_authored_element` — byte-identical
    to what `sync_spine_substance`'s authored reverse-mirror regenerates, so
    the file never fights sync: the disk→DB readers skip ``_authored/``, the
    reap skips origin='authored', and the reverse-mirror rewrites this same
    path. Returns the written mirror Path (live version is v1).
    """
    if client is None:
        raise ValueError(
            "promote with divergent sources must CREATE a new authored element "
            f"serving {work_item_est_id!r}, which requires an MC-2 client "
            "(authored elements are DB-owned; a mirror file alone would never "
            "sync). Pass client=..., or promote with the same sources as the "
            "live version to re-distill it."
        )

    spine_root = project_dir / "spine"
    base_slug = slugify(framing)
    slug, n = base_slug, 1
    while _authored_slug_taken(client, card, spine_root, slug):
        n += 1
        slug = f"{base_slug}-{n}"
    est_id = authored_est_item_id(slug)

    rows = build_create_rows(
        project_id=card.project_id,
        project_code=card.project_code,
        label=framing,
        type_=kind,
        body=body,
        serves=[work_item_est_id],
        now_iso=today_iso,
        sources=list(sources),
    )
    # build_create_rows derives the slug from the label; re-key to the
    # (possibly collision-suffixed) slug chosen above.
    for r in rows:
        r["est_item_id"] = est_id
        r["id"] = f"{card.project_code}/{est_id}/{r['version_label']}"

    client.table(_SUBSTANCE_TABLE).upsert(rows, on_conflict="id").execute()
    path = write_authored_element(
        project_dir, project_code=card.project_code,
        est_item_id=est_id, rows=rows,
    )

    if flip_card:
        client.table(_INBOX_TABLE).update({"status": "promoted"}).eq(
            "id", card.id
        ).execute()

    return path
