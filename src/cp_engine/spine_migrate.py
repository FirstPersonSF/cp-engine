"""`cp spine-migrate` — convert legacy `spine_elements` rows into `spine_substance`
rows with PROPOSED placement (Task 6.2).

The merge consolidates two stores. Legacy `public.spine_elements` (layer-organized,
no versions, no estimate binding) migrates INTO `public.spine_substance` (est-bound,
versioned). Each legacy element becomes ONE substance row carrying a single `v1`
version, with placement PROPOSED by `placement_rule.propose_placement`:

* cross-cutting layers → the context rail (`placement='context'`, no est binding);
* `Deliverables` → a best-guess `est_item_id` binding (`placement='item'`,
  `binding='proposed'`) for a human to confirm in /spine.

This module owns the PURE conversion (`element_to_substance_row`) and the
migration planning/execution (`plan_migration`, `run_migration`). The CLI wrapper
(`cp spine-migrate <code> [--dry-run]`) is thin.

SLUG GOTCHA (resolved): legacy `spine_elements` rows use the SHORT project_code
(`ibx-5153`) while the canonical dir-slug is `ibx-5153-ai-campaign`. The two
stores never share a project_code — but they DO share `project_id` (the MC project
UUID). So the migration resolves the canonical slug → `project_id` (from existing
`spine_substance` rows, or the projects table), then reads the legacy
`spine_elements` BY `project_id` — sidestepping the slug-name mismatch entirely.

Per the global Supabase rule: every read uses explicit columns, never `*`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from cp_engine.placement_rule import PlacementProposal, propose_placement
from cp_engine.spine import row_to_element

# Columns we read off legacy spine_elements (explicit; never `*`).
_ELEMENT_COLUMNS = (
    "element_id, project_code, layer, type, title, stage, fidelity, "
    "target_date, status, last_touched, depends_on, serves, source, "
    "target_history, author, rel_path, project_id"
)

_SUBSTANCE_TABLE = "spine_substance"


@dataclass(frozen=True)
class MigrationRow:
    """One planned conversion: the source element, the placement proposal, and
    the substance row dict that will be upserted. Carries the matched estimate
    item name (display only) for the dry-run table."""

    element_title: str
    layer: str
    proposal: PlacementProposal
    matched_item_name: str | None
    row: dict


def _element_stem(element_id: str) -> str:
    """The stable final segment of a legacy element_id (already slug-shaped),
    e.g. `ibx-5153/deliverable/positioning-narrative` → `positioning-narrative`.
    Used as the substance-row id's trailing segment so the row is stable across
    re-runs and distinct from siblings binding the same est_item_id."""
    return element_id.rstrip("/").rsplit("/", 1)[-1] or element_id


def element_to_substance_row(
    element,
    proposal: PlacementProposal,
    *,
    canonical_code: str,
    project_id: str,
    est_item_kind: str | None,
    today: date,
) -> dict:
    """Convert one legacy element + its placement proposal into a `spine_substance`
    row dict (PURE — no I/O).

    id scheme (mirrors substance_to_rows / context_to_row):
      * context placement → ``<canonical_code>/_context/<element-stem>``
      * item placement    → ``<canonical_code>/<est_item_id>/<element-stem>``

    The single migrated version is ``v1``. ``binding`` is ``proposed`` for an
    item-placement (a human confirms the guessed estimate binding) and ``unbound``
    for context (nothing to bind to). ``confirmed_by``/``confirmed_at`` stay null —
    migration never auto-confirms. ``version_date`` is the element's last_touched
    when it parses, else ``today``.
    """
    stem = _element_stem(element.id)
    if proposal.placement == "item":
        row_id = f"{canonical_code}/{proposal.est_item_id}/{stem}"
        binding = "proposed"
    else:
        row_id = f"{canonical_code}/_context/{stem}"
        binding = "unbound"

    version_date = element.last_touched if _is_iso_date(element.last_touched) else today.isoformat()

    return {
        "id": row_id,
        "project_id": project_id,
        "project_code": canonical_code,
        "est_item_id": proposal.est_item_id,
        "est_item_kind": est_item_kind,
        "phase": None,
        "binding": binding,
        "layer": element.layer,
        "placement": proposal.placement,
        "serves": list(element.serves),
        "version_label": "v1",
        "version_date": version_date,
        "status": "live",
        "framing": element.title,
        "body": element.body,
        "sources": [],
        "rel_path": str(element.path),
    }


def _is_iso_date(value: str | None) -> bool:
    if not value:
        return False
    try:
        date.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False


def plan_migration(elements, estimate, *, canonical_code, project_id, today) -> list[MigrationRow]:
    """Build the full set of MigrationRows for a project (PURE — no I/O).

    For each element: propose a placement, resolve the matched estimate item's
    kind/name (when it bound), and build the substance row dict. Estimate may be
    None (no backbone yet) — then Deliverables can't bind and fall to context.
    """
    out: list[MigrationRow] = []
    for el in elements:
        proposal = propose_placement(el, estimate) if estimate is not None else _context_fallback(el)
        item = (
            estimate.item_by_id(proposal.est_item_id)
            if (estimate is not None and proposal.est_item_id is not None)
            else None
        )
        est_item_kind = item.kind if item is not None else None
        matched_item_name = item.name if item is not None else None
        row = element_to_substance_row(
            el, proposal,
            canonical_code=canonical_code, project_id=project_id,
            est_item_kind=est_item_kind, today=today,
        )
        out.append(
            MigrationRow(
                element_title=el.title, layer=el.layer, proposal=proposal,
                matched_item_name=matched_item_name, row=row,
            )
        )
    return out


def _context_fallback(el) -> PlacementProposal:
    """No-estimate fallback: everything lands in context (nothing to bind to),
    with the layer's own placement basis preserved where it's cross-cutting."""
    p = propose_placement(el, _EmptyEstimate())
    # propose_placement already routes non-Deliverables correctly; Deliverables
    # come back as context (no items to match) — which is what we want.
    return p


class _EmptyEstimate:
    """A stand-in estimate with no items, so `propose_placement` can run its
    Deliverable branch and fall through to context when there's no backbone."""

    def all_items(self):  # noqa: D401 - trivial
        return ()

    def item_by_id(self, _id):
        return None


