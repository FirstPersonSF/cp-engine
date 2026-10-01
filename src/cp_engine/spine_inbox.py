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
from pathlib import Path
from typing import Callable

from cp_engine.clock import tenant_today
from cp_engine.authored_element import (
    authored_est_item_id,
    build_create_rows,
    canon_layer,
    slugify,
)
from cp_engine.distill_fidelity import assess, mark_machine_derived
from cp_engine.mc2_db import Tables
from cp_engine.substance import (
    SubstanceVersion,
    WorkItemSubstance,
    version_number,
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
    # #314 — the card is the raw material every later promote is judged
    # against, so a card that is not drawn from its transcript must be said
    # out loud here (the webhook log carries print, not logger — see
    # project_cp_engine_print_vs_logger). The card itself is a react-to
    # draft; the human reads it before framing.
    card_fidelity = assess(distillation, transcript)
    if card_fidelity["low"]:
        print(f"distill-fidelity: inbox card for {project_code} meeting "
              f"{source_ref} — {card_fidelity['reason']}")
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
    on_fidelity: Callable[[dict], None] | None = None,
    writer: str = "cxp spine-frame",
) -> Path:
    """Frame + promote a proposed card into a directed-distilled live version.

    Runs a DIRECTED distillation: the prompt carries the human ``framing`` brief
    + the card's raw material and asks for a faithful distillation UNDER that
    framing. The result becomes a new ``live`` version of the work item's
    distilled element: v1 if MC-2 holds none, else the next label after the
    highest MC-2 holds (any origin — #121), the prior live distilled version
    demoted to ``superseded``. If ``client`` is given AND ``flip_card`` is
    true, the card's ``spine_inbox`` status flips to ``promoted``. Returns the
    rendered file's Path.

    MC-2 FIRST (architecture step 4c). The version row is written to
    ``spine_substance`` (origin='distilled') and only then is the file under
    ``spine/`` rendered from MC-2 — the file is a generated view, never the
    source a later sync pushes up (that push is gone). So ``client`` is
    required. The element's existing rows decide its file path (``rel_path``),
    phase, binding and layer; a first version takes
    ``spine/<phase-slug>/<item-slug>.md``.

    CREATE-DON'T-VERSION on source divergence (issue #44): versioning semantics
    (v_{n+1} supersedes v_n) are only correct when the new body is an UPDATE OF
    THE SAME ARTIFACT. When the element's live version's ``sources`` and the
    incoming ``sources`` are both non-empty but DIFFERENT sets, this promotion
    is a *different artifact serving the same work item* — appending a version
    would silently supersede, and thereby hide, the prior artifact. Instead a
    NEW authored element (``_authored/<slugified framing>``, collisions
    suffixed ``-2``, ``-3``, …) is created with ``serves=[est_item_id]``.

    FIDELITY (#314): the distilled body is scored against the card's raw
    material (`distill_fidelity.assess`) and the result handed to
    ``on_fidelity`` BEFORE anything is written. A versioned body is written
    ``origin='distilled'``, which `distill_fidelity.provenance_of` reads as
    machine-derived until a human confirms it.
    """
    if client is None:
        raise ValueError(
            "promote_card writes the new version to MC-2 first (step 4c: MC-2 "
            "owns the spine; spine/ is rendered from it) — pass client=..."
        )
    today_iso = today if isinstance(today, str) else (today or tenant_today()).isoformat()

    existing = _element_rows(client, card.project_id, est_item_id)
    nums = [version_number(r.get("version_label")) for r in existing]
    db_max = max([n for n in nums if n >= 0], default=0)
    distilled = [r for r in existing if r.get("origin") != "authored"]
    live = next((r for r in distilled if r.get("status") == "live"), None)

    body = distiller(
        _FRAME_PROMPT.format(framing=framing, raw=card.raw_distillation),
        model=model,
        api_key=None,
    ).strip()
    sources_tuple = tuple(str(s) for s in (sources or []))
    fidelity = assess(body, card.raw_distillation)
    if on_fidelity is not None:
        on_fidelity(fidelity)

    prior_sources = {str(s) for s in ((live or {}).get("sources") or [])}
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
            fidelity=fidelity,
        )

    from cp_engine.mc2_db import canonical_spine_code
    from cp_engine.spine_mirror import distilled_path
    from cp_engine.spine_substance_sync import substance_to_rows

    code = canonical_spine_code(client, card.project_id, card.project_code)
    base = live or (max(distilled, key=lambda r: version_number(r.get("version_label")))
                    if distilled else None)
    if base is not None:
        target = distilled_path(project_dir, base)
        item_kind = base.get("est_item_kind") or kind
        item_phase = base.get("phase")
        binding = base.get("binding") or "live"
        # Rows promoted before layer stamping existed carry no layer (a NULL
        # row the UI can't file) — stamp on re-promote.
        layer = base.get("layer") or canon_layer(kind)
        placement = base.get("placement") or "item"
        serves = tuple(base.get("serves") or ())
        archived = bool(base.get("archived", False))
    else:
        target = _target_path(project_dir / "spine", phase=phase, name=name,
                              est_item_id=est_item_id)
        item_kind, item_phase, binding = kind, phase, "live"
        layer, placement, serves, archived = canon_layer(kind), "item", (), False

    version = SubstanceVersion(
        label=f"v{db_max + 1}", date=today_iso, status="live",
        framing=framing, sources=sources_tuple, body=body,
    )
    item = WorkItemSubstance(
        est_item_id=est_item_id, est_item_kind=item_kind, phase=item_phase,
        binding=binding, layer=layer, placement=placement, serves=serves,
        archived=archived, versions=(version,), path=target,
    )
    row = substance_to_rows(
        item, project_id=card.project_id, project_code=code,
        rel_path=str(target.relative_to(project_dir)),
    )[0]
    row["origin"] = "distilled"
    row["field_states"] = {}
    row["review_flags"] = []
    # New live row first, THEN demote: a failure between the two leaves two
    # live versions (loud — the render refuses it) rather than none.
    client.table(_SUBSTANCE_TABLE).upsert([row], on_conflict="id").execute()
    for r in distilled:
        if r.get("status") == "live" and r.get("id") != row["id"]:
            client.table(_SUBSTANCE_TABLE).update({"status": "superseded"}).eq(
                "id", r["id"]
            ).execute()

    path = _render_element_file(client, card.project_id, est_item_id,
                                project_dir=project_dir, writer=writer,
                                origin="distilled")

    if flip_card:
        client.table(_INBOX_TABLE).update({"status": "promoted"}).eq(
            "id", card.id
        ).execute()

    return path


