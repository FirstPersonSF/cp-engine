"""Project Spine slice 3 — sweep synthesis (Phase B).

Pure prompt construction for the whole-project sweep: rank every element via
the Lens, then build an LLM prompt asking for an across-the-project readout that
names the cold-but-important threads. The LLM call itself lives in run_sweep
(injectable, Task B2); this module is pure and testable without a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Callable

import yaml

from cp_engine.spine import (
    SpineElement,
    rank_elements,
    render_sweep,
)

# Top-N elements by score get a body excerpt; the rest are listed title-only.
# This is a deliberate scale guard: a mature project can carry dozens of
# elements, and dumping every body would blow the token budget without adding
# signal (the cold tail is named by title + status, which is enough for the
# LLM to flag it). Tune here, not at the call site.
_EXCERPT_LIMIT = 30
_EXCERPT_CHARS = 200


def _excerpt(body: str) -> str:
    """First ~_EXCERPT_CHARS chars of a body, whitespace-collapsed, one line."""
    collapsed = " ".join(body.split())
    if len(collapsed) <= _EXCERPT_CHARS:
        return collapsed
    return collapsed[:_EXCERPT_CHARS].rstrip() + "…"


# Default number of recent meeting entries to feed the sweep prompt. Bounded so
# a long-running project's full meeting history doesn't swamp the token budget;
# the newest few are what ground the synthesis in "what was just discussed".
_MEETING_LIMIT = 4
_MEETING_MARKER_RE = re.compile(r"^[ \t]*<!--[ \t]*cp:meeting=[^>]*-->[ \t]*\n?", re.MULTILINE)
# A real Retrospective entry header is `### <YYYY-MM-DD> · …` (build_entry's
# format). Anchoring on the date keeps embedded Fathom-summary H3s from being
# mistaken for entry boundaries.
_ENTRY_HEADER_RE = re.compile(r"^### \d{4}-\d{2}-\d{2}\b")


def recent_meeting_summaries(
    elements: tuple[SpineElement, ...],
    *,
    limit: int = _MEETING_LIMIT,
) -> list[str]:
    """Extract up to `limit` most-recent meeting entries from the project's
    Retrospective element body, newest first.

    Returns a list of entry strings (each the markdown block for one meeting,
    header through links). Returns [] if there's no Retrospective element or it
    has no `### `-headed entries. The Retrospective body is already newest-first
    (Phase 2 inserts at the top), so we just take the first `limit`.

    The idempotency `<!-- cp:meeting=... -->` trailer is stripped from each
    returned block — it's machine bookkeeping, noise for the LLM.
    """
    retro = next((e for e in elements if e.layer == "Retrospective"), None)
    if retro is None or not retro.body:
        return []

    # Split ONLY on true entry headers `### <YYYY-MM-DD> · …` (build_entry's
    # format). A bare `### ` test would also split on H3s inside an embedded
    # Fathom summary — which we deliberately keep WHOLE — fragmenting a meeting
    # into several "entries". The date anchor distinguishes a real entry header
    # from summary sub-headers.
    entries: list[str] = []
    current: list[str] | None = None
    for line in retro.body.splitlines():
        if _ENTRY_HEADER_RE.match(line):
            if current is not None:
                entries.append("\n".join(current))
            current = [line]
        elif current is not None:
            current.append(line)
        # lines before the first entry header (e.g. a leading `# Meeting
        # history` H1 or blank lines) are ignored.
    if current is not None:
        entries.append("\n".join(current))

    cleaned: list[str] = []
    for entry in entries[:limit]:
        entry = _MEETING_MARKER_RE.sub("", entry)
        cleaned.append(entry.rstrip())
    return cleaned


def build_sweep_prompt(
    code: str,
    elements: tuple[SpineElement, ...],
    *,
    today: date,
    meeting_summaries: list[str] | None = None,
) -> str:
    """Build the LLM prompt for a whole-project sweep synthesis.

    Ranks every element by the Lens score (via `rank_elements`, the canonical
    ranking contract shared with `render_sweep`), then emits a header, a ranked
    element list (every element, title-only for the cold tail; body excerpts for
    the top-N), and an instruction block asking for an across-the-project readout
    that explicitly names cold-but-important threads. Pure: no I/O, no model call.

    When `meeting_summaries` is non-empty, a `## Recent meeting discussion`
    section (those entries, newest first) is inserted before the instruction,
    and the instruction asks the model to ground the synthesis in what was
    actually discussed. When None/empty the prompt is byte-identical to the
    no-meetings form (backward-compatible).
    """
    scored, effective = rank_elements(elements, today=today)

    lines: list[str] = [
        f"# Project sweep — {code}",
        f"Date: {today.isoformat()}",
        f"Elements: {len(elements)}",
        "",
        "## Ranked elements (hot → cold)",
    ]

    for rank, (score, e) in enumerate(scored):
        eff_status = effective[e.id]
        meta_parts = [f"·{e.layer}", f"status={eff_status}"]
        if e.stage:
            meta_parts.append(f"stage={e.stage}")
        if e.target_date:
            meta_parts.append(f"due={e.target_date}")
        if e.serves:
            meta_parts.append(f"serves={', '.join(e.serves)}")
        lines.append(
            f"- [{score:0.2f}] {e.title}  ({' '.join(meta_parts)})"
        )
        # Only the top-N by score get a body excerpt — see _EXCERPT_LIMIT.
        if rank < _EXCERPT_LIMIT and e.body.strip():
            lines.append(f"    excerpt: {_excerpt(e.body)}")

    if meeting_summaries:
        lines += ["", "## Recent meeting discussion (newest first)"]
        for entry in meeting_summaries:
            lines += ["", entry]

    instruction = (
        f"Write a concise across-the-project readout (synthesis) for "
        f"{code}: where the project stands, what's active, and what's due "
        "next. Then explicitly name the cold threads that still want "
        "attention — unresolved asks, stalled deliverables, and decisions "
        "never closed. The ranking above is a relevance Lens, not a "
        "priority order: a low-scored (cold) element can still be the most "
        "important thing to re-heat, so call those out by name. Prose, no "
        "preamble."
    )
    if meeting_summaries:
        instruction += (
            " Ground the synthesis in what was actually discussed in the "
            "recent meetings above, not only the elements' current state — "
            "let the meeting discussion drive what's live and what's stalled."
        )

    instruction += (
        " After the prose, if and only if any element's recorded status or "
        "thinking appears to have drifted from the recent discussion (a "
        "decision now superseded, a brief no longer reflected in current "
        "direction, a deliverable whose stage looks stale), append a fenced "
        "yaml block listing them:\n"
        "```yaml\n"
        "drift:\n"
        "  - element_id: <id>\n"
        "    field: <status|stage|thinking>\n"
        "    observation: <one sentence on the apparent drift>\n"
        "```\n"
        "Omit the block entirely if nothing has drifted. Use the exact "
        "element ids from the ranked list above."
    )

    lines += ["", "## Instruction", instruction]

    return "\n".join(lines)


# Match a fenced ```yaml ... ``` (or bare ```) block whose body STARTS with a
# top-level `drift:` key. Anchoring on `drift:` at the fence start (not merely
# "contains") prevents latching onto an earlier illustrative code fence in the
# prose and spanning across it. Non-greedy body so we stop at the first close.
_DRIFT_FENCE_RE = re.compile(
    r"\n?[ \t]*```(?:yaml)?[ \t]*\n(?P<body>[ \t]*drift:.*?)\n?```[ \t]*",
    re.DOTALL,
)


def parse_drift(synthesis_text: str) -> tuple[str, list[dict]]:
    """Extract drift items from a sweep synthesis that may carry a trailing
    fenced ```yaml drift: ...``` block.

    Returns ``(clean_prose, drift_items)`` where ``clean_prose`` is the
    synthesis with the drift block removed (trailing whitespace trimmed) and
    ``drift_items`` is a list of ``{"element_id","field","observation"}`` dicts
    (possibly empty). Best-effort: a malformed or absent block yields
    ``(original_text, [])`` — drift is advisory, never load-bearing.

    Each item defaults ``field`` to ``"thinking"`` and ``observation`` to ``""``
    when absent; items lacking an ``element_id`` are skipped.
    """
    match = _DRIFT_FENCE_RE.search(synthesis_text)
    if not match:
        return synthesis_text, []

    try:
        parsed = yaml.safe_load(match.group("body"))
    except yaml.YAMLError:
        return synthesis_text, []
    if not isinstance(parsed, dict) or "drift" not in parsed:
        return synthesis_text, []

    raw_items = parsed.get("drift") or []
    if not isinstance(raw_items, list):
        return synthesis_text, []

    items: list[dict] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        element_id = raw.get("element_id")
        if not element_id:
            continue
        items.append({
            "element_id": str(element_id),
            "field": str(raw.get("field") or "thinking"),
            "observation": str(raw.get("observation") or ""),
        })

    clean = (synthesis_text[: match.start()] + synthesis_text[match.end():]).rstrip()
    return clean, items


@dataclass(frozen=True)
class SweepResult:
    """The output of a whole-project sweep: an LLM synthesis plus the ranked
    table that produced it. The table is always present (pure/cheap); the
    synthesis is a placeholder for empty spines (no LLM call was made)."""

    synthesis_text: str
    ranked_table: str
    drift_items: tuple = ()


def _hydrate_retrospective_body(elements, tenant_root):
    """Return `elements` with the Retrospective element's body read from disk.

    Elements loaded from MC-2 (the canonical read path) carry an empty body —
    `row_to_element` sets `body=""`. The meeting-summaries feature needs the
    Retrospective body, so we read it from the element's on-disk file
    (`tenant_root / rel_path`). Best-effort: if there's no Retrospective
    element, no tenant_root, or the file is unreadable, elements are returned
    unchanged (the feature degrades to a no-op, never raises).
    """
    if tenant_root is None:
        return elements
    from pathlib import Path

    out = []
    changed = False
    for e in elements:
        if e.layer == "Retrospective" and not e.body and e.path is not None:
            try:
                text = (Path(tenant_root) / e.path).read_text(encoding="utf-8")
                # Strip frontmatter so .body matches the parse_element contract.
                if text.startswith("---"):
                    end = text.find("\n---", 3)
                    if end != -1:
                        text = text[text.find("\n", end + 1) + 1 :]
                out.append(replace(e, body=text))
                changed = True
                continue
            except OSError:
                pass
        out.append(e)
    return tuple(out) if changed else elements


def run_sweep(
    code: str,
    elements: tuple[SpineElement, ...],
    *,
    today: date,
    llm: Callable[[str], str],
    tenant_root=None,
) -> SweepResult:
    """Run a whole-project sweep: rank, synthesize (via the injected `llm`),
    and return the synthesis + the ranked table.

    The `llm` is injected (`Callable[[str], str]`) so this is testable without a
    live model: the CLI wraps the real call, tests pass a fake.

    `tenant_root`, when given, hydrates the Retrospective element's body from
    disk (MC-2-loaded elements have empty bodies) so recent meeting summaries
    reach the prompt. Omitted in pure tests that build bodies directly.

    Empty spines skip the LLM entirely (don't pay for nothing) — design B: an
    empty project would otherwise burn a call asking the model to synthesize a
    project with no content. The ranked table is always included (pure/cheap).
    """
    table = render_sweep(code, elements, today=today)
    if not elements:
        return SweepResult(
            synthesis_text="(no spine elements to sweep)",
            ranked_table=table,
        )
    elements = _hydrate_retrospective_body(elements, tenant_root)
    meetings = recent_meeting_summaries(elements)
    prompt = build_sweep_prompt(
        code, elements, today=today, meeting_summaries=meetings
    )
    synthesis = llm(prompt)
    clean, drift = parse_drift(synthesis)
    return SweepResult(
        synthesis_text=clean,
        ranked_table=table,
        drift_items=tuple(drift),
    )
