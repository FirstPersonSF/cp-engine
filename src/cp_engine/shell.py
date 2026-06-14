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

from cp_engine.state import INACTIVE_DIR_NAME
from cp_engine.sync import _SCOPE_DIRS, _find_project_dir, _project_parent_dirs

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


# Status weights — a settled element stays in consideration but ranks below
# live work (design §2: "deliverable ships → its elements demote together";
# "cold is a dimmer, not an off-switch"). A freshly-delivered `final` artifact
# should sit just under the still-open work it served, not tie it.
_STATUS_WEIGHT: dict[str, float] = {
    "active": 1.0,
    "reference": 0.7,
    "dormant": 0.7,
    "final": 0.5,
}


def _status_term(status: str) -> float:
    return _STATUS_WEIGHT.get(status, 1.0)


def score_element(el: ShellElement, active: set[str], today: date) -> float:
    """The Lens score: recency × serves-active × layer-importance × status
    (design §2). The status term demotes settled (final/reference/dormant)
    elements below live ones without dropping them out of the sweep."""
    recency = _recency_term(el.last_touched, today)
    serves = _serves_active_term(el, active)
    importance = LAYER_IMPORTANCE.get(el.layer, 0.5)
    status = _status_term(el.status)
    return recency * serves * importance * status


class ShellDirNotFound(Exception):
    """No working dir matching the given code under any scope root."""


def find_shell_dir(tenant_root: Path, code: str) -> Path:
    """Locate a project's working dir from its code, offline (no MC-2).

    Reuses sync's scope/account-nesting model (`_project_parent_dirs`) and
    prefix-matcher (`_find_project_dir`) so this stays in lockstep with how
    sync itself lays out the tree. Raises ShellDirNotFound if nothing matches.
    """
    if code == INACTIVE_DIR_NAME:
        # Never resolve the inactive bin itself as a project.
        raise ShellDirNotFound(f"'{code}' is not a valid project code")
    for scope in _SCOPE_DIRS:
        for parent in _project_parent_dirs(tenant_root, scope):
            hit = _find_project_dir(parent, code)
            if hit is not None:
                return hit
    raise ShellDirNotFound(
        f"No working dir for '{code}' under {', '.join(_SCOPE_DIRS)}/"
    )


def _glyph(score: float) -> str:
    """Cosmetic tier glyph for the sweep — helps the eye scan hot vs cold."""
    if score >= 0.66:
        return "◆"  # hot
    if score >= 0.33:
        return "◇"  # warm
    return "○"  # cold


def render_sweep(
    code: str,
    elements: tuple[ShellElement, ...],
    *,
    today: date,
) -> str:
    """Render the full ranked sweep (design §2 'whole-project sweep' mode).

    Every element, hot and cold, ranked by Lens score descending. Read-only.
    """
    active = active_deliverable_ids(elements)
    scored = sorted(
        ((score_element(e, active, today), e) for e in elements),
        key=lambda pair: (-pair[0], pair[1].layer, pair[1].id),
    )
    lines = [f"{code} — full sweep ({len(elements)} elements)"]
    if not elements:
        lines.append("  (no shell/ elements — has this project been backfilled?)")
        return "\n".join(lines)
    for score, e in scored:
        suffix_parts: list[str] = []
        if e.stage:
            suffix_parts.append(f"[{e.stage}]")
        if e.target_date:
            due_date = _parse_date(e.target_date)
            if due_date is not None and due_date < today:
                suffix_parts.append(f"due {e.target_date} (overdue)")
            else:
                suffix_parts.append(f"due {e.target_date}")
        if e.status in ("reference", "dormant"):
            suffix_parts.append(f"({e.status})")
        suffix = ("  " + " ".join(suffix_parts)) if suffix_parts else ""
        lines.append(
            f"  {score:0.2f} {_glyph(score)} {e.title}  ·{e.layer}{suffix}"
        )
        if e.serves:
            lines.append(f"        ← serves: {', '.join(e.serves)}")
    return "\n".join(lines)
