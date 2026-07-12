"""Tenant-wide same-title duplicate cleanup for `rag_assets` (#57).

Before the same-title supersede landed in the ingest path, re-ingesting a doc
whose CONTENT changed created a second active `rag_assets` row under the same
title (the hash dedup — mig 067 — only catches byte-identical files, and the
pipeline's own dedup keys on file_path). This module is the one-time backlog
sweep: find same-owner same-title (case-insensitive) groups of ACTIVE assets,
keep the newest, chain it to its predecessors via `prev_asset_id`, and retire
the older copies (status='superseded' + chunks deleted — embeddings cascade).

Safety rails:
  - DRY-RUN is the only pure-read mode; `apply_dedupe` is called only behind
    the CLI's explicit `--apply` flag.
  - Spine `sources` references are checked FIRST: a group whose older copy is
    referenced by any spine element (a typed `{"type": "rag_asset", "id": ...}`
    ref) is BLOCKED — reported, never touched — so a cited source is never
    silently retired out from under its element.
  - Asset ROWS are never deleted (they are chain history); only their chunks
    are, and only for the non-kept copies.

Pure planning functions take plain row lists so tests need no client; the two
fetchers and `apply_dedupe` are the only Supabase-touching pieces.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cp_engine.mc2_db import Tables

# Explicit columns, never `*` — rag_assets.meta and spine_substance.body are
# the classic megabyte columns and neither is needed here.
_ASSET_COLUMNS = (
    "id, title, project_id, initiative_id, file_hash, created_at, prev_asset_id"
)
_SPINE_SOURCES_COLUMNS = "id, sources"

_PAGE_SIZE = 1000


@dataclass(frozen=True)
class DedupeGroup:
    """One same-owner same-title duplicate group and its plan."""

    owner_col: str  # 'project_id' | 'initiative_id'
    owner_id: str
    title: str  # the keeper's casing
    keeper: dict
    losers: list[dict] = field(default_factory=list)  # newest → oldest
    # Older copies referenced by spine `sources`. A non-empty list BLOCKS the
    # whole group: report, don't touch (rebinding the element is a human call).
    blocked_refs: list[dict] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_refs)


def _paged(build_query) -> list[dict]:
    """Drain a PostgREST query page by page (server default caps at 1000).

    `build_query()` must return a FRESH query each call (PostgREST builders
    are single-shot); `.range(start, end)` is inclusive.
    """
    rows: list[dict] = []
    start = 0
    while True:
        resp = build_query().range(start, start + _PAGE_SIZE - 1).execute()
        page = getattr(resp, "data", None) or []
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            return rows
        start += _PAGE_SIZE


def fetch_active_assets(client) -> list[dict]:
    """Every ACTIVE rag_assets row tenant-wide (paginated, explicit columns)."""
    return _paged(
        lambda: (
            client.table(Tables.RAG_ASSETS)
            .select(_ASSET_COLUMNS)
            .eq("status", "active")
            .order("created_at", desc=True)
        )
    )


def spine_referenced_asset_ids(rows: list[dict]) -> set[str]:
    """Asset ids referenced by spine elements' typed `sources` links.

    `spine_substance.sources` holds a list of refs; the typed shape is
    `{"type": "rag_asset", "id": <asset uuid>, ...}` (see
    spine_sources.match_sources_to_assets). Plain-string refs carry no id and
    are ignored. Tolerant of null/malformed sources.
    """
    out: set[str] = set()
    for row in rows:
        sources = row.get("sources") or []
        if not isinstance(sources, (list, tuple)):
            continue
        for ref in sources:
            if (
                isinstance(ref, dict)
                and ref.get("type") == "rag_asset"
                and ref.get("id")
            ):
                out.add(str(ref["id"]))
    return out


def fetch_spine_referenced_asset_ids(client) -> set[str]:
    """`spine_referenced_asset_ids` over EVERY spine_substance row.

    Deliberately unfiltered by status/archived: a superseded or archived
    element version still cites its source, and the safest cleanup treats any
    citation as a hold.
    """
    rows = _paged(
        lambda: client.table(Tables.SPINE_SUBSTANCE).select(_SPINE_SOURCES_COLUMNS)
    )
    return spine_referenced_asset_ids(rows)


def _owner_key(row: dict) -> tuple[str, str] | None:
    """(owner_col, owner_id) for a rag_assets row; None if unowned (bad data)."""
    if row.get("project_id"):
        return ("project_id", str(row["project_id"]))
    if row.get("initiative_id"):
        return ("initiative_id", str(row["initiative_id"]))
    return None


def plan_dedupe(
    assets: list[dict], referenced_ids: set[str]
) -> list[DedupeGroup]:
    """Group active assets by (owner, lower(title)); plan keep-newest cleanup.

    Only groups with 2+ rows come back. Within a group, rows sort newest-first
    by `created_at` (string sort — ISO timestamps order correctly); the first
    is the keeper, the rest are losers. Losers referenced by spine `sources`
    move to `blocked_refs` and block the whole group (see DedupeGroup).
    Untitled rows are skipped (nothing to collide on).
    """
    buckets: dict[tuple[str, str, str], list[dict]] = {}
    for row in assets:
        owner = _owner_key(row)
        title = (row.get("title") or "").strip()
        if owner is None or not title:
            continue
        buckets.setdefault((*owner, title.casefold()), []).append(row)

    groups: list[DedupeGroup] = []
    for (owner_col, owner_id, _key), rows in buckets.items():
        if len(rows) < 2:
            continue
        rows = sorted(
            rows, key=lambda r: str(r.get("created_at") or ""), reverse=True
        )
        keeper, rest = rows[0], rows[1:]
        blocked = [r for r in rest if str(r.get("id")) in referenced_ids]
        losers = [r for r in rest if str(r.get("id")) not in referenced_ids]
        groups.append(
            DedupeGroup(
                owner_col=owner_col,
                owner_id=owner_id,
                title=keeper.get("title") or "",
                keeper=keeper,
                losers=losers,
                blocked_refs=blocked,
            )
        )
    # Deterministic report order: by owner then title.
    groups.sort(key=lambda g: (g.owner_col, g.owner_id, g.title.casefold()))
    return groups


def apply_dedupe(client, groups: list[DedupeGroup]) -> dict[str, int]:
    """Execute the plan: chain the keeper, retire the losers. Skips blocked.

    Per actionable group:
      1. If the keeper has no `prev_asset_id` yet, chain it to the NEWEST
         loser (an existing chain — e.g. from the pipeline's own same-path
         versioning — is never clobbered).
      2. Each loser: `status='superseded'` + its `asset_chunks` deleted
         (embeddings FK-cascade with the chunks). Rows are kept as history.

    Returns counts: {'groups': .., 'retired': .., 'chained': .., 'blocked': ..}.
    """
    from cp_engine.asset_ingest import _retire_asset

    counts = {"groups": 0, "retired": 0, "chained": 0, "blocked": 0}
    for group in groups:
        if group.blocked:
            counts["blocked"] += 1
            continue
        if not group.losers:
            continue
        counts["groups"] += 1
        if not group.keeper.get("prev_asset_id"):
            client.table(Tables.RAG_ASSETS).update(
                {"prev_asset_id": group.losers[0]["id"]}
            ).eq("id", group.keeper["id"]).execute()
            counts["chained"] += 1
        for loser in group.losers:
            _retire_asset(client, loser["id"])
            counts["retired"] += 1
    return counts
