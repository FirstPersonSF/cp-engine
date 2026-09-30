"""Per-item routing and cross-target dedupe for auto-ingest (#322).

THE REPORT. Meeting `485c1bf8` (2026-09-21) was an SAP call that also
covered SalesLoft. The model routed its own SalesLoft items to slt-5196 —
but every Fathom action item was appended, by code, to the TAGGED project:
"Email Morgan: request SalesLoft interviewee background", "research
SalesLoft interviewees … compile Sean brief" landed in sap-5174's Open
asks. A meeting tagged to two projects is worse: each tagged project's
pass appended ALL the action items to itself. Separately, one SalesLoft
stage-hold risk arrived on slt-5196 twice from first-person-operations
(meetings `391f31fd`, `9984b759`) as two sibling bullets with different
wording — two hashes, one risk, contradictory counterparty.

Three deterministic checks, no model involved:

1. `route_action_items` — each action item goes to the project it names.
   A project is named by its IDENTITY tokens: the code's number, the
   company and project-name words, and its stakeholder cards' names —
   keeping only tokens distinctive within the candidate set (`campaign`
   names nobody when two candidates are campaigns). Among co-tagged
   projects only identity counts, so every tagged project's pass reaches
   the same answer and an item is written exactly once. A project the
   model already wrote a block for can also win on content overlap with
   that block's items. Ties and silence stay with the tagged project.
2. `dedupe_across_targets` — the same item (same verb family, same
   content words) under two projects of one plan is written once, unless
   the plan marks it `shared: true`.
3. `find_near_duplicate` — an accepted cross-routed item that matches an
   existing bullet on the target (a shared money figure and content-word
   Jaccard >= 0.35, or Jaccard >= 0.55 alone) is written as an UPDATE
   line under that bullet instead of a sibling.

Thresholds come from the tenant's real sprint files (see the #322 commit).
"""

from __future__ import annotations

import logging
import re
from typing import Callable, Iterable

from cp_engine.text_similarity import amounts, content_tokens, jaccard, normalized_key

log = logging.getLogger(__name__)

NEAR_DUP_WITH_AMOUNT = 0.35
NEAR_DUP_TEXT_ONLY = 0.55
BLOCK_OVERLAP = 0.3

_FAMILY = {
    "inbound": "inbound", "record-inbound": "inbound",
    "asks": "asks", "ask": "asks", "record-ask": "asks",
    "decisions": "decisions", "decision": "decisions", "add-decision": "decisions",
    "risks": "risks", "risk": "risks", "record-risk": "risks",
    "open-questions": "open-questions", "open_questions": "open-questions",
    "record-open-question": "open-questions",
}

# Words that name a kind of project, not a project.
# Topical words (vision, brand, campaign) stay: when two candidates share
# one, `_distinctive` drops it for that comparison.
_GENERIC = frozenset(
    """
    update refresh program project maintenance operations content launch
    """.split()
)


def verb_family(verb: str) -> str | None:
    return _FAMILY.get(verb)


# ──────────────────────────────────────────────────────────────────────
#  1. Action-item routing
# ──────────────────────────────────────────────────────────────────────


def _number(code: str) -> str | None:
    m = re.match(r"^[a-z0-9]+-(\d{3,5})\b", (code or "").lower())
    return m.group(1) if m else None


def same_project(a: str, b: str) -> bool:
    a, b = (a or "").lower(), (b or "").lower()
    if a == b:
        return True
    na, nb = _number(a), _number(b)
    return bool(na and na == nb and a.split("-")[0] == b.split("-")[0])


def identity_tokens(
    code: str, *, name: str = "", company: str = "", people: Iterable[str] = ()
) -> frozenset[str]:
    slug_words = " ".join(code.split("-")[2:]) if code.count("-") >= 2 else ""
    toks = set(content_tokens(f"{name} {company} {slug_words} {' '.join(people)}"))
    num = _number(code)
    if num:
        toks.add(num)
    return frozenset(t for t in toks if t not in _GENERIC and not re.fullmatch(r"20\d\d", t))


def _distinctive(idents: dict[str, frozenset[str]]) -> dict[str, frozenset[str]]:
    counts: dict[str, int] = {}
    for toks in idents.values():
        for t in toks:
            counts[t] = counts.get(t, 0) + 1
    return {c: frozenset(t for t in toks if counts[t] == 1) for c, toks in idents.items()}


def _identity_hits(text: str, toks: frozenset[str]) -> int:
    words = content_tokens(text)
    return len(words & toks)