_ELEMENT_COLUMNS = (
    "id, version_label, status, origin, sources, est_item_kind, phase, "
    "binding, layer, placement, serves, archived, rel_path"
)


def _element_rows(client, project_id: str, est_item_id: str) -> list[dict]:
    """Every MC-2 row of one element (all origins), by the stable project_id.

    A FAILED read raises: picking a version label without MC-2's answer is
    how a promote overwrites a version MC-2 already holds (#121)."""
    try:
        return (
            client.table(Tables.SPINE_SUBSTANCE)
            .select(_ELEMENT_COLUMNS)
            .eq("project_id", project_id)
            .eq("est_item_id", est_item_id)
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001 — re-raised with context
        raise RuntimeError(
            f"could not read MC-2 versions for {project_id}/{est_item_id} "
            f"({type(exc).__name__}: {exc}); refusing to pick a version label"
        ) from exc


def _render_element_file(client, project_id: str, est_item_id: str, *,
                         project_dir: Path, writer: str, origin: str) -> Path:
    """Render ONE element's file from its MC-2 rows through the generated-view
    guard (a hand edit in the file is quarantined before it is replaced), and
    record it in the directory's manifest. Returns the path."""
    from cp_engine.authored_mirror import render_element
    from cp_engine.spine_mirror import RENDER_SELECT, MirrorDir, element_path

    rows = (
        client.table(Tables.SPINE_SUBSTANCE)
        .select(RENDER_SELECT)
        .eq("project_id", project_id)
        .eq("est_item_id", est_item_id)
        .execute()
        .data
        or []
    )
    rows = [r for r in rows
            if (r.get("origin") == "authored") == (origin == "authored")]
    if not rows:
        raise RuntimeError(f"no MC-2 rows for {est_item_id} after the write")
    steps = (
        client.table(Tables.SPINE_STEPS)
        .select("est_item_id, position, title, status, step_date, note")
        .eq("project_id", project_id)
        .eq("est_item_id", est_item_id)
        .execute()
        .data
        or []
    )
    path = element_path(project_dir, rows)
    text = render_element(
        est_item_id=est_item_id, rows=rows, steps=steps,
        kind="context" if origin == "authored" else None, path=path,
    )
    mirror = MirrorDir(project_dir / "spine", writer=writer)
    mirror.write(path, text)
    mirror.save()
    return path


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
    fidelity: dict | None = None,
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

    Rows go to ``spine_substance`` FIRST (MC-2 owns every element; the disk
    file is only a generated view), then the file is rendered at
    ``spine/_authored/<slug>.md`` through the `spine_mirror` guard —
    byte-identical to what sync's render regenerates. Returns the rendered
    Path (live version is v1).
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

    # Cards carry whatever code the ingest caller used (often the short
    # `ibx-5192` form); the spine's canonical spelling is the dir-slug.
    # Writing the card's code verbatim is how drift seeded (mig 129).
    from cp_engine.mc2_db import canonical_spine_code

    code = canonical_spine_code(client, card.project_id, card.project_code)

    rows = build_create_rows(
        project_id=card.project_id,
        project_code=code,
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
        r["id"] = f"{code}/{est_id}/{r['version_label']}"
        # The body is a distiller's, not a person's (#314).
        mark_machine_derived(r, fidelity, source_label=card.source_ref,
                             now_iso=today_iso)

    client.table(_SUBSTANCE_TABLE).upsert(rows, on_conflict="id").execute()
    from cp_engine.authored_mirror import render_element
    from cp_engine.spine_mirror import MirrorDir

    path = project_dir / "spine" / "_authored" / f"{slug}.md"
    mirror = MirrorDir(project_dir / "spine", writer="cxp spine-frame")
    mirror.write(path, render_element(est_item_id=est_id, rows=rows,
                                      kind="context", path=path))
    mirror.save()

    if flip_card:
        client.table(_INBOX_TABLE).update({"status": "promoted"}).eq(
            "id", card.id
        ).execute()

    return path
