"""Resolve fathom-meeting-sync display tags to canonical cp project codes.

THE single owner of the tag→code heuristic (arch-phase-2). fathom-meeting-sync
historically re-implemented this parse as ``projectTagToCode`` in
``auto-ingest-trigger.js``; it now calls the cp-engine-webhook's
``POST /api/resolve-tags`` (which wraps :func:`resolve_tags`), keeping its
local parse only as a fallback for when the webhook is unreachable.

One tag shape since #301: any spelling `cp_engine.codes.parse_code` accepts
— ``"GGL 5136 go/safety website"``, ``"ggl-5136"``,
``"1pi-9005-mission-control"``. Resolution is DB-backed where possible: the
number is looked up in MC-2 ``projects`` and the code is rebuilt from the
COMPANY ROW's code (so a mistyped prefix in the tag still resolves to the
true canonical code). Falls back to the parsed short form when the number
isn't in MC-2 (``matched=False``) — historical meetings about archived /
deleted projects keep routing exactly as fathom's local parse always did.

A tag with no job number (``"storyos"``, the ``untagged`` sentinel, a
malformed string) resolves to ``None``: every workstream carries a number.
"""
from __future__ import annotations

from typing import Any

from cp_engine.codes import canonical_code, parse_code
from cp_engine.mc2_db import Tables


def parse_tag(tag: Any) -> dict | None:
    """Pure string parse of one tag. Returns
    ``{"prefix", "number", "code"}`` (``code`` is the ``<prefix>-<number>``
    short form) or None (unparseable / no job number / untagged sentinel).
    """
    parsed = parse_code(tag) if isinstance(tag, str) else None
    if parsed is None:
        return None
    return {
        "prefix": parsed.company,
        "number": parsed.number,
        "code": canonical_code(parsed) if parsed.slug is None else parsed.short,
    }


def resolve_tags(client: Any, tags: list) -> list[dict]:
    """Resolve display tags to canonical codes, DB-verified against MC-2.

    ``client`` is a Supabase client (or None — pure-parse mode). Returns one
    entry per input tag: ``{"tag", "code", "kind", "matched"}`` where
    ``code`` is None for unresolvable tags, ``kind`` is ``"project"`` for
    every resolved tag (the response shape fathom reads; one entry kind
    since #301) and ``matched`` says whether the code was verified against
    a live MC-2 row (False = parse-only fallback, the historical fathom
    behavior).
    """
    codes_by_number = _load_indexes(client)

    out: list[dict] = []
    for tag in tags:
        parsed = parse_tag(tag)
        if parsed is None:
            out.append({"tag": tag, "code": None, "kind": None, "matched": False})
            continue
        db_code = codes_by_number.get(parsed["number"])
        if db_code:
            out.append({"tag": tag, "code": db_code, "kind": "project", "matched": True})
        else:
            # Number unknown to MC-2 (archived/deleted project, or DB
            # unavailable) — fall back to the parsed code, preserving
            # fathom's historical parse-only routing.
            out.append({"tag": tag, "code": parsed["code"], "kind": "project", "matched": False})
    return out


def _load_indexes(client: Any) -> dict[int, str]:
    """Load ``{number: "<company>-<number>"}`` from MC-2.

    Best-effort: any failure returns an empty index, degrading resolve_tags
    to pure-parse mode (matched=False everywhere) rather than erroring the
    dispatch path. Every row is indexed — internal workstreams are ingest
    destinations with working dirs like any other (#301; the #221 skip
    guarded against pseudo-rows that mig 192 deleted).
    """
    codes_by_number: dict[int, str] = {}
    if client is None:
        return codes_by_number

    try:
        resp = client.table(Tables.PROJECTS).select("number, companies(code)").execute()
        for row in resp.data or []:
            number = row.get("number")
            company = (row.get("companies") or {}).get("code")
            if number is None or not company:
                continue
            codes_by_number[int(number)] = f"{company.lower()}-{number}"
    except Exception:  # noqa: BLE001 — degrade to parse-only, never block dispatch
        codes_by_number = {}

    return codes_by_number
