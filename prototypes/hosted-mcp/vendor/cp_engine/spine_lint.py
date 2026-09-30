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

from cp_engine.clock import tenant_today

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
            # #319: the remedy names only moves that clear the predicate. The
            # old text also offered "version it with its answer", which the
            # check never reads — two elements were versioned with their
            # answers and kept warning (08-25). A typed edge to another element
            # does not clear it either (08-27): an edge is not a binding.
            out.append(
                f"⚠ important-but-floating: '{title}' ({eid}) is flagged "
                "important yet unbound and serves nothing — bind it to the "
                "work it serves (set_spine_element serves=[…]), clear the "
                "important flag if it has been answered, or retire it; a new "
                "version or a typed edge does not clear this")
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
    dead_ends: list[tuple[str, str]] = []
    for row in rows:
        if _norm_layer(row.get("layer")) != "activity":
            continue
        eid = row.get("est_item_id")
        if eid in absorbed:
            continue
        if any(t in deliverable_ids for t in feeds_out.get(eid, [])):
            continue
        dead_ends.append((row.get("framing") or eid, eid))

    # #319: a project with NO deliverable element reported this as N dead-end
    # activities that all shared one cause — four projects on 08-25, where the
    # obvious move (wire each activity to *some* deliverable) was impossible
    # because there was nothing to wire to. One direct finding names the cause;
    # the per-activity lines are folded into it rather than repeated.
    if dead_ends and not deliverable_ids:
        names = ", ".join(f"'{t}'" for t, _ in dead_ends[:3])
        more = f" (+{len(dead_ends) - 3} more)" if len(dead_ends) > 3 else ""
        out.append(
            f"⚠ no deliverable element: the spine has nothing on the "
            f"Deliverables layer, so {len(dead_ends)} active "
            f"{'activity reads as a dead end' if len(dead_ends) == 1 else 'activities read as dead ends'}"
            f" ({names}{more}) — add a card for the thing being "
            "made, then wire each activity to it with informs/derives_from")
        dead_ends = []

    for title, eid in dead_ends:
        # #319: the predicate is right and the old wording was not. #163's
        # intent is that an activity's stream be reachable from the DELIVERABLE
        # it fed, so an edge to a decision or a note does not close it — the
        # 08-21 slt-5196 case wired `informs` to a Decisions element and the
        # warning (correctly) kept firing. Say what the edge must point AT.
        out.append(
            f"⚠ dead-end activity: '{title}' ({eid}) feeds no deliverable — "
            "its sources and decisions are unreachable from the work they "
            "informed. Add an informs/derives_from edge FROM it TO an element "
            "on the Deliverables layer (an edge to a decision or note does not "
            "count), or, if it is finished, seal it into that deliverable")

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

# #177 — framings that describe an EVENT or a REACTION rather than an
# artifact. Every phrase here was observed on the live Deliverables layer:
# "Kick off and direction for Geoff Ahmann", "Clarification on the
# deliverable for Mehul", "Initial feedback on Derek's designs", "AI campaign
# direction workshop (decision gate)". Anchored to the START of the framing —
# a real deliverable may well mention feedback ("Response to Platform
# Narrative Feedback") without BEING feedback.
_NOT_A_DELIVERABLE_RE = re.compile(
    r"^\s*(?:initial\s+)?(?:feedback\b|notes?\s+(?:from|on)\b|"
    r"clarification\b|kick[\s-]?off\b|debrief\b|recap\b|"
    r"(?:post[\s-])?meeting\b|discussion\b|reactions?\s+to\b)",
    re.IGNORECASE)

# A deliverable at its first version carrying less than this is thin.
# Deliberately well under BRIEF_MIN_CHARS: the fat v1s in the tenant
# (2.3k–24k chars) are genuine deliverables that shipped once.
STUB_BODY_MAX = 400
_FIRST_VERSION_RE = re.compile(r"^\s*v?0*1\s*$", re.IGNORECASE)

