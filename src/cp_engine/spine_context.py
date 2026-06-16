"""Context elements — project-level material that surrounds the estimate work
items (spine estimate-binding, Phase 1).

Where a *substance* file binds distilled memory to one estimate work item, a
*context element* is project-level: a brief, an agreement, a stakeholder, a
decision, or a source document. It links (many-to-many, optional) to the
estimate items it informs, but it is not owned by any single one. Markdown is
the source of truth; later phases mirror these to MC-2.

Context file format::

    ---
    type: source                 # brief | agreement | stakeholder | decision | source
    provenance: client           # client | we-found | third-party-analyst | public  (source only)
    nature: framework            # optional: research | brand | framework | feedback | brief-input
    links:                       # optional many-to-many to estimate item ids
      - <phase_deliverable uuid>
    ---
    <distilled context body>

Both vocabularies are CLOSED: an out-of-vocab `type` or `provenance` is a parse
error. `provenance` is meaningful only for `source` (absent → None elsewhere);
`nature` and `links` are optional.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import frontmatter
import yaml

# Closed type vocabulary (design: the context kinds that surround the spine).
_CONTEXT_TYPES: frozenset[str] = frozenset(
    {"brief", "agreement", "stakeholder", "decision", "source"}
)

# Closed provenance vocabulary — meaningful only for `source` elements.
_PROVENANCES: frozenset[str] = frozenset(
    {"client", "we-found", "third-party-analyst", "public"}
)


@dataclass(frozen=True)
class ContextElement:
    """One project-level context element (frontmatter + distilled body)."""

    type: str                       # brief | agreement | stakeholder | decision | source
    provenance: str | None          # source-only; None for other types
    nature: str | None              # research | brand | framework | feedback | brief-input
    links: tuple[str, ...]          # estimate item ids this context informs
    body: str
    path: Path
    title: str


def _as_tuple(value: object) -> tuple:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return (value,)


def parse_context(path: Path) -> ContextElement:
    """Parse one context element markdown file into a ContextElement."""
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
                f"{path}: context element missing required key '{key}'"
            ) from None

    ctype = _required("type")
    if ctype not in _CONTEXT_TYPES:
        raise ValueError(
            f"{path}: unknown context type '{ctype}' "
            f"(expected one of {sorted(_CONTEXT_TYPES)})"
        )

    provenance = None if meta.get("provenance") is None else str(meta["provenance"])
    if provenance is not None and provenance not in _PROVENANCES:
        raise ValueError(
            f"{path}: unknown provenance '{provenance}' "
            f"(expected one of {sorted(_PROVENANCES)})"
        )

    nature = None if meta.get("nature") is None else str(meta["nature"])
    links = tuple(str(x) for x in _as_tuple(meta.get("links")))

    return ContextElement(
        type=ctype,
        provenance=provenance,
        nature=nature,
        links=links,
        body=post.content,
        path=path,
        title=str(meta.get("title", path.stem)),
    )
