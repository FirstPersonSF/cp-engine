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

import dataclasses
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


# Leading integer after an optional case-insensitive "v": "v7", "V2", "2",
# "v2.1", "v2 (note)" all yield their leading int. Canonical labels are
# `v<int>` (_HEADER_RE enforces that on disk), but DB labels have historically
# drifted by hand-edit, so ranking must tolerate the drifted shapes.
_VERSION_NUM_RE = re.compile(r"\s*v?\s*(\d+)", re.IGNORECASE)


def version_number(label) -> int:
    """The leading version integer of a version_label, or -1 when unparseable.

    Shared version-ordering primitive (#113): both the spine read path (dedupe
    duplicate live rows to the latest version) and the substance mirror (shield
    a newer authored live version from a stale disk 'live') need to compare
    version labels numerically — "v10" must beat "v9", which string comparison
    gets wrong. Parses "v7", "V2", bare "2", "v2.1" → 2, "v2 (note)" → 2.
    Tolerant of None/odd labels so a dirty row can't raise — but note the -1
    fallback means a truly unparseable label LOSES to any parseable one
    regardless of recency (callers tie-break on version_date next)."""
    m = _VERSION_NUM_RE.match(str(label or ""))
    return int(m.group(1)) if m else -1


@dataclass(frozen=True)
class SubstanceVersion:
    """One distilled version of a work item's substance."""

    label: str          # "v3"
    date: str           # ISO date string (kept as text)
    status: str         # live | superseded
    framing: str
    sources: tuple[str | dict, ...]
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
    # Spine placement axis — SEPARATE from `binding` (estimate-resolution). A
    # substance file sits either on its estimate work item (`item`) or in the
    # project's context shelf (`context`), in which case `serves` names the
    # est_item_ids it informs. `layer` is the spine layer label (free text).
    layer: str | None = None
    # item | context — DERIVED from est_item_id's key shape; see
    # `derive_placement`. The default suits uuid-keyed items; authored
    # builders pass "context" explicitly and the disk reader derives it.
    placement: str = "item"
    serves: tuple[str, ...] = ()
    # UI archive flag (Phase 3): a human can archive a substance item from the
    # MC-2 spine card-dashboard. Default False; omitted from frontmatter when
    # False so untouched files round-trip byte-for-byte.
    archived: bool = False
    # Ordered progress trail (mig 119): element-scoped steps, each a dict
    # {position, title, status, date, note}. Element-level (not per-version), so
    # it lives in frontmatter. Empty tuple = no trail; omitted from frontmatter
    # when empty so untouched files round-trip byte-for-byte.
    steps: tuple[dict, ...] = ()
    # Unknown frontmatter keys (Phase 2: mc2 row id, confirmed_at, …) preserved
    # verbatim across parse->render so a render pass never wipes them.
    extra: dict = dataclasses.field(default_factory=dict)

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


AUTHORED_KEY_PREFIX = "_authored/"


def derive_placement(est_item_id: str) -> str:
    """Where an element renders: ``context`` (the shelf) or ``item`` (a slot).

    DERIVED, never authored. An ``_authored/<slug>`` element has no estimate
    slot by construction and belongs on the context shelf; a uuid-keyed element
    IS a slot and belongs in ``phases[].items``. Every write path already emits
    exactly this (`spine_inbox`, `spine_recover`, `authored_mirror`); this
    function is the single place that says so.

    Measured 2026-08-13 across all 401 live `spine_substance` rows: 368
    ``_authored/*`` -> context, 32 uuid -> item, ONE exception — and that row
    (`e94d0a03`) was itself a defect, uuid-keyed with a stale `binding` and no
    `rel_path`. Nothing landed in `outline.unbound`.

    NOT derivable from `binding`, which is the tempting mistake: 155 rows are
    `binding='live'` AND `placement='context'` — interview write-ups, client
    feedback, source documents that SERVE real work without OCCUPYING a slot.
    Keying on binding would relocate all 155 into the phase items, which is the
    "everything is a card" failure #179 removed.
    """
    return "context" if str(est_item_id).startswith(AUTHORED_KEY_PREFIX) else "item"


