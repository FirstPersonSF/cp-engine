"""Resolve fathom-meeting-sync display tags to canonical cp project codes.

THE single owner of the tag→code heuristic (arch-phase-2). fathom-meeting-sync
historically re-implemented this parse as ``projectTagToCode`` in
``auto-ingest-trigger.js``; it now calls the cp-engine-webhook's
``POST /api/resolve-tags`` (which wraps :func:`resolve_tags`), keeping its
local parse only as a fallback for when the webhook is unreachable.

Two recognized tag shapes (mirroring the dashboard's ``project_tags``):

- **Engagement display name** — ``"GGL 5136 go/safety website"`` →
  ``ggl-5136``. Three-letter company prefix + optional separator + 3-5
  digit number. Resolution is DB-backed where possible: the number is
  looked up in MC-2 ``projects`` and the code is rebuilt from the COMPANY
  ROW's code (so a mistyped prefix in the tag still resolves to the true
  canonical code). Falls back to the parsed string when the number isn't
  in MC-2 (``matched=False``) — historical meetings about archived/deleted
  projects keep routing exactly as fathom's local parse always did.
- **Initiative slug** — ``"mission-control"`` → itself. Lowercase slug, no
  digit prefix. DB-verified against ``initiatives.code`` when possible.

The ``untagged`` sentinel and malformed strings resolve to ``None``.
"""
from __future__ import annotations

from cp_engine.mc2_db import Tables
import re
from typing import Any

# The one copy of the string heuristic (was duplicated in fathom's
# projectTagToCode — keep the semantics identical to what the dashboard's
# tags were built for).
_ENGAGEMENT_TAG_RE = re.compile(r"^([A-Za-z]{3})[\s-]?(\d{3,5})")
_SLUG_TAG_RE = re.compile(r"^[a-z][a-z0-9-]+$")


def parse_tag(tag: Any) -> dict | None:
    """Pure string parse of one tag. Returns
    ``{"shape": "engagement", "prefix", "number", "code"}`` or
    ``{"shape": "slug", "code"}`` or None (unparseable / untagged sentinel).
    """
    if not isinstance(tag, str):
        return None
    trimmed = tag.strip()
    if not trimmed:
        return None

    m = _ENGAGEMENT_TAG_RE.match(trimmed)
    if m:
        prefix, number = m.group(1).lower(), int(m.group(2))
        return {
            "shape": "engagement",
            "prefix": prefix,
            "number": number,
            "code": f"{prefix}-{number}",
        }

    if _SLUG_TAG_RE.match(trimmed) and not trimmed.startswith("untagged"):
        return {"shape": "slug", "code": trimmed}

    return None


def resolve_tags(client: Any, tags: list) -> list[dict]:
    """Resolve display tags to canonical codes, DB-verified against MC-2.

    ``client`` is a Supabase client (or None — pure-parse mode). Returns one
    entry per input tag: ``{"tag", "code", "kind", "matched"}`` where
    ``code`` is None for unresolvable tags, ``kind`` is
    ``"project" | "initiative" | None`` and ``matched`` says whether the
    code was verified against a live MC-2 row (False = parse-only fallback,
    the historical fathom behavior).
    """
    codes_by_number, initiative_codes = _load_indexes(client)

    out: list[dict] = []
    for tag in tags:
        parsed = parse_tag(tag)
        if parsed is None:
            out.append({"tag": tag, "code": None, "kind": None, "matched": False})
            continue

        if parsed["shape"] == "engagement":
            number = parsed["number"]
            db_code = codes_by_number.get(number)
            if db_code:
                out.append({"tag": tag, "code": db_code, "kind": "project", "matched": True})
            else:
                # Number unknown to MC-2 (archived/deleted project, or DB
                # unavailable) — fall back to the parsed code, preserving
                # fathom's historical parse-only routing.
                out.append({"tag": tag, "code": parsed["code"], "kind": "project", "matched": False})
            continue

        # Slug shape: initiative (or standalone-repo) code.
        matched = parsed["code"] in initiative_codes
        out.append({
            "tag": tag,
            "code": parsed["code"],
            "kind": "initiative",
            "matched": matched,
        })
    return out


def _load_indexes(client: Any) -> tuple[dict, set]:
    """Load (codes_by_number, initiative_codes) from MC-2.

    Best-effort: any failure returns empty indexes, degrading resolve_tags
    to pure-parse mode (matched=False everywhere) rather than erroring the
    dispatch path.
    """
    codes_by_number: dict[int, str] = {}
    initiative_codes: set = set()
    if client is None:
        return codes_by_number, initiative_codes

    try:
        resp = (
            client.table(Tables.PROJECTS)
            .select("number, companies(code)")
            .execute()
        )
        for row in resp.data or []:
            number = row.get("number")
            company = (row.get("companies") or {}).get("code")
            if number is None or not company:
                continue
            codes_by_number[int(number)] = f"{company.lower()}-{number}"
    except Exception:  # noqa: BLE001 — degrade to parse-only, never block dispatch
        codes_by_number = {}

    try:
        resp = client.table(Tables.INITIATIVES).select("code").execute()
        initiative_codes = {
            row["code"] for row in (resp.data or []) if row.get("code")
        }
    except Exception:  # noqa: BLE001
        initiative_codes = set()

    return codes_by_number, initiative_codes
