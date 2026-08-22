#!/usr/bin/env python3
"""PreToolUse guard for engine-managed regions.

`CLAUDE.md` states that content between `<!-- cp-engine:start <name> -->` and
`<!-- cp-engine:end <name> -->` "MUST NOT be edited — sync owns them." That
was an instruction only: instructions are context, not enforcement, and the
tenant carries 2,111 such regions across 574 files. A single bad splice is
silently reverted on the next sync — or worse, fights it (see #180, where
sync silently reverted hosted-MCP edits).

This hook makes the rule real. It inspects Edit/Write payloads and blocks any
whose target text lands inside an engine-managed region. Distributed into
tenants by `cp sync` (claude_settings.install_into_tenant) — edit the
packaged copy at src/cp_engine/hooks/, never the tenant-side mirror.

Contract (Claude Code PreToolUse):
- stdin carries JSON: {"tool_name": ..., "tool_input": {...}}
- exit 0 → allow. exit 2 → BLOCK, with stderr shown to the model.
- Any other exit is treated as a non-blocking error.

Design notes:
- FAIL OPEN. Every unexpected condition — unreadable file, malformed JSON,
  unknown schema — exits 0. A guard that blocks work because it couldn't
  parse its own input is worse than no guard: it would make the engine
  un-editable the moment a payload shape changed.
- The escape hatch is `cxp write-region`, which is how the engine itself
  splices these regions. The block message names it, so the model has a
  correct next action rather than a dead end.
- No third-party imports: runs under bare system python3, same as the
  version-check hook.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_START = re.compile(r"<!--\s*cp-engine:start\s+([\w-]+)\s*-->")
_END = re.compile(r"<!--\s*cp-engine:end\s+([\w-]+)\s*-->")

# Regions that are marker-wrapped but AUTHORED, not engine-owned.
#
# `exec-summary` is the deliberate exception to "sync owns the markers": the
# engine only scaffolds the region and migrates the old Quick Resume into it,
# then reads it for `/cp-prep`. The six fields are written by a human or the
# model at `wrap up` — which is exactly what `/cp-wrapup` step 1 instructs
# ("Edit directly between the `exec-summary` markers"). Guarding it blocks
# the single most common legitimate edit in the tenant.
#
# The `**Last session:**` line inside it IS derived, but it self-heals on the
# next `cp sync`, so a stray edit there costs nothing and does not justify
# blocking the whole region.
_AUTHORED_REGIONS = frozenset({"exec-summary"})

# Tools whose payloads can modify a file's bytes.
_GUARDED_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})


def _regions(text: str) -> list[tuple[int, int, str]]:
    """(start_offset, end_offset, name) for each GUARDED region.

    Authored regions (`_AUTHORED_REGIONS`) are excluded here rather than at
    each call site, so every path — Edit, MultiEdit, and Write's
    region-preservation check — honors the exemption identically.
    """
    out: list[tuple[int, int, str]] = []
    for m in _START.finditer(text):
        name = m.group(1)
        if name in _AUTHORED_REGIONS:
            continue
        end = None
        for e in _END.finditer(text, m.end()):
            if e.group(1) == name:
                end = e
                break
        if end is not None:
            out.append((m.start(), end.end(), name))
    return out


def _hits_region(text: str, needle: str) -> str | None:
    """Name of the region `needle` falls inside, or None.

    Only an *exact* occurrence counts. A needle appearing in several places
    blocks if ANY occurrence is inside a region — the edit would be ambiguous
    anyway, and refusing is the safe read.
    """
    if not needle:
        return None
    regions = _regions(text)
    if not regions:
        return None
    pos = text.find(needle)
    while pos != -1:
        for start, end, name in regions:
            if pos < end and pos + len(needle) > start:
                return name
        pos = text.find(needle, pos + 1)
    return None


def _check(tool: str, payload: dict) -> str | None:
    """Region name this call would damage, or None to allow."""
    raw_path = payload.get("file_path") or payload.get("notebook_path")
    if not raw_path:
        return None

    path = Path(str(raw_path))
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None  # new file, binary, unreadable — nothing to protect

    # Write replaces the whole file: block if the CURRENT file has regions
    # and the replacement would not preserve them.
    if tool == "Write":
        current = {n for _, _, n in _regions(text)}
        if not current:
            return None
        replacement = str(payload.get("content") or "")
        kept = {n for _, _, n in _regions(replacement)}
        missing = current - kept
        return sorted(missing)[0] if missing else None

    # Edit / MultiEdit: check each old_string against the live regions.
    edits = payload.get("edits")
    if isinstance(edits, list):
        for e in edits:
            if isinstance(e, dict):
                hit = _hits_region(text, str(e.get("old_string") or ""))
                if hit:
                    return hit
        return None

    return _hits_region(text, str(payload.get("old_string") or ""))


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return 0  # fail open

    tool = str(event.get("tool_name") or "")
    if tool not in _GUARDED_TOOLS:
        return 0

    payload = event.get("tool_input")
    if not isinstance(payload, dict):
        return 0

    try:
        hit = _check(tool, payload)
    except Exception:  # noqa: BLE001 — a guard must never crash a session
        return 0

    if hit is None:
        return 0

    sys.stderr.write(
        f"BLOCKED: this edit lands inside the engine-managed region "
        f"'{hit}'. `cxp sync` owns that region and will revert or fight any "
        f"hand-edit (see cp-engine #180).\n\n"
        f"Write outside the cp-engine:start/end markers instead. If the "
        f"region's CONTENT genuinely must change, that is an engine change — "
        f"use `cxp write-region`, or fix the source the region is rendered "
        f"from.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
