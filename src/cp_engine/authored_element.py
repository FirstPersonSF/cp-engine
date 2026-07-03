"""The write engine for AUTHORED spine elements (the capture loop).

Pure row-builders shared by the mc-2 endpoint (POST /api/spine/{code}/element)
and the cp MCP write tools. No DB client here — callers upsert the returned
rows. Authored elements live in `spine_substance` with origin='authored':
- bound (serves a work-item) -> binding='live'
- unbound (an Email, a free Synthesis) -> a synthetic est_item_id `_authored/<slug>`,
  binding='unbound'. Either way they carry full version history.

`type` maps onto the `layer` column (the artifact-kind axis) so the existing
by-layer view + reconcile work unchanged; placement is always 'context' for
authored elements (they're project-level, optionally serving work-items).
"""
from __future__ import annotations

import re

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return s or "untitled"


# Canonical layer vocabulary. The spine UI (mc-2 `lib/spine/elementTypes.ts`
# ELEMENT_TYPES) is the source of truth for the stored `layer` strings; the MCP
# write tool documents a LOWERCASE alias vocab. Without convergence the SAME
# kind lands under different strings (`Email` vs `email`, `Source material` vs
# `SourceMaterial`) and the UI's by-layer filters miss whatever the other path
# wrote. The key is the input lowercased with spaces stripped; the value is the
# UI's exact canonical form. Unknown inputs pass through unchanged (a future or
# custom kind is never invented or dropped — only KNOWN aliases converge).
_LAYER_CANON = {
    "email": "Email",
    "note": "Note",
    "decision": "Decisions",
    "decisions": "Decisions",
    "source": "Source material",
    "sourcematerial": "Source material",
    "brief": "Brief",
    "stakeholder": "Stakeholders",
    "stakeholders": "Stakeholders",
    "agreement": "Agreement",
    "synthesis": "Synthesis",
    "output": "Output",
    "activity": "Activity",
    "retrospective": "Retrospective",
    "research": "Research",
    "deliverable": "Deliverables",
    "deliverables": "Deliverables",
    "clientfeedback": "Client feedback",
    "timeline": "Timeline",
}


def canon_layer(type_: str) -> str:
    """Map an element `type` to its canonical `layer` string (see _LAYER_CANON).

    Case- and whitespace-insensitive lookup; an unmapped value is returned
    unchanged so the UI's already-canonical TitleCase forms are idempotent and
    any custom/future kind survives.
    """
    if not type_:
        return type_
    key = type_.lower().replace(" ", "")
    return _LAYER_CANON.get(key, type_)


def authored_est_item_id(slug: str) -> str:
    return f"_authored/{slug}"


def _row(*, project_id, project_code, est_item_id, label, type_, body, serves,
         version_label, version_date, status, version_note=None, sources=None,
         important=False, note=None):
    return {
        "id": f"{project_code}/{est_item_id}/{version_label}",
        "project_id": project_id,
        "project_code": project_code,
        "est_item_id": est_item_id,
        "est_item_kind": None,
        "phase": None,
        "binding": "live" if serves else "unbound",
        "layer": canon_layer(type_),
        "placement": "context",
        "serves": list(serves or []),
        "version_label": version_label,
        "version_date": version_date,
        "status": status,
        "framing": label,           # the human-facing label/framing line
        "body": body,
        "sources": list(sources or []),
        "origin": "authored",
        "version_note": version_note,
        "rel_path": None,
        # PARITY: mirror in mc-2 authored_element
        "important": bool(important),
        "note": note,
    }


def build_create_rows(*, project_id, project_code, label, type_, body, serves, now_iso,
                      sources=None, important=False, note=None):
    """v1 of a new authored element."""
    slug = slugify(label)
    est_item_id = authored_est_item_id(slug)
    date = now_iso[:10]
    return [_row(
        project_id=project_id, project_code=project_code, est_item_id=est_item_id,
        label=label, type_=type_, body=body, serves=serves,
        version_label="v1", version_date=date, status="live",
        sources=sources, important=important, note=note,
    )]


def build_version_rows(*, project_id, project_code, est_item_id, prior_versions,
                       body, version_note, now_iso, label=None, type_=None, serves=None):
    """The NEW live version row for an authored element. The caller is
    responsible for demoting the prior live row to 'superseded' (a targeted
    status update) — we do NOT rebuild prior rows here (that would clobber
    their sources/version_note/any column we forget to carry).

    Note: passing serves=[] on a version drops the binding on the NEW live row;
    history keeps whatever binding its own prior rows already carry.
    """
    nums = [int(v["version_label"][1:]) for v in prior_versions
            if str(v.get("version_label", "")).startswith("v")]
    next_n = (max(nums) + 1) if nums else 1
    # Carry forward from the LIVE row, not prior_versions[0] — the fetch order is
    # unspecified, and set_spine_element mutates important/note on the live row
    # alone, so index-0 can be a stale superseded row. Falls back to [0] only if
    # no row is flagged live (shouldn't happen for a real element).
    base = next((v for v in prior_versions if v.get("status") == "live"), None) \
        or (prior_versions[0] if prior_versions else {})
    return [_row(
        project_id=project_id, project_code=project_code, est_item_id=est_item_id,
        label=label or base.get("framing"), type_=type_ or base.get("layer"),
        body=body, serves=serves if serves is not None else (base.get("serves") or []),
        version_label=f"v{next_n}", version_date=now_iso[:10], status="live",
        version_note=version_note,
        # important/note are element-level: carried forward only, never overridden on
        # a new version. The dedicated set_spine_element path edits them.
        important=base.get("important", False), note=base.get("note"),
    )]
