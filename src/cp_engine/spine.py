"""Project Spine — slice 1 (markdown-only, read-only Lens).

A spine *element* is a markdown file under
`1p/<acct>/<proj>/spine/<Layer>/<name>.md` whose YAML frontmatter is the
structured spine (design §3). This module parses those files and computes the
Lens relevance score (design §2). No MC-2, no writes — slice 1 is read-only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import frontmatter
import yaml

from cp_engine.state import INACTIVE_DIR_NAME
from cp_engine.sync import _SCOPE_DIRS, _find_project_dir, _project_parent_dirs

# The 11 fixed layers (dir names == `layer:` values).
LAYERS: tuple[str, ...] = (
    "Brief",
    "Agreement",
    "Research",
    "SourceMaterial",
    "ClientFeedback",
    "Synthesis",
    "Drafts",
    "Deliverables",
    "Decisions",
    "Timeline",
    "Stakeholders",
    "Retrospective",
)

# Layers that are always ambient — relevant regardless of which deliverable is
# active (design §2: "project-framing layer ... always ambient").
FRAMING_LAYERS: frozenset[str] = frozenset(
    {"Brief", "Agreement", "Timeline", "Stakeholders", "Retrospective"}
)


@dataclass(frozen=True)
class SpineElement:
    """One addressable unit in a project spine (frontmatter spine + body)."""

    id: str
    project: str
    layer: str
    title: str
    status: str  # active | dormant | final | reference
    last_touched: str  # ISO date string; kept as text (slice-1 simplicity)
    path: Path
    body: str
    type: str | None = None
    stage: str | None = None  # conception|first|revised|final
    fidelity: str | None = None  # text|design|motion
    target_date: str | None = None
    depends_on: tuple[str, ...] = ()
    serves: tuple[str, ...] = ()
    target_history: tuple[dict, ...] = ()
    # May hold a mix of plain-string file refs and typed-link dicts
    # ({"type":"rag_asset","id":...}); see _as_source.
    source: tuple = ()
    author: str | None = None
    # Read-only spine verification metadata (slice: spine inversion). Sourced
    # only from MC-2; never round-trips into markdown or the mirror payload —
    # the reconcile writer owns these columns.
    field_states: dict = field(default_factory=dict)  # {field: "proposed"|"confirmed"}
    review_flags: tuple[dict, ...] = ()  # re-derivation conflicts
    confirmed_by: str | None = None
    confirmed_at: str | None = None


def _as_tuple(value: object) -> tuple:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def _as_source(value: object) -> tuple:
    """Coerce a frontmatter/row `source` into a tuple, preserving dict entries
    (typed links like {"type":"rag_asset","id":...}) while stringifying scalars
    (legacy plain-text file refs)."""
    return tuple(x if isinstance(x, dict) else str(x) for x in _as_tuple(value))


def parse_element(path: Path) -> SpineElement:
    """Parse one spine element markdown file into a SpineElement."""
    try:
        post = frontmatter.load(str(path))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: failed to parse frontmatter: {exc}") from exc
    meta = post.metadata

    def _str(key: str) -> str | None:
        v = meta.get(key)
        return None if v is None else str(v)

    def _required(key: str) -> str:
        try:
            return str(meta[key])
        except KeyError:
            raise ValueError(
                f"{path}: spine element missing required key '{key}'"
            ) from None

    return SpineElement(
        id=_required("id"),
        project=_required("project"),
        layer=_required("layer"),
        title=str(meta.get("title", path.stem)),
        status=str(meta.get("status", "active")),
        last_touched=str(meta.get("last_touched", "")),
        path=path,
        body=post.content,
        type=_str("type"),
        stage=_str("stage"),
        fidelity=_str("fidelity"),
        target_date=_str("target_date"),
        depends_on=tuple(str(x) for x in _as_tuple(meta.get("depends_on"))),
        serves=tuple(str(x) for x in _as_tuple(meta.get("serves"))),
        target_history=_as_tuple(meta.get("target_history")),
        source=_as_source(meta.get("source")),
        author=_str("author"),
    )


# Layer-importance weights (design open-thread #1, resolved from the IBX
# backfill in a later task). Higher = stays warmer in the ranked sweep.
# Rationale: Deliverables are the project's spine; Decisions and ClientFeedback
# carry the "still wants attention" signal; framing layers
# (Brief/Timeline/Stakeholders) are ambient-important but shouldn't outrank live
# work; raw inputs (SourceMaterial/Research) matter most via `serves`, less on
# their own weight.
LAYER_IMPORTANCE: dict[str, float] = {
    "Deliverables": 1.00,
    "Decisions": 0.85,
    "ClientFeedback": 0.80,
    "Drafts": 0.75,
    "Synthesis": 0.70,
    "Brief": 0.65,
    "Agreement": 0.60,
    "Timeline": 0.60,
    "Stakeholders": 0.55,
    "Research": 0.50,
    "SourceMaterial": 0.45,
    # Living per-project meeting timeline — moderately important, framing
    # (never demotes). Weight tunable after a real sweep; see Part B of the
    # spine-inversion design.
    "Retrospective": 0.60,
}


def load_spine(project_dir: Path) -> tuple[SpineElement, ...]:
    """Parse every spine element under `<project_dir>/spine/<Layer>/*.md`."""
    spine_root = project_dir / "spine"
    if not spine_root.is_dir():
        return ()
    elements: list[SpineElement] = []
    for layer in LAYERS:
        layer_dir = spine_root / layer
        if not layer_dir.is_dir():
            continue
        for md in sorted(layer_dir.glob("*.md")):
            elements.append(parse_element(md))
    return tuple(elements)


def active_deliverable_ids(elements: tuple[SpineElement, ...]) -> set[str]:
    """Ids of deliverables that are the project's live focus.

    Active = a Deliverables-layer element whose stage is not `final` and whose
    status is `active` (not dormant/final/reference). Design §2 coordinate #1.
    """
    return {
        e.id
        for e in elements
        if e.layer == "Deliverables"
        and e.stage != "final"
        and e.status == "active"
    }


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _date_or_none(value: str | None) -> str | None:
    """Return an ISO date string only if it parses; else None (→ SQL null)."""
    return value if (value and _parse_date(value)) else None


def element_to_row(
    el: SpineElement,
    *,
    project_id: str,
    project_root: Path,
) -> dict[str, object]:
    """Map a SpineElement to its MC-2 `spine_elements` row (pure, no I/O).

    `project_root` is the tenant root, used to compute a tenant-relative
    `rel_path` for round-trip back to the file. Date fields that don't parse
    become null (empty `last_touched` is common on framing elements)."""
    try:
        rel = str(el.path.relative_to(project_root))
    except ValueError:
        rel = el.path.name
    return {
        "element_id": el.id,
        "project_id": project_id,
        "project_code": el.project,
        "layer": el.layer,
        "type": el.type,
        "title": el.title,
        "stage": el.stage,
        "fidelity": el.fidelity,
        "target_date": _date_or_none(el.target_date),
        "status": el.status,
        "last_touched": _date_or_none(el.last_touched),
        "depends_on": list(el.depends_on),
        "serves": list(el.serves),
        "source": list(el.source),
        # nested date values pass through verbatim; normalizing for JSONB (if
        # ever needed) is the sync writer's job, not this pure mapper's.
        "target_history": list(el.target_history),
        "author": el.author,
        "rel_path": rel,
    }


def row_to_element(row: dict[str, object]) -> SpineElement:
    """Reconstruct a SpineElement from a `spine_elements` MC-2 row.

    Body is empty (the spine row doesn't carry it — read the file via rel_path
    if the body is needed). Sufficient for the Lens, which scores from spine.
    """
    def _t(key: str) -> tuple[str, ...]:
        v = row.get(key) or []
        return tuple(str(x) for x in v)
    return SpineElement(
        id=row["element_id"],
        project=row.get("project_code", ""),
        layer=row["layer"],
        title=row.get("title", row["element_id"]),
        status=row.get("status", "active"),
        last_touched=str(row.get("last_touched") or ""),
        path=Path(row.get("rel_path") or row["element_id"]),
        body="",
        type=row.get("type"),
        stage=row.get("stage"),
        fidelity=row.get("fidelity"),
        target_date=(str(row["target_date"]) if row.get("target_date") else None),
        depends_on=_t("depends_on"),
        serves=_t("serves"),
        target_history=tuple(row.get("target_history") or []),
        source=_as_source(row.get("source")),
        author=row.get("author"),
        field_states=dict(row.get("field_states") or {}),
        review_flags=tuple(row.get("review_flags") or []),
        confirmed_by=row.get("confirmed_by"),
        confirmed_at=(str(row["confirmed_at"]) if row.get("confirmed_at") else None),
    )


# Substance version status → Lens status. The `spine_substance` store has a
# narrow lifecycle vocabulary (`live | superseded`); the Lens scores on the
# richer `active | reference | dormant | final` axis. A `live` version maps to
# `active` (its floor) — `derive_status` then auto-demotes it from the
# deliverable graph during the sweep, exactly as for the legacy element path.
# `superseded` versions never reach the sweep (filtered before mapping), so they
# need no Lens status.
_SUBSTANCE_STATUS_MAP: dict[str, str] = {"live": "active"}

# Item-placement substance rows that carry no explicit `layer` are estimate
# work-item deliverables — they ARE the project's spine of buildable outputs.
_DEFAULT_SUBSTANCE_LAYER = "Deliverables"


def substance_row_to_element(row: dict[str, object]) -> SpineElement:
    """Reconstruct a SpineElement from a `spine_substance` MC-2 row.

    `spine_substance` hangs distilled, versioned memory off estimate work items
    (and project-level context). Its schema differs from the legacy
    `spine_elements` table, so the fields the Lens reads are remapped:

      * id           ← row `id` (``<code>/<item>/<version>`` or ``…/_context/…``)
      * title        ← `framing` (the framing line IS the element's title)
      * body         ← `body`
      * layer        ← `layer` (defaults to Deliverables when null — native
                       item rows often omit it; migrated rows always carry it)
      * status       ← `status` mapped live→active (see `_SUBSTANCE_STATUS_MAP`);
                       `derive_status` demotes from there during the sweep
      * last_touched ← `version_date` (substance has no separate last_touched)
      * serves       ← `serves`

    Columns the legacy element model had but substance does not (type/stage/
    fidelity/target_date/depends_on/source/verification metadata) default to
    None/empty — the Lens tolerates their absence (stage gates only the
    `active_deliverable_ids` set, which still works off layer+status).
    """
    raw_status = str(row.get("status") or "live")
    return SpineElement(
        id=str(row["id"]),
        project=str(row.get("project_code") or ""),
        layer=str(row.get("layer") or _DEFAULT_SUBSTANCE_LAYER),
        title=str(row.get("framing") or row["id"]),
        status=_SUBSTANCE_STATUS_MAP.get(raw_status, raw_status),
        last_touched=str(row.get("version_date") or ""),
        path=Path(str(row.get("rel_path") or row["id"])),
        body=str(row.get("body") or ""),
        serves=tuple(str(x) for x in (row.get("serves") or [])),
        source=_as_source(row.get("sources")),
    )


def load_spine_from_mc2(client, project_code: str) -> tuple[SpineElement, ...]:
    """Load a project's spine elements from `spine_substance` (canonical read).

    One sweep entry per element: substance is versioned (an item row can hold
    v1/v2/v3), so we filter to the single ``live`` version per item — context
    rows are single-version and also ``live``. Superseded versions are excluded
    so the sweep shows the current state of each work item, not its history.
    """
    data = (
        client.table("spine_substance")
        .select(
            "id, project_code, est_item_id, layer, placement, status, "
            "version_label, version_date, framing, body, serves, sources, "
            "rel_path"
        )
        .eq("project_code", project_code)
        .eq("status", "live")
        .execute()
        .data
    ) or []
    return tuple(substance_row_to_element(r) for r in data)


def _recency_term(last_touched: str, today: date) -> float:
    """Exponential decay on age in days. ~30-day half-life; floored at 0.1 so
    cold never reaches zero ("a dimmer, not an off-switch", design §2)."""
    d = _parse_date(last_touched)
    if d is None:
        return 0.1
    age_days = max(0, (today - d).days)
    decayed = math.exp(-age_days / 43.28)  # 43.28 ≈ 30-day half-life
    return max(0.1, decayed)


def _serves_active_term(el: SpineElement, active: set[str]) -> float:
    """1.0 if the element serves an active deliverable OR is a framing layer
    (always ambient); 0.35 otherwise (cold but not gone)."""
    if el.layer in FRAMING_LAYERS:
        return 1.0
    if el.layer == "Deliverables" and el.id in active:
        return 1.0
    if any(s in active for s in el.serves):
        return 1.0
    return 0.35


# Status weights — a settled element stays in consideration but ranks below
# live work (design §2: "deliverable ships → its elements demote together";
# "cold is a dimmer, not an off-switch"). A freshly-delivered `final` artifact
# should sit just under the still-open work it served, not tie it.
# NOTE: `reference` and `dormant` share weight 0.7 — so derive_status's
# reference-vs-dormant distinction changes only the displayed marker, not the
# Lens score. Only the active (1.0) vs cool (0.7) split moves sweep ranking.
_STATUS_WEIGHT: dict[str, float] = {
    "active": 1.0,
    "reference": 0.7,
    "dormant": 0.7,
    "final": 0.5,
}


def _status_term(status: str) -> float:
    return _STATUS_WEIGHT.get(status, 1.0)


def _deliverable_is_blocked(
    deliv: SpineElement, by_id: dict[str, SpineElement]
) -> bool:
    """A deliverable is blocked if any dependency is not yet final."""
    for dep_id in deliv.depends_on:
        dep = by_id.get(dep_id)
        if dep is None or dep.stage != "final":
            return True
    return False


def _is_active_unblocked(deliv: SpineElement, by_id: dict[str, SpineElement]) -> bool:
    """A deliverable that is live: not shipped (final) and not blocked on a dep."""
    return deliv.stage != "final" and not _deliverable_is_blocked(deliv, by_id)


def derive_status(el: SpineElement, by_id: dict[str, SpineElement]) -> str:
    """Compute an element's effective status from the deliverable graph.

    Derivation only *demotes* below an active frontmatter status, never
    promotes (relevance is computed, the floor is what frontmatter
    declares). Framing-layer elements are always ambient and never demote.
    An explicit frontmatter `final`/`reference` is honored as-is.

    When an element serves multiple deliverables, the *warmest* consumer wins
    (active > reference > dormant): it stays active if it feeds any live
    (unblocked, non-final) deliverable; settles to reference if it only feeds
    shipped work (still citable); cools to dormant only when every consumer is
    blocked.
    """
    if el.status != "active":
        return el.status  # explicit non-active frontmatter wins
    if el.layer in FRAMING_LAYERS:
        return "active"
    # Find the deliverables this element serves (or is, if it's a deliverable).
    served = [by_id[s] for s in el.serves if s in by_id]
    if el.layer == "Deliverables" and el.id in by_id:
        served.append(el)
    if not served:
        return "active"
    # Warmest-wins: an element is as live as its liveliest consumer. Any
    # active-unblocked served deliverable keeps it active; else a shipped (final)
    # consumer keeps it reference (citable); only when every consumer is blocked
    # does it cool to dormant.
    if any(_is_active_unblocked(d, by_id) for d in served):
        return "active"
    if any(d.stage == "final" for d in served):
        return "reference"
    return "dormant"


def score_element(
    el: SpineElement,
    active: set[str],
    today: date,
    by_id: dict[str, SpineElement] | None = None,
) -> float:
    """The Lens score: recency × serves-active × layer-importance × status
    (design §2). The status term demotes settled (final/reference/dormant)
    elements below live ones without dropping them out of the sweep.

    When `by_id` is supplied, the status term uses the *derived* status
    (auto-demotion from the deliverable graph); otherwise it falls back to
    the frontmatter `status` (backward compatible)."""
    recency = _recency_term(el.last_touched, today)
    serves = _serves_active_term(el, active)
    importance = LAYER_IMPORTANCE.get(el.layer, 0.5)
    effective_status = derive_status(el, by_id) if by_id is not None else el.status
    status = _status_term(effective_status)
    return recency * serves * importance * status


def rank_elements(
    elements: tuple[SpineElement, ...], *, today: date
) -> tuple[list[tuple[float, SpineElement]], dict[str, str]]:
    """Rank elements by the Lens score (hot first) + the derived-status map.

    The canonical ranking contract shared by `render_sweep` (display) and the
    sweep prompt builder — factored out so the two views can never drift. Returns
    `(scored, effective)` where `scored` is `(score, element)` pairs sorted
    hottest-first with a `(layer, id)` tiebreak, and `effective` maps element id
    → derived (auto-demoted) status.
    """
    active = active_deliverable_ids(elements)
    by_id = {e.id: e for e in elements}
    effective = {e.id: derive_status(e, by_id) for e in elements}
    scored = sorted(
        ((score_element(e, active, today, by_id), e) for e in elements),
        key=lambda pair: (-pair[0], pair[1].layer, pair[1].id),
    )
    return scored, effective


class SpineDirNotFound(Exception):
    """No working dir matching the given code under any scope root."""


def find_spine_dir(tenant_root: Path, code: str) -> Path:
    """Locate a project's working dir from its code, offline (no MC-2).

    Reuses sync's scope/account-nesting model (`_project_parent_dirs`) and
    prefix-matcher (`_find_project_dir`) so this stays in lockstep with how
    sync itself lays out the tree. Raises SpineDirNotFound if nothing matches.
    """
    if code == INACTIVE_DIR_NAME:
        # Never resolve the inactive bin itself as a project.
        raise SpineDirNotFound(f"'{code}' is not a valid project code")
    for scope in _SCOPE_DIRS:
        for parent in _project_parent_dirs(tenant_root, scope):
            hit = _find_project_dir(parent, code)
            if hit is not None:
                return hit
    raise SpineDirNotFound(
        f"No working dir for '{code}' under {', '.join(_SCOPE_DIRS)}/"
    )


def _glyph(score: float) -> str:
    """Cosmetic tier glyph for the sweep — helps the eye scan hot vs cold."""
    if score >= 0.66:
        return "◆"  # hot
    if score >= 0.33:
        return "◇"  # warm
    return "○"  # cold


def render_sweep(
    code: str,
    elements: tuple[SpineElement, ...],
    *,
    today: date,
) -> str:
    """Render the full ranked sweep (design §2 'whole-project sweep' mode).

    Every element, hot and cold, ranked by Lens score descending. Read-only.
    """
    # Effective (derived) status drives both the score and the marker, so the
    # display can never contradict the ranking. Shared with the sweep prompt
    # builder via rank_elements so the two views can never drift.
    scored, effective = rank_elements(elements, today=today)
    lines = [f"{code} — full sweep ({len(elements)} elements)"]
    if not elements:
        lines.append("  (no spine/ elements — has this project been backfilled?)")
        return "\n".join(lines)
    for score, e in scored:
        suffix_parts: list[str] = []
        if e.stage:
            suffix_parts.append(f"[{e.stage}]")
        if e.target_date:
            due_date = _parse_date(e.target_date)
            if due_date is not None and due_date < today:
                suffix_parts.append(f"due {e.target_date} (overdue)")
            else:
                suffix_parts.append(f"due {e.target_date}")
        eff_status = effective[e.id]
        if eff_status in ("reference", "dormant"):
            suffix_parts.append(f"({eff_status})")
        suffix = ("  " + " ".join(suffix_parts)) if suffix_parts else ""
        lines.append(
            f"  {score:0.2f} {_glyph(score)} {e.title}  ·{e.layer}{suffix}"
        )
        if e.serves:
            lines.append(f"        ← serves: {', '.join(e.serves)}")
    return "\n".join(lines)


# Cap the rendered asset list — ibx-5153 alone carries ~101 assets, and the
# facet is an orientation aid, not a full manifest. The header count is always
# the TRUE total (no silent caps); only the bullet list is truncated.
_SOURCE_DOCS_CAP = 25


def render_source_documents(assets, elements) -> str:
    """Render the 'Source documents' facet block (a string).

    `assets` is the list from `fetch_project_assets`; `elements` is the
    project's SpineElements. Links are computed LIVE: each element's `source`
    refs (plain-string file refs and/or already-typed dicts) are matched to the
    assets by basename via `match_sources_to_assets`, so each linked asset shows
    which distilled element(s) cite it without persisting links into frontmatter.

    Linked assets sort first (most useful), then the rest by title. Long lists
    are capped at `_SOURCE_DOCS_CAP` bullets with an `…and K more` line; the
    header `(N)` is always the true total. Returns '' when there are no assets
    so the caller can omit the block entirely.
    """
    if not assets:
        return ""

    from cp_engine.spine_sources import match_sources_to_assets

    # asset_id -> [linking element titles], computed live (frontmatter untouched).
    linked_by: dict[str, list[str]] = {}
    for el in elements:
        if not el.source:
            continue
        for ref in match_sources_to_assets(el.source, assets):
            if isinstance(ref, dict) and ref.get("type") == "rag_asset":
                linked_by.setdefault(ref.get("id"), []).append(el.title)

    total = len(assets)
    # Linked assets first, then by title (both groups title-sorted).
    ordered = sorted(
        assets,
        key=lambda a: (
            0 if linked_by.get(a.get("id")) else 1,
            str(a.get("title", "")).casefold(),
        ),
    )

    lines = [f"Source documents ({total})"]
    for a in ordered[:_SOURCE_DOCS_CAP]:
        title = a.get("title", "(untitled)")
        src_type = a.get("source_type") or "?"
        scope = a.get("scope") or "?"
        suffix = ""
        linkers = linked_by.get(a.get("id"))
        if linkers:
            suffix = f"  ← {', '.join(linkers)}"
        lines.append(f"  • {title}  ·{src_type}  [{scope}]{suffix}")
    remaining = total - _SOURCE_DOCS_CAP
    if remaining > 0:
        lines.append(f"  …and {remaining} more")
    return "\n".join(lines)
