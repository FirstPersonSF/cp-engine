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
      7. a dead-end activity (#163) — an active Activity with no outgoing
         `informs`/`derives_from` edge to any deliverable, so the stream
         bound to it is unreachable from the work it fed. Absorbed
         activities are exempt: they are finished by definition.

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

    # 7. Dead-end activity (#163): an active Activity with no outgoing edge to
    #    any deliverable. Its stream — the sources, meetings and decisions
    #    bound to it — is then unreachable from the work it was supposed to
    #    feed, which is how a discovery activity quietly stops counting.
    #
    #    Read via `informs` / `derives_from` rather than a dedicated `feeds`
    #    kind: those two ALREADY carry activity -> deliverable, many-to-many,
    #    and are what sealFeeders offers at seal time. A third near-synonym in
    #    a seven-kind vocabulary would split the same meaning across three
    #    edges and make the picker worse.
    #
    #    Absorbed activities are exempt — they are finished by definition, and
    #    flagging them would make every sealed round noisier than the last.
    feeds_out: dict[str, list[str]] = {}
    for e in relations:
        if e.get("kind") not in ("informs", "derives_from"):
            continue
        feeds_out.setdefault(e.get("from_item_id"), []).append(e.get("to_item_id"))

    deliverable_ids = {
        r.get("est_item_id") for r in rows
        if _norm_layer(r.get("layer")) in ("deliverables", "output")
    }
    for row in rows:
        if _norm_layer(row.get("layer")) != "activity":
            continue
        eid = row.get("est_item_id")
        if eid in absorbed:
            continue
        if any(t in deliverable_ids for t in feeds_out.get(eid, [])):
            continue
        title = row.get("framing") or eid
        out.append(
            f"⚠ dead-end activity: '{title}' ({eid}) feeds no deliverable — "
            "its sources and decisions are unreachable from the work they "
            "informed; add an informs/derives_from edge to the deliverable "
            "it fed, or seal it if it is finished")

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


# ── Curation checks (#112 P3 + #158 gaps 2–4) ────────────────────────────

# The standing front-door elements (#112 P3): a Brief that is still the
# scaffold weeks in means the spine has no front door and nothing says so.
_STANDING_BRIEF_RE = re.compile(
    r"inputs\s*&?\s*briefing|statement\s+of\s+work|^sow\b", re.IGNORECASE)
# Below this the "Brief" is a title + pointer, not an authored brief.
BRIEF_MIN_CHARS = 300

# Date tokens a time-bound card carries in its framing ("feedback on monday
# 7-27", "workshop 2026-07-30"): ISO first, then M/D or M-D shapes.
_FRAMING_DATE_RE = re.compile(
    r"\b(?:(?P<iso>\d{4}-\d{2}-\d{2})|(?P<m>\d{1,2})[/-](?P<d>\d{1,2}))\b")
STALE_AFTER_DAYS = 7

# Raw-capture size ceiling (#158 gap 3) on layers whose value IS the
# distillation — a 16KB ClientFeedback card is a paste, not a distillation.
# Layer values drift between CamelCase (code) and spaced ("Client feedback",
# the live DB shape) — compare normalized.
DISTILL_LAYERS = frozenset({"clientfeedback", "synthesis", "decisions"})
DISTILL_BODY_MAX = 10_000


def _norm_layer(layer) -> str:
    return re.sub(r"[^a-z]", "", str(layer or "").lower())

# Instruction-shaped framing (#158 gap 4): a pasted prompt, not a title.
_INSTRUCTION_FRAMING_RE = re.compile(
    r"\byou\s+(?:can|should|will|need)\b|^please\s|\bcapture\s+our\b",
    re.IGNORECASE)


def lint_curation(rows: list[dict], *, today=None) -> list[str]:
    """Curation drift over live rows (#112 P3, #158 gaps 2–4). Pure, warn-only.

    Needs the SPINE_LINT_COLUMNS shape plus `version_date`.
    """
    from datetime import date as _date

    today = today or _date.today()
    out: list[str] = []
    for row in rows:
        eid = row.get("est_item_id") or "(unknown)"
        title = row.get("framing") or eid
        body = row.get("body") or ""
        layer = row.get("layer")

        # #112 P3 — standing Brief still the scaffold.
        if ((_norm_layer(layer) == "brief"
                or _STANDING_BRIEF_RE.search(row.get("framing") or ""))
                and (len(body) < BRIEF_MIN_CHARS or _PLACEHOLDER_RE.search(body))):
            out.append(
                f"⚠ unauthored standing Brief: '{title}' ({eid}) is still a "
                f"{len(body)}-char scaffold — the spine has no front door "
                "until it's authored (draft-and-confirm; never auto-written)")

        # #158 gap 2 — time-bound card whose moment has passed, untouched since.
        stale_date = _past_framing_date(row.get("framing") or "", today)
        if stale_date is not None:
            moved = (row.get("version_date") or "") >= stale_date.isoformat()
            if not moved:
                out.append(
                    f"⚠ time-bound and past: '{title}' ({eid}) references "
                    f"{stale_date.isoformat()} ({(today - stale_date).days}d "
                    "ago) and hasn't been versioned since — capture the "
                    "outcome or retire it")

        # #158 gap 3 — raw paste on a distillation layer.
        if _norm_layer(layer) in DISTILL_LAYERS and len(body) > DISTILL_BODY_MAX:
            out.append(
                f"⚠ undistilled capture: '{title}' ({eid}) is "
                f"{len(body):,} chars on the {layer} layer — distill into "
                "the card and attach the raw text as a source instead")

        # #158 gap 4 — unlayered, or framing that is a pasted instruction.
        if layer is None:
            out.append(
                f"⚠ unlayered element: '{title}' ({eid}) has layer: null — "
                "the UI can't file it; set a layer")
        if _INSTRUCTION_FRAMING_RE.search(row.get("framing") or ""):
            out.append(
                f"⚠ instruction-shaped framing: '{title}' ({eid}) reads as a "
                "pasted prompt, not a title — reframe it as what the card "
                "holds")
    return out


def _past_framing_date(framing: str, today) -> "object | None":
    """Newest date token in a framing that is ≥STALE_AFTER_DAYS past, or None.

    M/D shapes assume the current year (and the prior year when that lands
    in the future — a December card read in January must not flag)."""
    from datetime import date as _date

    best = None
    for m in _FRAMING_DATE_RE.finditer(framing):
        try:
            if m.group("iso"):
                d = _date.fromisoformat(m.group("iso"))
            else:
                month, day = int(m.group("m")), int(m.group("d"))
                d = _date(today.year, month, day)
                if d > today:
                    d = _date(today.year - 1, month, day)
        except ValueError:
            continue
        if best is None or d > best:
            best = d
    if best is not None and (today - best).days >= STALE_AFTER_DAYS:
        return best
    return None


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
