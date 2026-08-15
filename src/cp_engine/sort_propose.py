"""Model pre-pass for the weekly sort — propose a lifetime, with a reason.

WHY THIS EXISTS
---------------
`cp weekly-sort` finds 129 unclassified rows and can decide 3 of them from
structure. The remaining 126 need reading: "Fred Fredericks Interview" and
"Workday Picks Spotnana To Support AI Travel Agent.pdf" cannot be told apart by
layer, and a human facing 126 blank fields will abandon the tool on the first
sitting. So the model proposes and the human confirms — the same
machine-proposes / human-confirms shape as auto-steps, inbox cards and
cross-project routing.

THE PROMPT IS NOT WRITTEN HERE
------------------------------
Judgment priors come from the master prompt (mig 139) via `system=`, resolved
per project so an override applies. This module supplies only the TASK — the
items and the output contract. What counts as canon, whose voice outranks
whose, and when to admit uncertainty all live in the priors, editable by a
human without touching code. That separation is the point of the whole
exercise.

WHAT THE MODEL SEES, AND WHY IT IS TRUNCATED
--------------------------------------------
Measured 2026-08-15 across the 129 unclassified attachments: 82 are THIN
(<200 chars — the body is `Ingested document: <name>` plus a rag_asset uuid),
42 are LONG (up to 25,562 chars; 232,656 total). Sending full bodies would
cost a fortune and swamp the signal.

For a thin card the FILENAME is the entire signal, and it is usually enough —
`260807-Email-thread-Costco-story-inputs.docx` reads as feedback,
`infoblox-use-case-brief-enterprise-wide-visibility.pdf` reads as background.
For a long card the opening is where the framing lives. So bodies are cut to
`_BODY_CHARS` and the model is told they are excerpts, so it can answer
`unsure` rather than guessing from a truncation.

BATCHED PER PROJECT, DELIBERATELY
---------------------------------
One call per project rather than per item: the 14 sap-5174 interview write-ups
are near-identical, and judging them together produces consistent answers where
14 separate calls would drift. It also lets the model see that they ARE a set —
"one of 14 interview write-ups feeding a synthesis" is a different judgement
from "an interview write-up".

NOTHING HERE WRITES
-------------------
`propose()` returns proposals. Persisting them is the caller's decision, and
`cp weekly-sort --apply` still only writes structurally-decided lifetimes. A
model proposal reaches the database only through a human confirming it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Enough to carry the framing of a long body; a thin card is under this anyway.
_BODY_CHARS = 600

# Items per call. Keeps a single prompt well inside a comfortable window even
# when every item in a batch is a long one, and keeps a failed call cheap.
_BATCH = 25

_LIFETIMES = ("background", "feedback", "canon", "unsure")

_TASK = """\
You are classifying items in First Person's project memory by LIFETIME — how \
long each stays true. This is the weekly sort.

Three lifetimes, plus an escape hatch:

- **background** — always true, rarely changes. Who the client is, how they \
buy, org shape, market context, reference material, retrospectives. Permanent \
but low salience.
- **feedback** — transient working material that needs action and should stop \
existing as feedback once handled. Internal OR external: a note to a \
contractor and a client's marked-up deck are the same kind, because both are \
pending until processed. Point-in-time; it decays.
- **canon** — a truth WE defined for this project. Positioning, architecture, \
audience, a ruling someone made. Evolving but lasting, and highest authority.
- **unsure** — you genuinely cannot tell from what you were given. Use this \
freely. A wrong confident answer costs a human more than an honest gap, \
because nobody knows to check it.

Classification is by LIFECYCLE, not origin. Where something came from is \
metadata; how long it stays true is the classification.

Notes on what you are reading:
- Bodies are EXCERPTS, cut at {body_chars} characters. If the excerpt does \
not settle it, answer `unsure`.
- Many items are pointer cards whose body is just `Ingested document: <name>` \
plus an asset id. For those the FILENAME is the signal — judge from it, or \
answer `unsure`.
- Items are grouped by project. Several may be near-identical (e.g. a run of \
interview write-ups); classify them consistently.

Return ONLY a JSON array, one object per item, in the order given:

[{{"id": "<the id given>", "lifetime": "background|feedback|canon|unsure", \
"why": "<up to 15 words>"}}]

No prose, no markdown fence, no preamble."""


@dataclass
class Proposed:
    row_id: str
    lifetime: str  # one of _LIFETIMES
    why: str


def _clip(text: str | None, limit: int = _BODY_CHARS) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[:limit].rstrip() + " […]"


def build_prompt(items: list[dict]) -> str:
    """The task plus one batch of items. Pure — no network, no clock."""
    lines = [_TASK.format(body_chars=_BODY_CHARS), "", "# Items", ""]
    for it in items:
        lines.append(f"## {it['id']}")
        lines.append(f"project: {it.get('project_code') or '—'}")
        lines.append(f"layer: {it.get('layer') or '—'}")
        lines.append(f"title: {it.get('framing') or '(no title)'}")
        if it.get("version_date"):
            lines.append(f"dated: {it['version_date']}")
        body = _clip(it.get("body"))
        lines.append(f"body: {body}" if body else "body: (empty)")
        lines.append("")
    return "\n".join(lines)


def parse_response(text: str, batch: list[dict]) -> list[Proposed]:
    """Model text -> proposals. Unparseable or unknown ids are DROPPED.

    A dropped item simply stays unclassified and reaches the human as an
    unproposed row — which is the correct failure mode. Inventing a lifetime
    to fill a gap is the one thing this must never do.
    """
    raw = (text or "").strip()
    # Tolerate a fence even though the contract forbids one.
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3]
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end == -1:
        log.debug("no JSON array in model response")
        return []
    try:
        rows = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        log.debug("proposal JSON did not parse: %s", exc)
        return []

    known = {it["id"] for it in batch}
    out: list[Proposed] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        row_id = str(row.get("id") or "")
        lifetime = str(row.get("lifetime") or "").strip().lower()
        if row_id not in known or lifetime not in _LIFETIMES:
            continue
        out.append(Proposed(row_id, lifetime, str(row.get("why") or "")[:120]))
    return out


def propose(
    items: list[dict],
    *,
    llm,
    batch_size: int = _BATCH,
) -> list[Proposed]:
    """Propose a lifetime for each item. `llm(prompt) -> str` is injected.

    Batches never span projects, so a batch always shares one set of priors and
    the model can see a run of similar items as a set. A batch that fails or
    returns garbage is skipped, not retried — those rows reach the human
    unproposed, which is a worse experience but never a wrong one.
    """
    by_project: dict[str, list[dict]] = {}
    for it in items:
        by_project.setdefault(it.get("project_code") or "", []).append(it)

    out: list[Proposed] = []
    for _code, rows in sorted(by_project.items()):
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            try:
                out.extend(parse_response(llm(build_prompt(batch)), batch))
            except Exception as exc:  # noqa: BLE001 — one batch must not sink the run
                log.debug("proposal batch failed (%s: %s)", type(exc).__name__, exc)
    return out
