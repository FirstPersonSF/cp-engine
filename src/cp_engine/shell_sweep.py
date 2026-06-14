"""Project Shell slice 3 — sweep synthesis (Phase B).

Pure prompt construction for the whole-project sweep: rank every element via
the Lens, then build an LLM prompt asking for an across-the-project readout that
names the cold-but-important threads. The LLM call itself lives in run_sweep
(injectable, Task B2); this module is pure and testable without a model.
"""

from __future__ import annotations

from datetime import date

from cp_engine.shell import (
    ShellElement,
    rank_elements,
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


def build_sweep_prompt(
    code: str,
    elements: tuple[ShellElement, ...],
    *,
    today: date,
) -> str:
    """Build the LLM prompt for a whole-project sweep synthesis.

    Ranks every element by the Lens score (via `rank_elements`, the canonical
    ranking contract shared with `render_sweep`), then emits a header, a ranked
    element list (every element, title-only for the cold tail; body excerpts for
    the top-N), and an instruction block asking for an across-the-project readout
    that explicitly names cold-but-important threads. Pure: no I/O, no model call.
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

    lines += [
        "",
        "## Instruction",
        (
            f"Write a concise across-the-project readout (synthesis) for "
            f"{code}: where the project stands, what's active, and what's due "
            "next. Then explicitly name the cold threads that still want "
            "attention — unresolved asks, stalled deliverables, and decisions "
            "never closed. The ranking above is a relevance Lens, not a "
            "priority order: a low-scored (cold) element can still be the most "
            "important thing to re-heat, so call those out by name. Prose, no "
            "preamble."
        ),
    ]

    return "\n".join(lines)
