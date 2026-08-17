"""Model pre-pass for the routing queue — propose a DESTINATION, with a reason.

Companion to `sort_propose`. That one answers "how long does this stay true?";
this answers "what work does this belong to?". They are separate acts, and the
weekly sort tracked only the first: measured 2026-08-16, ibx-5192's sort queue
hit ZERO with 42 of its 74 classified rows still attached to no work item —
including 10 of 16 canon rows (the Retrospective, four Synthesis elements, five
Decisions). Tenant-wide the routing backlog is 84 rows across 11 projects.

WHAT MAKES THIS HARDER THAN THE LIFETIME PASS
---------------------------------------------
A lifetime is one of three fixed words, so a malformed answer is trivially
detectable. A destination is an `est_item_id` from THIS project's estimate —
per-project, unbounded, and a plausible-looking id can be entirely invented.
So the slot list goes INTO the prompt and every returned id is checked against
it on the way out (`parse_response`). An id that is not in the list is dropped,
not repaired: a confident wrong routing is worse than a blank field, because
nobody knows to check it.

BACKGROUND IS NOT PROPOSED ON
-----------------------------
Background belongs to no single work item by definition — that is why the Sort
tab ungates it from needing a home. Asking the model where the Infoblox
corporate library "belongs" would invite it to invent an answer. Callers filter
to canon/feedback/unclassified; this module does not re-check, but the prompt
says so, and a body that reads as background should come back `unsure`.

THE PROMPT IS NOT WRITTEN HERE
------------------------------
Judgment priors come from the master prompt (mig 139) via `system=`, resolved
per project. This module supplies only the task and the output contract.

NOTHING HERE WRITES `serves`
----------------------------
`propose()` returns proposals; `persist()` writes them to
`spine_sort_proposals` with `kind='route'` (mig 145). A model proposal reaches
`spine_substance.serves` only through a human confirming it in the UI.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from cp_engine.mc2_db import Tables

log = logging.getLogger(__name__)

_BODY_CHARS = 600
# Smaller than the lifetime pass's 25: each item now carries a slot list in the
# shared preamble and the answers are ids rather than one of three words, so a
# failed call is more expensive to redo.
_BATCH = 15

_UNSURE = "unsure"

_TASK = """\
You are filing items into First Person's project memory. For each item, say \
which WORK ITEM it belongs to — the deliverable or activity whose work it \
describes, informs, or responds to.

You will be given the project's work items. Answer with one of their exact \
ids, or `unsure`.

- **Route it** when the item is clearly about one piece of work: a decision \
that governs a deck, feedback on a specific draft, a synthesis that fed one \
deliverable.
- **`unsure`** when you genuinely cannot tell, when it plausibly belongs to \
several, or when it belongs to the project as a whole rather than any one \
piece of work. Use this freely. A wrong confident routing is worse than an \
honest gap, because a human reading a blank field knows to look while nobody \
re-checks a filled one.

Do NOT invent an id. If nothing in the list fits, the answer is `unsure`.

Notes on what you are reading:
- Bodies are EXCERPTS, cut at {body_chars} characters. If the excerpt does not \
settle it, answer `unsure`.
- An item's layer is a strong hint about what it can belong to: a Decision or \
a Client-feedback item usually points at the thing it ruled on or reviewed.
- Several items may be near-identical; file them consistently.

Return ONLY a JSON array, one object per item, in the order given:

[{{"id": "<the item id given>", "slot": "<work item id|unsure>", \
"confidence": 0.0-1.0, "why": "<up to 15 words>"}}]

