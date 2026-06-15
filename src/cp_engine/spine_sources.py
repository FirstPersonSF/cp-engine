"""Link distilled spine elements to their rag_assets source documents.

A distilled Brief/ClientFeedback element's `source` frontmatter holds
human-readable file refs. This matches those refs to actual rag_assets rows
(by basename) so the element can carry a typed link the spine renders and the
future sweep can follow back to the embedded original. Linkage only — no
semantic matching (a design Non-goal)."""
from __future__ import annotations

import os


def _basename_key(path_or_title: str) -> str:
    """Normalize a path or title to a comparable basename (lowercased)."""
    base = os.path.basename(str(path_or_title).strip().rstrip("/"))
    return base.casefold()


def match_sources_to_assets(source_refs, assets):
    """Given an element's source refs (tuple of str|dict) and a project's
    rag_assets (list of dicts with 'id'/'title'), return a NEW source tuple
    where any plain-string ref whose basename matches an asset title's basename
    is REPLACED by a typed link dict {"type":"rag_asset","id":..,"title":..}.
    Refs already typed (dict) pass through unchanged. Unmatched string refs
    pass through unchanged. Deterministic: first asset match by basename wins
    (sort assets by id for stability)."""
    # Build basename -> asset index. Sort by str(id) so that, for two assets
    # sharing a basename, the smallest id wins deterministically (first-wins).
    # An empty key (blank/whitespace/extensionless title) is NOT indexed —
    # otherwise an empty source ref would spuriously match an untitled asset.
    index: dict[str, dict] = {}
    for asset in sorted(assets, key=lambda a: str(a.get("id"))):
        key = _basename_key(asset.get("title", ""))
        if key:
            index.setdefault(key, asset)

    out = []
    for ref in source_refs:
        if isinstance(ref, dict):
            out.append(ref)
            continue
        ref_key = _basename_key(ref)
        asset = index.get(ref_key) if ref_key else None
        if asset is not None:
            out.append(
                {
                    "type": "rag_asset",
                    "id": asset["id"],
                    "title": asset["title"],
                }
            )
        else:
            out.append(ref)
    return tuple(out)


def fetch_project_assets(client, project_code):
    """Return a project's active rag_assets as a list of dicts.

    Each dict carries (id, title, source_type, scope). Resolves the project's
    uuid via spine_elements (spine_elements.project_id === rag_assets.project_id
    — NOT projects.code, which is a different slug), then queries rag_assets.

    Best-effort by contract: the 'Source documents' facet must never break
    `cp spine`. Returns [] if the project_id can't be resolved, if there are no
    assets, or on any client error. Explicit columns only (rag_assets carries a
    `meta jsonb` we must never pull)."""
    try:
        rows = (
            client.table("spine_elements")
            .select("project_id")
            .eq("project_code", project_code)
            .limit(1)
            .execute()
            .data
        ) or []
        if not rows:
            return []
        pid = rows[0].get("project_id")
        if not pid:
            return []
        return (
            client.table("rag_assets")
            .select("id, title, source_type, scope")
            .eq("project_id", pid)
            .eq("status", "active")
            .execute()
            .data
        ) or []
    except Exception:  # noqa: BLE001 — facet is best-effort, never break the sweep
        return []