def route_action_items(
    items: list[dict],
    *,
    plan: dict,
    project_code: str,
    co_tagged: Iterable[str] = (),
    identity_for: Callable[[str], frozenset[str]],
    rehash: Callable[[str, dict], None] | None = None,
) -> dict:
    """Place Fathom action items (already built as `record-ask` dicts) into
    `plan` in place. Returns `{kept, moved, left_to_cotagged}` counts.

    `identity_for(code)` returns a project's raw identity tokens (the
    caller owns roster and disk access). `rehash(code, item)` re-stamps an
    item's deterministic hash for its new project.
    """
    summary = {"kept": 0, "moved": 0, "left_to_cotagged": 0}
    projects = plan.setdefault("projects", {})
    others = [c for c in co_tagged if not same_project(c, project_code)]

    # Pass 1 — co-tagged: identity only, so every tagged pass agrees.
    tagged_set = [project_code] + others
    tagged_ids = _distinctive({c: identity_for(c) for c in tagged_set})

    # Pass 2 — blocks the model already wrote into THIS plan.
    block_codes = [
        c for c in projects
        if not same_project(c, project_code) and not any(same_project(c, o) for o in others)
    ]
    plan_set = [project_code] + others + block_codes
    plan_ids = _distinctive({c: identity_for(c) for c in plan_set})

    def _block_texts(code: str) -> list[str]:
        out: list[str] = []
        for verb, its in (projects.get(code) or {}).items():
            if verb_family(verb) and isinstance(its, list):
                out += [str(i.get("text") or "") for i in its if isinstance(i, dict)]
        return out

    # With no signal (or a tie) every pass must still agree on ONE owner:
    # the first tagged project, in the order the webhook passes them.
    canonical = next(iter(co_tagged), project_code) if others else project_code

    for item in items:
        text = str(item.get("text") or "")
        if others:
            scores = {c: _identity_hits(text, tagged_ids[c]) for c in tagged_set}
            top = max(scores.values())
            leaders = [c for c in tagged_set if scores[c] == top]
            owner = leaders[0] if top > 0 and len(leaders) == 1 else canonical
            if not same_project(owner, project_code):
                summary["left_to_cotagged"] += 1
                continue
        scores = {}
        for c in plan_set:
            s = _identity_hits(text, plan_ids[c])
            if c in block_codes and any(
                jaccard(text, t) >= BLOCK_OVERLAP for t in _block_texts(c)
            ):
                s += 1
            scores[c] = s
        best = max(block_codes, key=lambda c: scores[c], default=None)
        target = project_code
        if best is not None and scores[best] > scores[project_code] and (
            sum(1 for c in plan_set if scores[c] == scores[best]) == 1
        ):
            target = best
        if target != project_code:
            if rehash:
                rehash(target, item)
            summary["moved"] += 1
        else:
            summary["kept"] += 1
        projects.setdefault(target, {}).setdefault("record-ask", []).append(item)
    return summary


# ──────────────────────────────────────────────────────────────────────
#  2. Cross-target dedupe inside one plan
# ──────────────────────────────────────────────────────────────────────


def dedupe_across_targets(plan: dict) -> list[str]:
    """Drop the second and later copies of an item that appears under more
    than one project. Returns `"<code>/<family>: <text>"` per dropped copy."""
    projects = plan.get("projects")
    if not isinstance(projects, dict) or len(projects) < 2:
        return []
    seen: dict[tuple[str, str], str] = {}
    dropped: list[str] = []
    for code, entries in projects.items():
        if not isinstance(entries, dict):
            continue
        for verb, items in entries.items():
            fam = verb_family(verb)
            if not fam or not isinstance(items, list):
                continue
            kept = []
            for item in items:
                if not isinstance(item, dict) or item.get("shared"):
                    kept.append(item)
                    continue
                key = normalized_key(str(item.get("text") or ""))
                if not key:
                    kept.append(item)
                    continue
                owner = seen.setdefault((fam, key), code)
                if owner != code and not same_project(owner, code):
                    dropped.append(f"{code}/{fam}: {str(item.get('text'))[:80]}")
                    log.warning(
                        "cross-target duplicate: %s %s already written to %s — "
                        "skipped (mark `shared: true` to write both)",
                        code, fam, owner,
                    )
                    continue
                kept.append(item)
            entries[verb] = kept
    return dropped


# ──────────────────────────────────────────────────────────────────────
#  3. Near-duplicate on a cross-routed write
# ──────────────────────────────────────────────────────────────────────

_MANAGED_RE = re.compile(
    r"<!-- cp-engine:start (?P<n>[\w-]+) -->.*?<!-- cp-engine:end (?P=n) -->", re.S
)
_BULLET_RE = re.compile(r"^- \[[^\]]*\]\s*(?P<text>.*cp:hash=[0-9a-f]{8}.*)$")


def find_near_duplicate(body: str, text: str) -> str | None:
    """The existing hand-written bullet line in `body` that `text` restates,
    or None. Managed regions are never matched (a write there is undone by
    the next render), nor are source-arrival bullets."""
    spans = [(m.start(), m.end()) for m in _MANAGED_RE.finditer(body)]
    want_amounts = amounts(text)
    best: tuple[float, str] | None = None
    pos = 0
    for line in body.splitlines(keepends=True):
        start, pos = pos, pos + len(line)
        if any(a <= start < b for a, b in spans):
            continue
        m = _BULLET_RE.match(line.rstrip("\n"))
        if not m or "New source ingested" in line:
            continue
        existing = m.group("text")
        j = jaccard(existing, text)
        hit = j >= NEAR_DUP_TEXT_ONLY or (
            j >= NEAR_DUP_WITH_AMOUNT and bool(want_amounts & amounts(existing))
        )
        if hit and (best is None or j > best[0]):
            best = (j, line.rstrip("\n"))
    return best[1] if best else None