def is_skipped_spine_dir(parts: tuple[str, ...]) -> bool:
    """True if a ``spine/<...>`` path should NOT be read as a work-item substance
    file: project context (``_context/``), generated authored mirrors
    (``_authored/``, DB-owned, never read back), and frozen snapshots.

    Shared by both disk readers (`spine_substance_sync._load_substance_items` and
    `spine_inbox._iter_substance_files`) so their skip scoping can't drift —
    an authored mirror file flowing into either reader would let DB-owned rows be
    re-read disk→DB (substance sync) or be promoted as a real candidate (inbox)."""
    return parts[0] in ("_context", "_authored") or any(
        p.endswith(".snapshots") for p in parts
    )


def _parse_version_section(section: str, path: Path) -> SubstanceVersion:
    """Parse one `## v<N> …` section (header line + framing/sources + body)."""
    lines = section.splitlines()
    if not lines:
        raise ValueError(f"{path}: empty version section")
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

    # The `framing:` line and `sources:` list form a small head, terminated by
    # the first blank line; everything after is the distilled body. `framing` is
    # FREE-FORM PROSE (may contain colons, #, quotes, em dashes) — it is read as
    # a LITERAL line, not YAML; only the `sources:` list goes through YAML.
    rest = lines[1:]
    blank = next((i for i, ln in enumerate(rest) if ln.strip() == ""), len(rest))
    head_lines = rest[:blank]
    body = "\n".join(rest[blank + 1 :]).strip()

    framing = ""
    sources_lines: list[str] = []
    for i, ln in enumerate(head_lines):
        if ln.startswith("framing:"):
            framing = ln[len("framing:") :].strip()
        elif ln.startswith("sources:"):
            sources_lines = head_lines[i:]
            break

    if sources_lines:
        try:
            parsed = yaml.safe_load("\n".join(sources_lines)) or {}
        except yaml.YAMLError as exc:
            raise ValueError(
                f"{path}: version {label} has malformed sources: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"{path}: version {label} sources is not a mapping"
            )
        # Keep mappings as mappings: a source is `{id, title, type}` in MC-2
        # and consumers (stub_sweep, asset_dedupe) branch on `isinstance(dict)`.
        # `str(s)` used to flatten them to a Python repr, so a write→read→write
        # cycle corrupted the file a second way beyond the indent bug (#216).
        sources = tuple(
            s if isinstance(s, dict) else str(s)
            for s in _as_tuple(parsed.get("sources"))
        )
    else:
        sources = ()

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
    layer = None if meta.get("layer") is None else str(meta["layer"])
    # DERIVED from the key shape — a `placement:` in frontmatter is ignored
    # (kept in _KNOWN below so it doesn't surface as an unknown-key `extra`).
    placement = derive_placement(est_item_id)
    serves = tuple(str(s) for s in _as_tuple(meta.get("serves")))
    archived = bool(meta.get("archived", False))
    steps_raw = meta.get("steps") or []
    steps = tuple(dict(s) for s in steps_raw) if isinstance(steps_raw, list) else ()

    _KNOWN = {
        "est_item_id", "est_item_kind", "binding", "phase",
        "layer", "placement", "serves", "archived", "steps",
    }
    extra = {k: v for k, v in meta.items() if k not in _KNOWN}

    # Split the body into version sections on version-header lines ONLY. Bodies
    # may contain their own `## ` subheadings (e.g. `## Key risks`), so we split
    # on the version-header shape, not on every `## `. The split puts everything
    # before the first version header into sections[0] (the preamble).
    sections = re.split(r"(?m)^(?=##\s+v\d+\s*—)", post.content)
    preamble, sections = sections[0], sections[1:]
    if preamble.strip():
        raise ValueError(
            f"{path}: non-whitespace content before the first version "
            f"section: {preamble.strip()[:60]!r}"
        )
    versions = tuple(_parse_version_section(s, path) for s in sections if s.strip())

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
        layer=layer,
        placement=placement,
        serves=serves,
        archived=archived,
        steps=steps,
        extra=extra,
    )


def _yaml_scalar(value: object) -> str:
    """Render one scalar the way YAML would, quoting only when necessary, so it
    round-trips through `yaml.safe_load` (used to parse the `sources:` list)."""
    dumped = yaml.safe_dump(value, allow_unicode=True, default_flow_style=False)
    # safe_dump emits e.g. "src:with-colon\n...\n" for a bare string; strip the
    # trailing document markers/newlines to get the inline scalar form.
    return dumped.rstrip("\n").removesuffix("\n...").rstrip("\n")