`confidence` is how sure you are, and it is used: a human can accept a \
proposal with one keystroke, so report it honestly."""


@dataclass
class ProposedRoute:
    row_id: str
    #: an est_item_id from the project's slot list, or "unsure".
    slot_id: str
    why: str
    confidence: float | None = None


def _clip(text: str | None, limit: int = _BODY_CHARS) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + " […]"


def build_prompt(items: list[dict], slots: list[dict]) -> str:
    """The task, the project's work items, and one batch. Pure.

    The slot list is rendered ONCE as a preamble rather than per item — it is
    the same list for every item in a project batch, and repeating it would
    crowd out the bodies that actually decide the answer.
    """
    lines = [_TASK.format(body_chars=_BODY_CHARS), "", "# Work items", ""]
    if slots:
        for s in slots:
            phase = f" · {s['phase']}" if s.get("phase") else ""
            kind = f" ({s['kind']})" if s.get("kind") else ""
            lines.append(f"- `{s['id']}` — {s.get('name') or '(unnamed)'}{kind}{phase}")
    else:
        # A project with no estimate has nowhere to route. Say so rather than
        # rendering an empty list the model might read as an oversight.
        lines.append("(none — this project has no estimate; answer `unsure`)")
    lines += ["", "# Items", ""]
    for it in items:
        lines.append(f"## {it['id']}")
        lines.append(f"layer: {it.get('layer') or '—'}")
        lines.append(f"lifetime: {it.get('lifetime') or 'unclassified'}")
        lines.append(f"title: {it.get('framing') or '(no title)'}")
        if it.get("version_date"):
            lines.append(f"dated: {it['version_date']}")
        body = _clip(it.get("body"))
        lines.append(f"body: {body}" if body else "body: (empty)")
        lines.append("")
    return "\n".join(lines)


def parse_response(
    text: str, batch: list[dict], valid_slot_ids: set[str]
) -> list[ProposedRoute]:
    """Model text -> proposals. Unknown item ids AND unknown slot ids are DROPPED.

    THE SLOT CHECK IS THE POINT. A lifetime is one of three words so a bad
    answer is obvious; a slot id is free-form and a hallucinated one looks
    exactly like a real one. An id outside the project's own estimate is not
    repaired or guessed at — the item reaches the human with a blank field,
    which is the correct failure mode.

    `unsure` survives: it is a real answer, and knowing which items the model
    declined is a signal about the priors.
    """
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3]
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        log.debug("no JSON array in route response")
        return []
    try:
        rows = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        log.debug("route proposal JSON did not parse: %s", exc)
        return []

    known = {it["id"] for it in batch}
    out: list[ProposedRoute] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        row_id = str(row.get("id") or "")
        slot = str(row.get("slot") or "").strip()
        if row_id not in known:
            continue
        if slot != _UNSURE and slot not in valid_slot_ids:
            # A hallucinated or stale id. Dropped, never coerced to `unsure`:
            # "the model named a slot that does not exist" and "the model
            # declined" are different facts, and only the second is a judgement
            # worth recording.
            log.debug("dropped route proposal naming unknown slot %r", slot)
            continue
        conf: float | None
        try:
            conf = float(row["confidence"])
            if not 0.0 <= conf <= 1.0:
                conf = None
        except (KeyError, TypeError, ValueError):
            conf = None
        out.append(
            ProposedRoute(row_id, slot, str(row.get("why") or "")[:120], conf)
        )
    return out


def persist(
    client,
    items: list[dict],
    proposals: list[ProposedRoute],
    *,
    prompt_version: int | None = None,
    model: str | None = None,
) -> int:
    """Write to `spine_sort_proposals` with kind='route' (mig 145).

    Same discipline as the lifetime pass: supersede the prior ACTIVE row rather
    than overwriting, so a changed answer after a priors edit stays visible;
    leave accepted/rejected rows alone, since those are terminal records of
    what a human decided.

    The unique index is per (substance_id, kind), so a route proposal and a
    lifetime proposal for the same element coexist — they answer different
    questions and neither supersedes the other.
    """
    if not proposals:
        return 0

    by_id = {it["id"]: it for it in items}
    written = 0
    for p in proposals:
        row = by_id.get(p.row_id)
        if row is None:
            continue
        client.table(Tables.SPINE_SORT_PROPOSALS).update(
            {"status": "superseded"}
        ).eq("substance_id", p.row_id).eq("kind", "route").eq(
            "status", "active"
        ).execute()
        client.table(Tables.SPINE_SORT_PROPOSALS).insert(
            {
                "substance_id": p.row_id,
                "project_id": row.get("project_id"),
                "project_code": row.get("project_code"),
                "est_item_id": row.get("est_item_id"),
                "kind": "route",
                "proposal": p.slot_id,
                "confidence": p.confidence,
                "reason": p.why or None,
                "status": "active",
                "prompt_version": prompt_version,
                "model": model,
            }
        ).execute()
        written += 1
    return written


def propose(
    items: list[dict],
    slots: list[dict],
    *,
    project_id: str | None = None,
    model: str | None = None,
    call=None,
) -> list[ProposedRoute]:
    """Run the pre-pass over one project's items. Returns proposals; writes nothing.

    Batched, and a failed batch DROPS its items rather than failing the run —
    those reach the human as blank fields, which is recoverable. `call` is
    injected for testing; by default it goes through `plan_from_transcript`'s
    `_call_claude`, which resolves the master prompt as `system=` for this
    project.
    """
    if not items:
        return []
    valid = {s["id"] for s in slots}
    if call is None:
        from cp_engine.plan_from_transcript import _call_claude

        # `model` and `api_key` are KEYWORD-ONLY and REQUIRED. Omitting them
        # raised TypeError on every batch, which the loop below swallowed into
        # a silent "0 proposals" — the failure looked like a model that
        # declined 21 times rather than a call that never happened.
        def call(prompt: str) -> str:  # type: ignore[misc]
            return _call_claude(
                prompt,
                model=model or "claude-opus-4-7",
                api_key=None,
                project_id=project_id,
            )

    out: list[ProposedRoute] = []
    for i in range(0, len(items), _BATCH):
        batch = items[i : i + _BATCH]
        try:
            text = call(build_prompt(batch, slots))
        except Exception as exc:  # noqa: BLE001
            # WARNING, not debug. A swallowed batch is indistinguishable from a
            # model that declined everything, and that is exactly how a wrong
            # call signature read as "proposed 0 of 21" instead of as an error.
            log.warning(
                "route batch failed (%s: %s)", type(exc).__name__, exc
            )
            continue
        out.extend(parse_response(text, batch, valid))
    return out
