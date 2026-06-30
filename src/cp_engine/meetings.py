"""Bridge Fathom meetings into the project/RAG world.

Meetings are tagged in MC-2's ``fathom_meetings.project_tags`` (a ``text[]``)
with human DISPLAY STRINGS, e.g. ``["IBX 5167 DDI Platform Video"]`` — not a
resolved project id. This module resolves those tags to a ``projects.id``.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from cp_engine.asset_ingest import _utc_now_iso
from cp_engine.spine_promote import ingest_single_file

_SKIP_TAGS = {"untagged", ""}


def _safe(s: str) -> str:
    """Sanitize an identifier into a filesystem-safe name (mirrors
    spine_promote._safe / asset_ingest._stable_dir_for's `re.sub`)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(s))


def stamp_meeting_asset(client, *, project_id: str, source_file_id: str,
                        title: str, file_path: str, meta: dict) -> dict:
    """Stamp the just-ingested meeting-summary rag_assets row with provenance.

    Locates the active row by (project_id, file_path, status='active') — the
    same locate-key spine_promote / the folder-scan stamp use — and writes
    source_provider='fathom', source_file_id=<recording_id>, scope='project',
    and `meta` (the kind discriminator). Because the summary is written to a
    STABLE file_path keyed on recording_id, re-embedding lands on the same row,
    so this update is idempotent (re-stamps, never duplicates). No `SELECT *`;
    fields set directly via `.update()` (matching stamp_promoted_asset).
    """
    resp = (
        client.table("rag_assets")
        .update({
            "source_provider": "fathom",
            "source_file_id": source_file_id,
            "source_path": None,
            "scope": "project",
            "meta": meta,
        })
        .eq("project_id", project_id)
        .eq("file_path", file_path)
        .eq("status", "active")
        .execute()
    )
    rows = getattr(resp, "data", None) or []
    return {"stamped": bool(rows), "title": title, "ids": [r.get("id") for r in rows]}


def embed_meeting_summary(client, meeting_row, project_id, *,
                          ingest=ingest_single_file, stamp=stamp_meeting_asset,
                          supabase_url, supabase_key, force=False) -> dict:
    """Embed a tagged meeting's summary text into rag_assets for `project_id`.

    Returns `{"ok": True, "asset_id": ...}` on success, else
    `{"ok": False, "reason": ...}`. NEVER raises — any exception is wrapped.

    Skip conditions (return ok:False WITHOUT any ingest/stamp work):
      - `summary` empty/whitespace → "meeting has no summary".
      - `summary_embedded_at` set and not `force` → "summary already embedded".
      - `recording_id` missing/falsy → "meeting has no recording_id" (it's the
        stable-path + provenance key; a None would corrupt both — fail first,
        mirroring promote_transcript's est_item_id guard).

    The summary is written to a STABLE temp path keyed on recording_id
    (`<tmp>/meeting-embed/<recording_id>/summary.md`), so re-embedding lands on
    the same rag_assets row (idempotent). The SAME path is passed to both
    `ingest` and `stamp`; after stamping we VERIFY exactly one row matched
    (stamped True and len(ids)==1) before write-back, else ok:False — otherwise
    "reported success but nothing was stamped". On success, write
    `fathom_meetings.summary_embedded_at = now()` (located by recording_id).

    The `meta={'kind':'meeting_summary'}` discriminator is load-bearing: it lets
    retrieval separate summary-recall from transcript-depth.
    """
    try:
        summary = meeting_row.get("summary")
        if not summary or not summary.strip():
            return {"ok": False, "reason": "meeting has no summary"}

        if meeting_row.get("summary_embedded_at") and not force:
            return {"ok": False, "reason": "summary already embedded"}

        recording_id = meeting_row.get("recording_id")
        if not recording_id:
            return {"ok": False, "reason": "meeting has no recording_id"}

        title = meeting_row.get("title") or "Meeting summary"

        # STABLE dest path keyed on recording_id, under the SYSTEM temp dir
        # (mirroring spine_promote): deterministic per recording_id, so a
        # re-embed lands on the same rag_assets row.
        dest_dir = Path(tempfile.gettempdir()) / "meeting-embed" / _safe(recording_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "summary.md"
        dest.write_text(summary)
        stable_path = str(dest)

        ingest(stable_path, project_id, title,
               supabase_url=supabase_url, supabase_key=supabase_key)

        # SAME stable_path to stamp, then verify exactly one match.
        st = stamp(client, project_id=project_id, source_file_id=str(recording_id),
                   title=title, file_path=stable_path,
                   meta={"kind": "meeting_summary"})
        ids = st.get("ids") or []
        if not st.get("stamped") or len(ids) != 1:
            reason = ("stamp matched no row" if not st.get("stamped")
                      else f"stamp matched {len(ids)} rows")
            return {"ok": False, "reason": reason}

        # Mark the meeting embedded (located by the stable recording_id key).
        (
            client.table("fathom_meetings")
            .update({"summary_embedded_at": _utc_now_iso()})
            .eq("recording_id", recording_id)
            .execute()
        )

        return {"ok": True, "asset_id": ids[0]}
    except Exception as exc:  # never raises
        return {"ok": False, "reason": str(exc)}


def _default_resolver(client, code):
    from cp_engine.mcp_server import _resolve_project_id

    return _resolve_project_id(client, code)


def resolve_meeting_project(
    client,
    project_tags,
    *,
    resolver=_default_resolver,
) -> tuple[str | None, str | None]:
    """Resolve a meeting's ``project_tags`` to a ``(project_id, matched_tag)``.

    Iterates ``project_tags`` (may be ``None`` or empty), skipping any tag that
    is empty/whitespace or an "untagged" marker. The first tag that ``resolver``
    maps to a truthy project id wins. Returns ``(None, None)`` if none resolve.
    """
    for tag in project_tags or []:
        if not tag or tag.strip().lower() in _SKIP_TAGS:
            continue
        project_id = resolver(client, tag)
        if project_id:
            return project_id, tag
    return None, None
