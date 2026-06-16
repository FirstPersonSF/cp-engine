"""Work-item substance — distilled, buildable project memory bound to an
estimate work item (spine estimate-binding, Phase 1).

The estimate (estimator schema → activities/deliverables) is the spine's
backbone. A *substance* file is the distilled, human-confirmed memory that hangs
off one estimate work item — the few hundred words you can actually build from,
versioned so the line of work is legible over time. Markdown is the source of
truth; git is the version history.

Canonical substance file format::

    ---
    est_item_id: <phase_deliverable uuid>
    est_item_kind: deliverable        # activity | deliverable
    phase: Phase 0 Discovery & Alignment
    binding: live                     # live | unbound
    ---
    ## v3 — 2026-06-11 · live
    framing: two-track AI story is the spine — pull Engage/Execute/Extend
    sources:
      - janet-5-27-transcript
      - carol-deck#p12-18

    <300–600 word distilled body you can build from>

    ## v2 — 2026-04-23 · superseded
    framing: core-team synthesis pass
    sources:
      - kickoff-transcript

    <body>

Each `## v<N> — <YYYY-MM-DD> · <status>` header (em dash `—`, middle dot `·`)
opens a version section. The leading `framing:` line and `sources:` YAML list
are parsed off; the rest is the distilled body. Versions are kept newest-first
and exactly one must be `live`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import frontmatter
import yaml

# Closed status vocabulary for a version.
_VERSION_STATUSES: frozenset[str] = frozenset({"live", "superseded"})

# `## v3 — 2026-06-11 · live` — tolerant of surrounding whitespace.
_HEADER_RE = re.compile(
    r"^##\s+(?P<label>v\d+)\s*—\s*(?P<date>\d{4}-\d{2}-\d{2})\s*·\s*(?P<status>\S+)\s*$"
)


@dataclass(frozen=True)
class SubstanceVersion:
    """One distilled version of a work item's substance."""

    label: str          # "v3"
    date: str           # ISO date string (kept as text)
    status: str         # live | superseded
    framing: str
    sources: tuple[str, ...]
    body: str


@dataclass(frozen=True)
class WorkItemSubstance:
    """The full substance bound to one estimate work item (newest-first
    versions). Exactly one version is `live`."""

    est_item_id: str
    est_item_kind: str  # activity | deliverable
    phase: str | None
    binding: str        # live | unbound
    versions: tuple[SubstanceVersion, ...]
    path: Path

    def live_version(self) -> SubstanceVersion:
        """Return the single `live` version (invariant enforced at parse)."""
        for v in self.versions:
            if v.status == "live":
                return v
        raise ValueError(f"{self.path}: substance has no live version")


def _as_tuple(value: object) -> tuple:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def _parse_version_section(section: str, path: Path) -> SubstanceVersion:
    """Parse one `## v<N> …` section (header line + framing/sources + body)."""
    lines = section.splitlines()
    m = _HEADER_RE.match(lines[0].strip())
    if m is None:
        raise ValueError(
            f"{path}: malformed version header: {lines[0]!r} "
            "(expected '## v<N> — <YYYY-MM-DD> · <status>')"
        )
    label, date, status = m.group("label"), m.group("date"), m.group("status")
    if status not in _VERSION_STATUSES:
        raise ValueError(
            f"{path}: version {label} has unknown status '{status}' "
            f"(expected one of {sorted(_VERSION_STATUSES)})"
        )

    # The `framing:` line and `sources:` list form a small YAML head, terminated
    # by the first blank line; everything after is the distilled body.
    rest = lines[1:]
    blank = next((i for i, ln in enumerate(rest) if ln.strip() == ""), len(rest))
    head_text = "\n".join(rest[:blank])
    body = "\n".join(rest[blank + 1 :]).strip()

    try:
        head = yaml.safe_load(head_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(
            f"{path}: version {label} has malformed framing/sources: {exc}"
        ) from exc
    if not isinstance(head, dict):
        raise ValueError(
            f"{path}: version {label} framing/sources is not a mapping"
        )

    framing = "" if head.get("framing") is None else str(head["framing"])
    sources = tuple(str(s) for s in _as_tuple(head.get("sources")))
    return SubstanceVersion(
        label=label, date=date, status=status,
        framing=framing, sources=sources, body=body,
    )


def parse_substance(path: Path) -> WorkItemSubstance:
    """Parse one substance markdown file into a WorkItemSubstance."""
    try:
        post = frontmatter.load(str(path))
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: failed to parse frontmatter: {exc}") from exc
    meta = post.metadata

    def _required(key: str) -> str:
        try:
            return str(meta[key])
        except KeyError:
            raise ValueError(
                f"{path}: substance missing required key '{key}'"
            ) from None

    est_item_id = _required("est_item_id")
    est_item_kind = _required("est_item_kind")
    binding = str(meta.get("binding", "live"))
    phase = None if meta.get("phase") is None else str(meta["phase"])

    # Split the body into version sections on `## ` header lines.
    sections = re.split(r"(?m)^(?=##\s)", post.content)
    versions = tuple(
        _parse_version_section(s, path)
        for s in sections
        if s.strip().startswith("##")
    )

    live_count = sum(1 for v in versions if v.status == "live")
    if live_count != 1:
        raise ValueError(
            f"{path}: substance must have exactly one live version "
            f"(found {live_count})"
        )

    return WorkItemSubstance(
        est_item_id=est_item_id,
        est_item_kind=est_item_kind,
        phase=phase,
        binding=binding,
        versions=versions,
        path=path,
    )


