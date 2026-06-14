"""Project Shell — slice 1 (markdown-only, read-only Lens).

A shell *element* is a markdown file under
`1p/<acct>/<proj>/shell/<Layer>/<name>.md` whose YAML frontmatter is the
structured spine (design §3). This module parses those files and computes the
Lens relevance score (design §2). No MC-2, no writes — slice 1 is read-only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import frontmatter
import yaml

# The 11 fixed layers (dir names == `layer:` values).
LAYERS: tuple[str, ...] = (
    "Brief",
    "Agreement",
    "Research",
    "SourceMaterial",
    "ClientFeedback",
    "Synthesis",
    "Drafts",
    "Deliverables",
    "Decisions",
    "Timeline",
    "Stakeholders",
)

# Layers that are always ambient — relevant regardless of which deliverable is
# active (design §2: "project-framing layer ... always ambient").
FRAMING_LAYERS: frozenset[str] = frozenset(
    {"Brief", "Agreement", "Timeline", "Stakeholders"}
)


@dataclass(frozen=True)
class ShellElement:
    """One addressable unit in a project shell (frontmatter spine + body)."""

    id: str
    project: str
    layer: str
    title: str
    status: str  # active | dormant | final | reference
    last_touched: str  # ISO date string; kept as text (slice-1 simplicity)
    path: Path
    body: str
    type: str | None = None
    stage: str | None = None  # conception|first|revised|final
    fidelity: str | None = None  # text|design|motion
    target_date: str | None = None
    depends_on: tuple[str, ...] = ()
    serves: tuple[str, ...] = ()
    target_history: tuple[dict, ...] = ()
    source: tuple[str, ...] = ()
    author: str | None = None


def _as_tuple(value: object) -> tuple:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def parse_element(path: Path) -> ShellElement:
    """Parse one shell element markdown file into a ShellElement."""
    try:
        post = frontmatter.load(str(path))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: failed to parse frontmatter: {exc}") from exc
    meta = post.metadata

    def _str(key: str) -> str | None:
        v = meta.get(key)
        return None if v is None else str(v)

    def _required(key: str) -> str:
        try:
            return str(meta[key])
        except KeyError:
            raise ValueError(
                f"{path}: shell element missing required key '{key}'"
            ) from None

    return ShellElement(
        id=_required("id"),
        project=_required("project"),
        layer=_required("layer"),
        title=str(meta.get("title", path.stem)),
        status=str(meta.get("status", "active")),
        last_touched=str(meta.get("last_touched", "")),
        path=path,
        body=post.content,
        type=_str("type"),
        stage=_str("stage"),
        fidelity=_str("fidelity"),
        target_date=_str("target_date"),
        depends_on=tuple(str(x) for x in _as_tuple(meta.get("depends_on"))),
        serves=tuple(str(x) for x in _as_tuple(meta.get("serves"))),
        target_history=_as_tuple(meta.get("target_history")),
        source=tuple(str(x) for x in _as_tuple(meta.get("source"))),
        author=_str("author"),
    )


# Layer-importance weights (design open-thread #1, resolved from the IBX
# backfill in a later task). Higher = stays warmer in the ranked sweep.
# Rationale: Deliverables are the project's spine; Decisions and ClientFeedback
# carry the "still wants attention" signal; framing layers
# (Brief/Timeline/Stakeholders) are ambient-important but shouldn't outrank live
# work; raw inputs (SourceMaterial/Research) matter most via `serves`, less on
# their own weight.
LAYER_IMPORTANCE: dict[str, float] = {
    "Deliverables": 1.00,
    "Decisions": 0.85,
    "ClientFeedback": 0.80,
    "Drafts": 0.75,
    "Synthesis": 0.70,
    "Brief": 0.65,
    "Agreement": 0.60,
    "Timeline": 0.60,
    "Stakeholders": 0.55,
    "Research": 0.50,
    "SourceMaterial": 0.45,
}


def load_shell(project_dir: Path) -> tuple[ShellElement, ...]:
    """Parse every shell element under `<project_dir>/shell/<Layer>/*.md`."""
    shell_root = project_dir / "shell"
    if not shell_root.is_dir():
        return ()
    elements: list[ShellElement] = []
    for layer in LAYERS:
        layer_dir = shell_root / layer
        if not layer_dir.is_dir():
            continue
        for md in sorted(layer_dir.glob("*.md")):
            elements.append(parse_element(md))
    return tuple(elements)


def active_deliverable_ids(elements: tuple[ShellElement, ...]) -> set[str]:
    """Ids of deliverables that are the project's live focus.

    Active = a Deliverables-layer element whose stage is not `final` and whose
    status is `active` (not dormant/final/reference). Design §2 coordinate #1.
    """
    return {
        e.id
        for e in elements
        if e.layer == "Deliverables"
        and e.stage != "final"
        and e.status == "active"
    }


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _recency_term(last_touched: str, today: date) -> float:
    """Exponential decay on age in days. ~30-day half-life; floored at 0.1 so
    cold never reaches zero ("a dimmer, not an off-switch", design §2)."""
    d = _parse_date(last_touched)
    if d is None:
        return 0.1
    age_days = max(0, (today - d).days)
    decayed = math.exp(-age_days / 43.28)  # 43.28 ≈ 30-day half-life
    return max(0.1, decayed)


def _serves_active_term(el: ShellElement, active: set[str]) -> float:
    """1.0 if the element serves an active deliverable OR is a framing layer
    (always ambient); 0.35 otherwise (cold but not gone)."""
    if el.layer in FRAMING_LAYERS:
        return 1.0
    if el.layer == "Deliverables" and el.id in active:
        return 1.0
    if any(s in active for s in el.serves):
        return 1.0
    return 0.35


def score_element(el: ShellElement, active: set[str], today: date) -> float:
    """The Lens score: recency × serves-active × layer-importance (design §2)."""
    recency = _recency_term(el.last_touched, today)
    serves = _serves_active_term(el, active)
    importance = LAYER_IMPORTANCE.get(el.layer, 0.5)
    return recency * serves * importance