# ...but thin is not the same as empty. A POINTER card is legitimate: it
# summarises a deliverable whose substance lives in a file or an attached
# source ("Delivered 6/12. See synthesis-docs/storyos_campaign_brief.md.").
# The first live run flagged seven of these on ibx-5153 — all real,
# delivered work. What is actually wrong is a thin card that points NOWHERE.
_POINTER_RE = re.compile(
    r"\bsee\s+\S+|\.(?:md|docx?|pptx?|pdf|xlsx?)\b|https?://|"
    r"\bdeliver(?:ed|y)\b|\bdue\b|\bdraft\b",
    re.IGNORECASE)


def _is_deliverable_row(row: dict) -> bool:
    """True when this row is a deliverable — explicit `card_kind` preferred."""
    from cp_engine.card_class import CardKind, classify, classify_is_inferred

    if not classify_is_inferred(row):
        return classify(row) is CardKind.DELIVERABLE
    return _norm_layer(row.get("layer")) in ("deliverables", "output")


def _version_depth(label) -> int:
    """The number in a version label (v7 / V1 / 1 / v01); 0 when unparseable."""
    m = re.search(r"\d+", str(label or ""))
    return int(m.group(0)) if m else 0


def _first_version(label) -> bool:
    """True for v1 / V1 / 1 / v01 — a deliverable that never moved."""
    return bool(_FIRST_VERSION_RE.match(str(label or "")))


# #176 — a live card's prose names its successor by TITLE, not by id:
#   > **[HISTORICAL — superseded by `SRS Arc B — v08→r01 build delta`]**
# An id-only reference check would miss the very case the issue was filed
# about, so framings are matched too. Only titles this long are matched — a
# short framing ("Notes", "Marcello Grande") collides with ordinary prose and
# would fire on every card that happened to use the word.
REFERENCE_TITLE_MIN_CHARS = 18