def render_dry_run(canonical_code: str, plan: list[MigrationRow]) -> str:
    """Render the dry-run table: per element → proposed placement + binding."""
    n_context = sum(1 for m in plan if m.proposal.placement == "context")
    n_item = sum(1 for m in plan if m.proposal.placement == "item")
    lines = [
        f"spine-migrate {canonical_code} — DRY RUN ({len(plan)} elements: "
        f"{n_item} proposed-binding, {n_context} context)",
        "",
    ]
    for m in plan:
        p = m.proposal
        if p.placement == "item":
            target = f"→ item  {p.est_item_id}  ({m.matched_item_name})  conf {p.confidence:.2f}"
        else:
            target = f"→ context  conf {p.confidence:.2f}"
        lines.append(f"  [{m.layer:<14}] {m.element_title}")
        lines.append(f"        {target}")
        lines.append(f"        basis: {p.basis}")
    return "\n".join(lines)


def run_migration(client, rows: list[dict]) -> None:
    """Upsert the planned substance rows into `spine_substance` (on_conflict=id).

    Does NOT delete `spine_elements` (a later task) and does NOT auto-confirm
    (confirmed_by/confirmed_at left unset). Idempotent: re-running upserts the
    same ids."""
    if rows:
        client.table(_SUBSTANCE_TABLE).upsert(rows, on_conflict="id").execute()


def fetch_legacy_elements(client, *, project_id: str):
    """Read legacy `spine_elements` for a project BY project_id (the cross-store
    join key — see module docstring on the slug gotcha). Returns SpineElements."""
    data = (
        client.table("spine_elements")
        .select(_ELEMENT_COLUMNS)
        .eq("project_id", project_id)
        .execute()
        .data
    ) or []
    return tuple(row_to_element(r) for r in data)


def resolve_project_id(client, canonical_code: str) -> str | None:
    """Resolve a canonical dir-slug → its MC project_id (UUID).

    Lifts it from any existing `spine_substance` row for the canonical slug (the
    same value on every row), mirroring `where.fetch_substance_status`. Returns
    None when the project has no substance rows yet — the caller then can't locate
    the legacy rows by the shared key and should surface a clear error."""
    rows = (
        client.table(_SUBSTANCE_TABLE)
        .select("project_id")
        .eq("project_code", canonical_code)
        .limit(1)
        .execute()
        .data
    ) or []
    if rows and rows[0].get("project_id"):
        return rows[0]["project_id"]
    return None
