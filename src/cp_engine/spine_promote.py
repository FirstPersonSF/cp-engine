"""Promote a spine element's source transcript into the RAG store.

A spine element marked important gets its underlying transcript embedded into
`rag_assets` so it's retrievable via pull_project_source.

This module is built up across item-3's tasks:
  - `ingest_single_file` (here) — the thin embed wrapper: feed ONE local file
    to the ingest pipeline. No spine/idempotency logic of its own.
  - the stamp + orchestration (later tasks) add the spine-promote provenance
    and the (owner, source_file_id=est_item_id) check-before-write that makes
    re-promotion update-in-place rather than duplicate.
"""
from __future__ import annotations


def _default_pipeline_factory(project_id: str, supabase_url: str, supabase_key: str):
    # Lazy import: the real `ingest` package is optional and may be absent
    # (e.g. in tests / minimal envs). Importing it here, not at module top,
    # keeps this module importable without `ingest` installed.
    from cp_engine.asset_ingest import _build_pipeline
    return _build_pipeline(project_id, supabase_url, supabase_key)


def ingest_single_file(file_path: str, project_id: str, title: str, *,
                       supabase_url: str, supabase_key: str,
                       pipeline_factory=_default_pipeline_factory) -> dict:
    """Embed ONE local file into rag_assets for `project_id`. Returns the
    pipeline's result dict. `pipeline_factory` is injectable so tests never
    touch the real ingest pipeline / Voyage / Supabase."""
    pipeline = pipeline_factory(project_id, supabase_url, supabase_key)
    return pipeline.ingest_file(file_path, title=title, url=None)


def stamp_promoted_asset(client, *, project_id: str, est_item_id: str,
                         title: str, file_path: str) -> dict:
    """Stamp the just-ingested rag_assets row with spine-promote provenance.

    Locates the active row by (project_id, file_path, status='active') — the
    same locate-key the folder-scan stamp uses — and writes
    source_provider='spine-promote', source_file_id=est_item_id. Because the
    promoted transcript is written to a STABLE file_path keyed on est_item_id,
    re-promotion lands on the same row, so this update is idempotent (re-stamps,
    never duplicates). No `SELECT *`.
    """
    resp = (
        client.table("rag_assets")
        .update({
            "source_provider": "spine-promote",
            "source_file_id": est_item_id,
            "source_path": None,
            "scope": "project",
        })
        .eq("project_id", project_id)
        .eq("file_path", file_path)
        .eq("status", "active")
        .execute()
    )
    rows = getattr(resp, "data", None) or []
    return {"stamped": bool(rows), "title": title, "ids": [r.get("id") for r in rows]}