def lint_curation(rows: list[dict], *, today=None,
                  has_agreement: bool | None = None) -> list[str]:
    """Curation drift over live rows (#112 P3, #158 gaps 2–4). Pure, warn-only.

    Needs the SPINE_LINT_COLUMNS shape plus `version_date`.

    `has_agreement` is whether the workstream carries an agreement — the same
    `deal_stage IS NOT NULL` signal `state.derive_label` is fed. The standing
    Brief/SOW check runs only when it does: an initiative has no agreement, an
    account never carries one (v0.124.3), and neither does a program that is
    only a grouping — there is no signed SOW to author and the warning could
    never be satisfied (#319). It is keyed on the agreement, not the label,
    because a program that carries its OWN agreement still owes a SOW, and its
    label ("program" wins over "job") would hide that. `None` (lookup failed)
    keeps the check, so an unresolved project is linted as before.
    """

    today = today or tenant_today()
    out: list[str] = []
    for row in rows:
        eid = row.get("est_item_id") or "(unknown)"
        title = row.get("framing") or eid
        body = row.get("body") or ""
        layer = row.get("layer")

        # #112 P3 — standing Brief still the scaffold. Only where there is an
        # agreement (#319): mission-control carried a permanent, unsatisfiable
        # warning for a scaffolded SOW on internal work with no client and no
        # agreement, and each reader re-investigated it. Accounts and
        # agreement-less programs are the same shape.
        if (has_agreement is not False
                and (_norm_layer(layer) == "brief"
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
        #
        # #319: length alone cannot tell a paste from a card that is long
        # because it SAYS a lot. slt-5196's `what-winning-means-by-role` was
        # 10.8k chars at v7 with four sources attached — authored analysis,
        # restructured on direction — and "distill it" would have destroyed
        # work. A card that has been re-versioned AND carries attached sources
        # is authored, not captured, so it is left alone. The remedy also
        # splits: with nothing attached, "attach the raw text as a source" was
        # impossible (ibx-5192, sap-5174 — the card was the ONLY copy), so the
        # message says to land the raw text as a source first.
        n_sources = len(row.get("sources") or [])
        authored = n_sources > 0 and _version_depth(row.get("version_label")) > 1
        if (_norm_layer(layer) in DISTILL_LAYERS and len(body) > DISTILL_BODY_MAX
                and not authored):
            if n_sources:
                remedy = (f"distill into the card; its {n_sources} attached "
                          f"source{'' if n_sources == 1 else 's'} already "
                          "hold the raw material")
            else:
                remedy = ("this card may be the only copy of the raw text — "
                          "land it as a source first (push_to_dropbox, "
                          "ingest, then add_element_source), then distill "
                          "the card")
            out.append(
                f"⚠ undistilled capture: '{title}' ({eid}) is "
                f"{len(body):,} chars on the {layer} layer — {remedy}")

        # #177 — CLASSIFICATION, not content. The Deliverables layer is what a
        # cold reader trusts to answer "what are we making?", and on ibx-5192
        # three of five cards on it were something else. Both checks below are
        # scoped tightly on purpose: 13 of the tenant's 19 shipped deliverables
        # sit at v1, so "never versioned" alone would flag most of the layer
        # and teach the reader to skim past the lint.
        # #179: the same question `seal_sweep._is_deliverable` asks. The
        # duplicated string list here is what #172 broke; prefer the explicit
        # `card_kind` and keep the strings as the pre-field fallback.
        if _is_deliverable_row(row):
            # A deliverable names an artifact; these name an EVENT or a
            # REACTION to one. "Kick off and direction for Geoff Ahmann" and
            # "Clarification on the deliverable for Mehul" sat here for a
            # month; "Initial feedback on Derek's designs" still does.
            if _NOT_A_DELIVERABLE_RE.search(row.get("framing") or ""):
                out.append(
                    f"⚠ misfiled on Deliverables: '{title}' ({eid}) reads as "
                    "a meeting, a note, or feedback — not an artifact we are "
                    "making. Reclassify (Note / Activity / Client feedback); "
                    "the Deliverables layer is what a cold reader trusts")
            # A thin card at v1 that points nowhere: no substance, no file,
            # no attached source, no delivery language. A pointer card is
            # fine — the work lives elsewhere and the card says where.
            elif (_first_version(row.get("version_label"))
                    and len(body) < STUB_BODY_MAX
                    and not (row.get("sources") or [])
                    and not _POINTER_RE.search(body)):
                out.append(
                    f"⚠ empty deliverable: '{title}' ({eid}) is "
                    f"{len(body)} chars at {row.get('version_label')} with "
                    "no attached source and no pointer to where the work "
                    "lives — say what it is, attach it, or move it off the "
                    "Deliverables layer")

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


def lint_archived_referrers(
    live_rows: list[dict],
    archived_rows: list[dict],
    relations: list[dict],
) -> list[str]:
    """Live elements that point at something ARCHIVED (#176).

    Archiving says "this shouldn't exist". When a live card names an archived
    one, the reader is told where to go next and the destination is hidden —
    found in use on ibx-5192, where a HISTORICAL banner names its successor
    and that successor is archived.

    Three reference shapes, because the real cases use different ones:

      - an **active typed edge** to or from the archived element;
      - the archived element's **est_item_id** in a live body or `sources`;
      - the archived element's **framing** quoted in a live body — which is
        how the motivating case is actually written (``superseded by `SRS
        Arc B — v08→r01 build delta` ``), so an id-only check would miss it.

    Warn-only and never a block: archiving a referenced element is sometimes
    right. It should be a decision, not a surprise.
    """
    if not archived_rows:
        return []

    live_by_id = {r.get("est_item_id"): r for r in live_rows if r.get("est_item_id")}
    edges_by_endpoint: dict[str, set[str]] = {}
    for e in relations:
        for near, far in (("from_item_id", "to_item_id"),
                          ("to_item_id", "from_item_id")):
            a, b = e.get(near), e.get(far)
            if a and b and b in live_by_id:
                edges_by_endpoint.setdefault(a, set()).add(b)

    out: list[str] = []
    seen: set[str] = set()
    for arch in archived_rows:
        eid = arch.get("est_item_id")
        if not eid or eid in seen or eid in live_by_id:
            # `eid in live_by_id` is the half-archived case: some versions
            # archived, some live. That is its own defect (below), not a
            # dangling reference — the element is still readable.
            continue
        seen.add(eid)
        title = arch.get("framing") or eid

        referrers: list[str] = []
        for ref_id in sorted(edges_by_endpoint.get(eid, ())):
            referrers.append(f"{live_by_id[ref_id].get('framing') or ref_id} (edge)")
        # A live card that ATTACHED the same document is not referring to the
        # archived stub of that document — it holds the provenance directly,
        # which is the outcome the migration wants. Two live findings on
        # ibx-5192 were exactly this: Kimber's card says "Resources attached
        # as sources: … SRS Block Details · Platform Media Coverage", naming
        # its OWN attachments, and both stubs' rag_assets already sit in its
        # `sources`. Flagging that told the reader to un-archive a stub whose
        # only content was a pointer the live card already has.
        arch_asset_ids = {
            str(s.get("id")) for s in (arch.get("sources") or [])
            if isinstance(s, dict) and s.get("id")
        }
        for ref_id, row in live_by_id.items():
            body = row.get("body") or ""
            row_asset_ids = {
                str(s.get("id")) for s in (row.get("sources") or [])
                if isinstance(s, dict) and s.get("id")
            }
            # Provenance already transferred — nothing dangles.
            if arch_asset_ids and arch_asset_ids <= row_asset_ids:
                continue
            hit = eid in body or eid in str(row.get("sources") or "")
            if (not hit and len(str(title)) >= REFERENCE_TITLE_MIN_CHARS
                    and str(title) in body):
                hit = True
            if hit:
                label = f"{row.get('framing') or ref_id} (names it)"
                if label not in referrers:
                    referrers.append(label)
        if not referrers:
            continue

        shown = "; ".join(referrers[:3])
        more = f" (+{len(referrers) - 3} more)" if len(referrers) > 3 else ""
        out.append(
            f"⚠ archived but still referenced: '{title}' ({eid}) is archived, "
            f"yet {len(referrers)} live element(s) point at it — {shown}{more}. "
            "Following the pointer leads nowhere: un-archive it, absorb it "
            "into what replaced it, or fix the referrer")

    return out


def lint_partial_archive(all_rows: list[dict]) -> list[str]:
    """Elements archived on SOME versions but not others (#176).

    `retire` is element-level — it archives every version of an est_item_id.
    The plain archive action writes only the selected row ids, so archiving
    from a version view leaves the rest live and the element reads
    inconsistently: hidden in one view, present in another. Two elements in
    the tenant are in this state.

    `all_rows` is every row for the project, archived and live alike.
    """
    versions: dict[str, list[dict]] = {}
    for r in all_rows:
        eid = r.get("est_item_id")
        if eid:
            versions.setdefault(eid, []).append(r)

    out: list[str] = []
    for eid, rows in sorted(versions.items()):
        archived = [r for r in rows if r.get("archived")]
        live = [r for r in rows if not r.get("archived")]
        if not archived or not live:
            continue
        title = (archived[0].get("framing") or live[0].get("framing") or eid)
        out.append(
            f"⚠ partially archived: '{title}' ({eid}) has "
            f"{len(archived)} archived version(s) and {len(live)} live — the "
            "element is half-hidden and reads inconsistently across views. "
            "Archive is element-level like retire: archive all versions or none")
    return out


# ──────────────────────────────────────────────────────────────────────
#  The whole pass, assembled once (#280)
# ──────────────────────────────────────────────────────────────────────


def _workstream_has_agreement(client, rows: list[dict]) -> bool | None:
    """Whether the project its rows carry has an agreement.

    The same signal `sync_mc2.workstream_rows_to_states` feeds
    `state.derive_label` as `has_agreement` — `deal_stage IS NOT NULL` — read
    rather than re-derived, so the lint and the label cannot disagree about
    what an agreement is. Best-effort: any failure returns None, and None
    keeps every check on (#319).
    """
    from cp_engine import mc2_db

    try:
        project_ids = {r.get("project_id") for r in rows if r.get("project_id")}
        if len(project_ids) != 1:
            return None  # none, or rows under two projects: don't guess
        (pid,) = project_ids
        found = (
            client.table(mc2_db.Tables.PROJECTS)
            .select("id, deal_stage")
            .eq("id", pid)
            .limit(1)
            .execute()
            .data
        ) or []
        if not found or found[0].get("id") != pid:
            return None
        return found[0].get("deal_stage") is not None
    except Exception:  # noqa: BLE001 — advisory pass, never fail the lint
        return None


# ──────────────────────────────────────────────────────────────────────
#  Dangling `Source reviewed:` references (#324 part c)
# ──────────────────────────────────────────────────────────────────────
#
# A derived doc's header names the file it was written against —
# **Source reviewed:** `Infoblox_Truth_AI_Depends_On_ECD_Creative_Room.md`
# (ibx-5153, 2026-08-28). That reads as provenance; when the file never
# reached the source store it is a dangling pointer, and the review quietly
# outlives the only copy of what it compressed. For two weeks nothing said so.

_SOURCE_REVIEWED_RE = re.compile(
    r"^[\s>*_-]*sources?[ _-]+reviewed[*_]*\s*:[*_]*\s*(?P<rest>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_BACKTICK_RE = re.compile(r"`([^`]+)`")
# A bare (un-backticked) reference counts only when it LOOKS like a file —
# a prose value ("the Q3 deck, from memory") makes no claim to resolve.
_FILE_TOKEN_RE = re.compile(r"[^\s,;()`]+(?:\s[^\s,;()`]+)*\.[A-Za-z0-9]{2,5}")
_KNOWN_EXT_RE = re.compile(
    r"\.(?:md|markdown|txt|pdf|docx?|pptx?|xlsx?|csv|key|pages|numbers|rtf|"
    r"png|jpe?g|gif|mp4|mov|m4a|mp3|html?|json|vtt|srt)$",
    re.IGNORECASE,
)


def extract_source_reviewed(text: str) -> list[str]:
    """Every file a doc says it reviewed, from its `Source(s) reviewed:`
    lines — backticked names, else file-shaped tokens. Order kept, deduped."""
    refs: list[str] = []
    for m in _SOURCE_REVIEWED_RE.finditer(text or ""):
        rest = m.group("rest")
        found = _BACKTICK_RE.findall(rest) or _FILE_TOKEN_RE.findall(rest)
        for ref in found:
            ref = ref.strip()
            if ref and ref not in refs:
                refs.append(ref)
    return refs


def _norm_source_name(name: str) -> str:
    """Compare a referenced filename with an ingested title: basename,
    case-folded, known extensions stripped (ingest keeps `x.pptx.pdf`,
    Drive-native titles carry none), separators collapsed."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    while True:
        stripped = _KNOWN_EXT_RE.sub("", base)
        if stripped == base:
            break
        base = stripped
    return re.sub(r"[\s_\-]+", " ", base).strip()


def lint_source_reviewed(
    docs: dict[str, str],
    source_titles,
    local_files=(),
) -> list[str]:
    """Warn for each `Source reviewed:` reference that resolves to nothing.

    `docs` maps a doc's display path to its text. `source_titles` is every
    title the source store has held for this workstream (any status —
    `project_sources.ingested_source_titles`). `local_files` are file names
    present in the workstream's own directory, which also resolve: a file
    in the tree is reachable even though it is not in the store.

    Match is on the normalised name only (`_norm_source_name`) — the file
    a doc NAMES against the file the store HOLDS; nothing is inferred from
    either one's contents.
    """
    known = {_norm_source_name(t) for t in source_titles if t}
    known |= {_norm_source_name(f) for f in local_files if f}
    out: list[str] = []
    for path in sorted(docs):
        for ref in extract_source_reviewed(docs[path]):
            norm = _norm_source_name(ref)
            # A bare reference can carry leading prose ("the ECD deck.pdf");
            # it resolves when a known name is its trailing words.
            if norm in known or any(k and norm.endswith(" " + k) for k in known):
                continue
            out.append(
                f"`{path}` names Source reviewed `{ref}`, which was never "
                "ingested (not in the source store or this workstream's "
                "directory) — the review may be outliving its only source; "
                "ingest the file, or record where it lives"
            )
    return out


def run_all_lints(
    client,
    codes: list[str],
    *,
    cp_md_text: str | None = None,
    today=None,
    workstream_docs: dict[str, str] | None = None,
    local_files=(),
    source_titles=None,
) -> list[str]:
    """Every spine-lint check for one project, as a flat list of warnings.

    WHY THIS EXISTS. The assembly — which tables to read, which columns, the
    one-live-per-element discipline, which checks take relations and which take
    the archived rows too — lived only inside the `cxp spine-lint` command
    body. A hosted caller wanting the same answer had to reimplement it, and
    two copies of a rule drift: that is the #172 and #178 lesson, and the
    reason `project_state.py` reuses `merge_exec_summary_fields` rather than
    restating the field grammar.

    `client` is any PostgREST client — the CLI's service-credentialed one or
    the hosted server's per-caller one. Under RLS the hosted caller simply sees
    fewer rows; the checks are identical either way.

    `codes` is the project's code plus, where it differs, its directory slug —
    ibx-5153 carries live rows under both. Pass every code the project answers
    to, as the CLI does.

    `cp_md_text` enables the scaffold-placeholder check. Omitted (the hosted
    case, until a caller reads the file) it is skipped rather than guessed at.

    `workstream_docs` (`{relpath: text}` of the workstream directory's own
    markdown — the caller reads it; this module touches no filesystem) +
    `source_titles` enable the dangling `Source reviewed:` check (#324);
    `local_files` are the directory's file names, which also resolve (see
    `lint_source_reviewed`). Either omitted, the check is skipped. It does not depend on the spine,
    so it runs even for a workstream with no live spine rows.

    Returns warnings in the CLI's order so the two surfaces read the same.
    """
    from cp_engine import mc2_db
    from cp_engine.project_sources import _one_live_per_element

    all_rows = (
        client.table(mc2_db.Tables.SPINE_SUBSTANCE)
        .select(mc2_db.SPINE_LINT_COLUMNS)
        .in_("project_code", codes)
        .eq("status", "live")
        .execute()
        .data
    ) or []
    # One live row per element (#113) — a double-live element warns once.
    rows = _one_live_per_element([r for r in all_rows if not r.get("archived")])
    source_warnings: list[str] = []
    if workstream_docs is not None and source_titles is not None:
        source_warnings = lint_source_reviewed(
            workstream_docs, source_titles, local_files
        )
    if not rows:
        return source_warnings

    warnings: list[str] = list(lint_spine_rows(rows))

    # Relations are optional: a read failure degrades the lifecycle and
    # archived-referrer checks rather than failing the pass.
    relations_all: list[dict] = []
    try:
        relations_all = (
            client.table(mc2_db.Tables.SPINE_RELATIONS)
            .select("kind, from_item_id, to_item_id, status")
            .in_("project_code", codes)
            .eq("status", "active")
            .execute()
            .data
        ) or []
        warnings.extend(lint_lifecycle(rows, relations_all))
    except Exception:  # noqa: BLE001 — advisory pass, never fail the lint
        pass

    warnings.extend(
        lint_curation(rows, today=today,
                      has_agreement=_workstream_has_agreement(client, rows))
    )

    try:
        every_row = (
            client.table(mc2_db.Tables.SPINE_SUBSTANCE)
            .select(mc2_db.SPINE_LINT_COLUMNS)
            .in_("project_code", codes)
            .execute()
            .data
        ) or []
        archived_rows = [r for r in every_row if r.get("archived")]
        warnings.extend(
            lint_archived_referrers(rows, archived_rows, relations_all)
        )
        warnings.extend(lint_partial_archive(every_row))
    except Exception:  # noqa: BLE001
        pass

    if cp_md_text is not None:
        warnings.extend(lint_cp_placeholders(cp_md_text))
        # The Exec Summary field budgets ride the same text (#204). Kept here
        # rather than at the call site so both surfaces get the whole pass.
        from cp_engine.exec_summary_lint import lint_exec_summary

        warnings.extend(lint_exec_summary(cp_md_text))
    warnings.extend(source_warnings)
    return warnings
