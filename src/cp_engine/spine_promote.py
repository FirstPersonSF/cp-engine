"""Promote a spine element's source transcript into the RAG store.

A spine element marked important gets its underlying transcript embedded into
`rag_assets` so it's retrievable via pull_project_source.

Three functions, smallest to largest:
  - `ingest_single_file` — the thin embed wrapper: feed ONE local file to the
    ingest pipeline. No spine/idempotency logic of its own.
  - `stamp_promoted_asset` — stamp the just-ingested row with spine-promote
    provenance (source_provider='spine-promote', source_file_id=est_item_id),
    located by (project_id, file_path, status='active').
  - `promote_transcript` — the orchestrator: resolve rel_path → copy to a
    stable temp path keyed on est_item_id → ingest → stamp → verify exactly
    one row matched. Engagement-only and never raises. Because the stable path
    is deterministic per est_item_id, re-promotion lands on the same row, so
    the whole path is idempotent (update-in-place, never duplicate).
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path


def _default_pipeline_factory(project_id: str, supabase_url: str, supabase_key: str):
    # Lazy import: the real `ingest` package is optional and may be absent
    # (e.g. in tests / minimal envs). Importing it here, not at module top,
    # keeps this module importable without `ingest` installed.
    from cp_engine.asset_ingest import _build_pipeline, _configure_pipeline_once

    # Install document-ingest's settings + OpenAI client factory BEFORE building.
    # `_build_pipeline` -> `IngestPipeline.__init__` eagerly constructs an
    # AudioParser whose default client reads OPENAI_API_KEY from the raw env; the
    # configure step replaces that with cp's own factory. The regular ingest path
    # (`ingest_project_assets`) does this; the promote path must too, or pipeline
    # construction raises `OpenAIError: Missing credentials` where the key isn't
    # in env (the Railway webhook) — masked downstream as "stamp matched no row".
    _configure_pipeline_once()
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


def _safe(s: str) -> str:
    """Sanitize an identifier into a filesystem-safe name (mirrors
    asset_ingest._stable_dir_for's `re.sub`)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(s))


def promote_transcript(client, tenant_root, project_code, project_id, company_id,
                       element_row, *, supabase_url, supabase_key,
                       ingest=ingest_single_file, stamp=stamp_promoted_asset) -> dict:
    """Orchestrate resolve→file→ingest→stamp for one important spine element.

    Returns `{"ok": True, "title", "ids"}` on success, else
    `{"ok": False, "reason": ...}`. NEVER raises — any exception is wrapped.

    Two load-bearing contracts:
      A — engagement-only guard: if `company_id is None` (an initiative, whose
          folders don't resolve) return ok:False WITHOUT any file/ingest work.
      B — stamp↔ingest seam: ONE stable file_path (keyed on est_item_id) is
          passed to BOTH `ingest` and `stamp`; the stamp locates its rag_assets
          row by that file_path. After stamping we VERIFY it matched exactly one
          row (stamped True, len(ids)==1) before reporting ok:True — otherwise a
          "promotion reported success but nothing was stamped" silent no-op.
    """
    try:
        # CONTRACT A — initiative guard, before any file work.
        if company_id is None:
            return {"ok": False, "reason": "initiative promotion not yet supported"}

        rel_path = element_row.get("rel_path")
        if not rel_path:
            return {"ok": False, "reason": "element has no source file (rel_path)"}

        est_item_id = element_row.get("est_item_id")
        if not est_item_id:
            # A None/empty est_item_id would corrupt BOTH the stable path
            # (`.../None/`) and the stamp provenance key (source_file_id=None),
            # silently breaking idempotency. Fail before any file work.
            return {"ok": False, "reason": "element has no est_item_id"}

        # Resolve the project's REAL on-disk working dir. Engagements are
        # account-nested (`<tenant>/1p/<company>/<code>/`), not flat under the
        # tenant root, so joining the bare code onto tenant_root misses every
        # real engagement. `_resolve_project_cp_path` walks all scopes (1p/<co>,
        # firstpersonsf/, canonic/) and returns the cp.md; its parent is the
        # working dir rel_path is relative to. Fall back to the flat layout when
        # no cp.md resolves (keeps minimal/test trees working).
        from cp_engine.ingest import _resolve_project_cp_path

        cp_path = _resolve_project_cp_path(Path(tenant_root), project_code)
        work_dir = cp_path.parent if cp_path else Path(tenant_root) / project_code
        src = work_dir / rel_path
        if not src.exists():
            return {"ok": False, "reason": f"transcript file not found: {src}"}

        # CONTRACT B step 1 — ONE stable dest path keyed on est_item_id. Rooted
        # at the SYSTEM temp dir (mirroring asset_ingest._stable_dir_for), NOT
        # the tenant tree: the file need not be durable, only the path string
        # deterministic per est_item_id (re-promote → same path → stamp matches
        # the same row; same content → same hash). Keeping it out of the tenant
        # repo avoids cluttering the checked-out working tree.
        dest_dir = Path(tempfile.gettempdir()) / "spine-promote" / _safe(est_item_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        shutil.copy2(src, dest)
        stable_path = str(dest)

        title = element_row.get("framing") or src.name

        ingest(stable_path, project_id, title,
               supabase_url=supabase_url, supabase_key=supabase_key)

        # CONTRACT B — SAME stable_path to stamp, then verify exactly one match.
        st = stamp(client, project_id=project_id, est_item_id=est_item_id,
                   title=title, file_path=stable_path)
        ids = st.get("ids") or []
        if not st.get("stamped") or len(ids) != 1:
            reason = ("stamp matched no row" if not st.get("stamped")
                      else f"stamp matched {len(ids)} rows")
            return {"ok": False, "reason": reason}

        return {"ok": True, "title": title, "ids": st["ids"]}
    except Exception as exc:  # never raises
        return {"ok": False, "reason": str(exc)}
