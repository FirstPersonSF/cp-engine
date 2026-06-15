"""Link distilled shell elements to their rag_assets source documents.

A distilled Brief/ClientFeedback element's `source` frontmatter holds
human-readable file refs. This matches those refs to actual rag_assets rows
(by basename) so the element can carry a typed link the shell renders and the
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
    index: dict[str, dict] = {}
    for asset in sorted(assets, key=lambda a: str(a.get("id"))):
        key = _basename_key(asset.get("title", ""))
        index.setdefault(key, asset)

    out = []
    for ref in source_refs:
        if isinstance(ref, dict):
            out.append(ref)
            continue
        asset = index.get(_basename_key(ref))
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
