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
                "source — add_element_source on the `cp-hosted` connector "
                "closes the loop")
    return out


# Canon size target (spec v04 §2) — mirrors the hosted verb's warn threshold.
CANON_TARGET_MAX = 7


def lint_lifecycle(rows: list[dict], relations: list[dict]) -> list[str]:
    """Spec-v04 lifecycle checks (cp-engine #149) over live rows + edges.

    `relations` are the project's ACTIVE `canon_of` / `absorbed_by` /
    `supersedes` edges (kind, from_item_id, to_item_id). Three checks:

      4. canon larger than the ≤7 target — promotion should displace
         (`replaces_key`), not accrete;
      5. an element sealed into a deliverable (`absorbed_by`) yet still
         bound `serves`-ing live work — historical material driving a live
         work item is usually a seal that fired too early or a binding that
         outlived delivery;
      6. a stale canon member — one that is itself absorbed into a
         deliverable, or that a `supersedes` edge points at. (NOT a
         date comparison against the brief: any brief re-version would
         instantly flag every member, and noisy lint is dead lint.)

    Pure and warn-only, like every check here.
    """
    out: list[str] = []
    by_id = {r.get("est_item_id"): r for r in rows}

    canon_ids = [e.get("from_item_id") for e in relations
                 if e.get("kind") == "canon_of"]
    absorbed = {e.get("from_item_id"): e.get("to_item_id")
                for e in relations if e.get("kind") == "absorbed_by"}

    if len(canon_ids) > CANON_TARGET_MAX:
        out.append(
            f"⚠ canon oversized: {len(canon_ids)} members against the "
            f"≤{CANON_TARGET_MAX} target — scarcity is the feature; displace "
            "with promote_to_canon(replaces_key=…) rather than accreting")

    for eid, deliverable in absorbed.items():
        row = by_id.get(eid)
        if row is None:
            continue
        if (row.get("binding") or "") == "live" and (row.get("serves") or []):
            title = row.get("framing") or eid
            out.append(
                f"⚠ absorbed-but-serving: '{title}' ({eid}) is sealed into "
                f"{deliverable} yet still serves live work — either the seal "
                "fired early or the binding outlived delivery")

    superseded = {e.get("to_item_id") for e in relations
                  if e.get("kind") == "supersedes"}
    for eid in canon_ids:
        title = (by_id.get(eid) or {}).get("framing") or eid
        if eid in absorbed:
            out.append(
                f"⚠ stale canon member: '{title}' ({eid}) is sealed into "
                f"{absorbed[eid]} yet still sits in the canon — displace it "
                "(promote its deliverable or successor with replaces_key)")
        elif eid in superseded:
            out.append(
                f"⚠ stale canon member: '{title}' ({eid}) has a supersedes "
                "edge pointing at it — promote the successor with "
                "replaces_key so the canon tracks current truth")
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
