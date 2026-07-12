"""Warn-only spine lint (cp-engine #69).

Mechanical hygiene checks that otherwise survive indefinitely because nothing
scans for them (all three were found in one sap-5174 read-through):

  1. an element flagged `important` yet unbound with nothing served — a
     standing flag on a floating note usually means "answered, never filed";
  2. an Agreement whose body still says "attach as source" while its
     `sources` array is empty — an instruction the element wrote to itself
     and never executed;
  3. scaffold template placeholders (`- _<...>_`) still sitting in `cp.md`.

Pure functions: rows/text in, display-ready warning strings out. WARN ONLY —
callers echo the lines and always exit 0; the lint never auto-fixes and never
blocks a wrap-up. Surfaced by `cp spine-lint` (run per touched project at
`wrap up`, alongside word-count discipline).
"""

from __future__ import annotations

import re

# Mirrors ``sprints._TEMPLATE_PLACEHOLDER_RE`` (the `- _<...>_` scaffold
# bullet), kept in sync by shape like the open-ask regexes are.
_PLACEHOLDER_RE = re.compile(r"^\s*-\s+_<[^>]+>_\s*$", re.MULTILINE)

# The self-instruction an Agreement body carries while its signed doc is
# still unattached (see agreement_projection.sow_attach_nudge — same loop,
# write side).
_ATTACH_INSTRUCTION_RE = re.compile(r"attach(?:ed)?\s+as\s+(?:a\s+)?source",
                                    re.IGNORECASE)


def lint_spine_rows(rows: list[dict]) -> list[str]:
    """Checks 1 + 2 over live spine rows.

    `rows` need `est_item_id, framing, layer, binding, serves, important,
    body, sources` (the SPINE_LINT_COLUMNS shape). Returns one display-ready
    warning per finding; [] when clean.
    """
    out: list[str] = []
    for row in rows:
        eid = row.get("est_item_id") or "(unknown)"
        title = row.get("framing") or eid
        serves = row.get("serves") or []
        if (bool(row.get("important"))
                and (row.get("binding") or "") == "unbound"
                and len(serves) == 0):
            out.append(
                f"⚠ important-but-floating: '{title}' ({eid}) is flagged "
                "important yet unbound and serves nothing — file it "
                "(bind/serves), version it with its answer, or retire it")
        if ((row.get("layer") or "").lower() == "agreement"
                and _ATTACH_INSTRUCTION_RE.search(row.get("body") or "")
                and not (row.get("sources") or [])):
            out.append(
                f"⚠ unexecuted attach-instruction: Agreement '{title}' "
                f"({eid}) says \"attach as source\" but has no attached "
                "source — add_element_source closes the loop")
    return out


def lint_cp_placeholders(cp_md_text: str) -> list[str]:
    """Check 3: scaffold placeholders still in a `cp.md`.

    One warning naming the count + first placeholder, not one per line —
    a pristine section reads as one finding, not twelve.
    """
    hits = _PLACEHOLDER_RE.findall(cp_md_text or "")
    if not hits:
        return []
    first = hits[0].strip()
    more = f" (+{len(hits) - 1} more)" if len(hits) > 1 else ""
    return [f"⚠ scaffold placeholders: cp.md still carries {len(hits)} "
            f"template bullet(s), e.g. `{first}`{more} — write a real line "
            "or note that state lives in the Exec Summary + spine"]
