"""Managed-region provenance: tell an engine write from a foreign one.

Architecture plan step 2 ("one machine writer"). The rule in CLAUDE.md —
content between ``<!-- cp-engine:start X -->`` / ``<!-- cp-engine:end X -->``
is owned by the engine — was enforced only by a Claude Code PreToolUse hook
(``hooks/guard-engine-regions.py``). Every other writer (a person in an
editor, the webhook, CI, an LLM plan verb that appended in the wrong place —
#263) could put text inside a region, and the next render discarded it
without a word.

THE QUESTION THE GUARD HAS TO ANSWER is not "does the region differ from what
the engine would render now" — it differs on almost every render, because the
data moved (see the long note in sprints.py above
``_warn_on_discarded_carry_forward``). It is "did the engine write what is on
disk now?". Nothing recorded that, so this module records it: every region
the engine splices carries, as its last inner line, a digest of the content
the engine wrote::

    <!-- cp-engine:start carry-forward -->
    - [ ] an ask
    <!-- cp-engine:digest 3f2a91c04b7e -->
    <!-- cp-engine:end carry-forward -->

On the next splice the on-disk body is re-hashed. Equal → the engine wrote it
and replacing it loses nothing. Different → someone else changed it since →
the text is QUARANTINED (``exceptions/region-edits/``, committed with the
render) and a warning names it before the splice replaces it. No digest
(a region last written before this change, or by a full-file template render)
→ "unstamped": no claim either way, and the splice stamps it.

Lives INSIDE the markers on purpose: the start/end marker grammar, every
reader that slices a region by those markers, and all hand-written territory
outside them are unchanged; the Claude Code hook already blocks edits to it.

Exempt: ``exec-summary`` — the one marker-wrapped region that is AUTHORED
(people at /cp-wrapup, ``capture_project_state``, ``draft-summaries``). Same
set as the hook's ``_AUTHORED_REGIONS``. It is never stamped or guarded.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

#: Marker-wrapped but authored, not engine-owned. Mirrors
#: hooks/guard-engine-regions.py ``_AUTHORED_REGIONS``.
AUTHORED_REGIONS: frozenset[str] = frozenset({"exec-summary"})

#: Where preserved edits land, relative to the tenant root. Under
#: ``exceptions/`` (engine-surfaced, committed) rather than ``.cp-engine/``
#: (gitignored — a CI runner's quarantine would vanish with the runner).
QUARANTINE_DIR = Path("exceptions") / "region-edits"

_DIGEST_LINE_RE = re.compile(r"^[ \t]*<!-- cp-engine:digest ([0-9a-f]{12}) -->[ \t]*$", re.M)
# The whole digest line INCLUDING its newline, so removing it leaves the
# surrounding content byte-for-byte as the engine rendered it.
_DIGEST_LINE_FULL_RE = re.compile(
    r"^[ \t]*<!-- cp-engine:digest [0-9a-f]{12} -->[ \t]*(?:\n|\Z)", re.M
)
_REGION_RE = re.compile(
    r"<!-- cp-engine:start (?P<name>[\w-]+) -->(?P<body>.*?)<!-- cp-engine:end (?P=name) -->",
    re.S,
)


def is_guarded(region: str) -> bool:
    return region not in AUTHORED_REGIONS


def strip_digest(inner: str) -> str:
    """``inner`` without its digest line(s). Readers that show a region's
    content verbatim (briefs, viewers) call this; parsers can ignore it."""
    if "cp-engine:digest" not in inner:
        return inner
    return _DIGEST_LINE_FULL_RE.sub("", inner)


def _normalize(inner: str) -> str:
    # Trailing whitespace and edge blank lines are not authorship: an editor
    # that trims on save must not make the engine's own text look foreign.
    lines = [ln.rstrip() for ln in strip_digest(inner).splitlines()]
    return "\n".join(lines).strip("\n")


def digest(inner: str) -> str:
    return hashlib.sha256(_normalize(inner).encode("utf-8")).hexdigest()[:12]


def stamp_of(inner: str) -> str | None:
    """The digest the engine left in ``inner``, or None if unstamped."""
    found = _DIGEST_LINE_RE.findall(inner)
    return found[-1] if found else None


def stamp(body: str) -> str:
    """``body`` (no markers) with the engine's digest as its last line.

    The body itself is written byte-for-byte (trailing double-space line
    breaks included); only the HASH is computed over the normalized form."""
    clean = strip_digest(body).strip("\n")
    line = f"<!-- cp-engine:digest {digest(clean)} -->"
    return f"{clean}\n{line}" if clean.strip() else line


def provenance(inner: str) -> str:
    """``"engine"`` (stamp matches), ``"foreign"`` (stamp present, content
    changed since) or ``"unstamped"`` (no claim)."""
    s = stamp_of(inner)
    if s is None:
        return "unstamped"
    return "engine" if s == digest(inner) else "foreign"


def regions(text: str) -> dict[str, str]:
    """``{name: inner}`` for every well-formed region in ``text`` (first
    occurrence of a name wins; duplicates are the splicer's problem)."""
    out: dict[str, str] = {}
    for m in _REGION_RE.finditer(text):
        out.setdefault(m.group("name"), m.group("body"))
    return out


@dataclass(frozen=True)
class ForeignEdit:
    path: str            # tenant-relative when known
    region: str
    discarded: str       # the foreign inner content, digest line stripped
    replacement: str     # what replaced it
    writer: str          # "cxp sync" / "cxp render" / "webhook" / ...
    quarantined: str | None = None   # tenant-relative quarantine path


def find_tenant_root(path: Path) -> Path | None:
    p = path.resolve()
    for cand in (p, *p.parents):
        if (cand / ".cp-engine.toml").is_file():
            return cand
    return None


def quarantine(
    *,
    root: Path,
    rel_path: str,
    region: str,
    discarded: str,
    replacement: str,
    writer: str,
    now: datetime | None = None,
) -> Path:
    """Write the discarded text to ``exceptions/region-edits/`` and return the
    file. Idempotent per (file, region, content): the same edit found by two
    renders lands in the same file rather than piling up copies."""
    now = now or datetime.now(timezone.utc)
    body = strip_digest(discarded).strip("\n")
    key = hashlib.sha256(f"{rel_path}\0{region}\0{body}".encode()).hexdigest()[:10]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", rel_path.replace("/", "__"))
    name = f"{now:%Y-%m-%d}--{slug}--{region}--{key}.md"
    out = root / QUARANTINE_DIR / name
    # Same content found again on a later day → keep the first record.
    existing = list((root / QUARANTINE_DIR).glob(f"*--{slug}--{region}--{key}.md"))
    if existing:
        return existing[0]
    out.parent.mkdir(parents=True, exist_ok=True)
    fence = "````"
    out.write_text(
        "---\n"
        f"Project: region-edit quarantine\n"
        f"Provenance: Version 01 | {now:%Y-%m-%d}\n"
        f"Filename: {name}\n"
        "Author: cp-engine\n"
        "---\n\n"
        "# Managed-region edit preserved\n\n"
        f"- **File:** `{rel_path}`\n"
        f"- **Region:** `{region}`\n"
        f"- **Found by:** {writer}, {now:%Y-%m-%dT%H:%MZ}\n\n"
        "The engine owns this region and re-renders it. The text below was in "
        "it but was not written by the engine, so the render replaced it. Move "
        "what still matters outside the markers, or into the source the region "
        "is rendered from, then delete this file.\n\n"
        f"## Edit that was replaced\n\n{fence}markdown\n{body}\n{fence}\n\n"
        f"## Rendered in its place\n\n{fence}markdown\n"
        f"{strip_digest(replacement).strip(chr(10))}\n{fence}\n",
        encoding="utf-8",
    )
    return out


def report(
    *,
    source: Path | str | None,
    region: str,
    discarded: str,
    replacement: str,
    writer: str,
    hint: str | None = None,
) -> ForeignEdit:
    """Quarantine (when the tenant root can be found) and warn LOUDLY.

    ``logger.warning`` is the channel on purpose: sync's warning counter
    carries it onto the outcome line, the only place ``cxp sync`` output
    reaches a person (#197/#212). Never raises — a guard that crashes a
    render is worse than the defect it guards.
    """
    rel = str(source) if source is not None else "<unknown file>"
    qpath: str | None = None
    try:
        if source is not None:
            src = Path(source)
            root = find_tenant_root(src)
            if root is not None:
                try:
                    rel = str(src.resolve().relative_to(root))
                except ValueError:
                    pass
                q = quarantine(
                    root=root, rel_path=rel, region=region,
                    discarded=discarded, replacement=replacement, writer=writer,
                )
                qpath = str(q.relative_to(root))
    except Exception:  # noqa: BLE001
        logger.exception("region guard: could not quarantine %s#%s", rel, region)
    kept = {ln.strip() for ln in strip_digest(replacement).splitlines()}
    body_lines = [ln for ln in strip_digest(discarded).splitlines() if ln.strip()]
    lost = [ln for ln in body_lines if ln.strip() not in kept] or body_lines[:1]
    logger.warning(
        "MANAGED-REGION EDIT REPLACED: %s region `%s` held %d line(s) the engine "
        "did not write; the render replaced them. %s%s First: %s",
        rel, region, len(lost),
        f"Preserved in {qpath}." if qpath else "COULD NOT PRESERVE IT — copy it from git history.",
        f" {hint}" if hint else "",
        (lost[0] if lost else "")[:160],
    )
    return ForeignEdit(
        path=rel, region=region, discarded=strip_digest(discarded),
        replacement=replacement, writer=writer, quarantined=qpath,
    )


def stamp_all(text: str) -> str:
    """Stamp every guarded region in a freshly rendered full body, in the
    exact shape ``splice_managed_region`` writes (``start\\n<body>\\n<digest>\\nend``),
    so a file created whole from a template and the same file re-spliced
    later are byte-identical (resync stays a no-op)."""
    if "cp-engine:start" not in text:
        return text

    def _one(m: re.Match) -> str:
        name = m.group("name")
        if not is_guarded(name):
            return m.group(0)
        body = stamp(m.group("body"))
        return (
            f"<!-- cp-engine:start {name} -->\n{body}\n"
            f"<!-- cp-engine:end {name} -->"
        )

    return _REGION_RE.sub(_one, text)