def _yaml_list_item(value: object, indent: str = "  ") -> str:
    """Render one `sources:` entry as a YAML block-sequence item.

    A source is either a plain string or a mapping (`{id, title, type}` — the
    shape `spine_substance.sources` holds in MC-2). `_yaml_scalar` dumps a
    mapping to MULTIPLE lines; prefixing only the first with "- " left the rest
    at column 0, which YAML then rejects with "mapping values are not allowed
    here" (#216). Every continuation line has to be indented under the dash.
    """
    dumped = _yaml_scalar(value)
    first, *rest = dumped.split("\n")
    out = [f"{indent}- {first}"]
    # Continuation lines sit two columns further in, aligning under the text
    # that follows the "- " marker.
    out.extend(f"{indent}  {ln}" if ln else "" for ln in rest)
    return "\n".join(out)


def _render_version(v: SubstanceVersion) -> str:
    """Render one version back to the canonical `## v<N> …` shape.

    `framing` is emitted as a LITERAL line (verbatim) — safe because the parser
    reads it literally too. `sources` is emitted as a YAML list so items with
    special chars (colons, quotes) round-trip cleanly.
    """
    lines = [f"## {v.label} — {v.date} · {v.status}"]
    lines.append(f"framing: {v.framing}")
    lines.append("sources:")
    for s in v.sources:
        lines.append(_yaml_list_item(s))
    lines.append("")
    lines.append(v.body)
    return "\n".join(lines)


def render_substance(item: WorkItemSubstance) -> str:
    """Serialize a WorkItemSubstance back to canonical markdown.

    Round-trips `parse_substance` output byte-for-byte modulo a single trailing
    newline (compare with `.rstrip("\\n")`). Frontmatter keys are emitted in the
    canonical order; `phase` is omitted when absent.
    """
    # Build the frontmatter in canonical key order (known keys first, then any
    # preserved unknown keys in sorted order) and render via yaml.safe_dump so
    # values with special chars (colons, quotes) are escaped correctly.
    fm: dict = {
        "est_item_id": item.est_item_id,
        "est_item_kind": item.est_item_kind,
    }
    if item.phase is not None:
        fm["phase"] = item.phase
    fm["binding"] = item.binding
    if item.layer is not None:
        fm["layer"] = item.layer
    # Omit the default placement so files that never set it round-trip byte-for-
    # byte (mirrors how `phase` is omitted when absent). `context` is emitted.
    #
    # Still emitted even though placement is now DERIVED on read: 255 of the
    # 256 on-disk files carrying `placement:` are `_authored/*`-keyed, so
    # `derive_placement` reproduces the written value exactly. Emitting keeps
    # them byte-for-byte stable; dropping it would rewrite 256 files to remove
    # a line that is now merely redundant rather than wrong. (The 256th lives
    # under `_authored/` but is uuid-KEYED — `e94d0a03`, the measurement's one
    # exception; it is a defect, tracked on #182, not a counter-example.)
    if item.placement != "item":
        fm["placement"] = item.placement
    if item.serves:
        fm["serves"] = list(item.serves)
    # Omit the default False so untouched files round-trip byte-for-byte
    # (mirrors how `placement` is omitted when "item").
    if item.archived:
        fm["archived"] = True
    # The ordered progress trail (mig 119). Emit each step as a plain dict in a
    # stable key order so the YAML is legible + diff-friendly; omit when empty.
    if item.steps:
        fm["steps"] = [
            {k: s[k] for k in ("position", "title", "status", "date", "note")
             if k in s}
            for s in item.steps
        ]
    for k in sorted(item.extra):
        fm[k] = item.extra[k]

    fm_text = yaml.safe_dump(
        fm, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).rstrip("\n")
    parts = ["---", fm_text, "---"]
    body = "\n\n".join(_render_version(v) for v in item.versions)
    return "\n".join(parts) + "\n" + body + "\n"


def add_version(
    item: WorkItemSubstance, version: SubstanceVersion
) -> WorkItemSubstance:
    """Return a new item with `version` prepended (newest-first).

    When the new version is `live`, any prior `live` version is demoted to
    `superseded`, preserving the exactly-one-live invariant. Frozen dataclasses
    → returns fresh instances via `dataclasses.replace`.
    """
    prior = item.versions
    if version.status == "live":
        prior = tuple(
            dataclasses.replace(v, status="superseded")
            if v.status == "live"
            else v
            for v in prior
        )
    return dataclasses.replace(item, versions=(version, *prior))
