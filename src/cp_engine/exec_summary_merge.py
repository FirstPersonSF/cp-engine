"""Merge named fields into a project CP's ``exec-summary`` region.

WHY PER-FIELD AND NOT WHOLE-REGION. The obvious shape — take the whole region
as one markdown block and splice it — mirrors ``capture_session`` and keeps the
engine/model seam trivially clean. The tenant's own history says it is the
wrong shape. Measured over 60 days, of 45 exec-summary rewrites:

    26  touched exactly ONE field   (58%)
     5  touched two
    10  rewrote all eight

So the dominant real use is "update Status, leave the rest alone". Under a
whole-region verb, a caller who sends only the field it meant to change
silently blanks the other seven — turning the COMMON case into the destructive
one. Merging by field makes a partial update the safe default and a full
rewrite still possible (name every field).

It also makes optimistic concurrency unnecessary. A hosted write that touches
only the fields it names cannot clobber a wrap-up authored five minutes
earlier; the untouched fields survive by construction. Measured: 137 rewrites
in 90 days produced 6 same-DAY collisions and zero same-minute ones, so a
read-back-first round trip would have bought nothing.

THE SEAM THIS RESPECTS. ``docs/plans/2026-06-30-exec-summary.md`` gives the
engine *scaffold + read + render* and the model *all prose*. Nothing here
generates text: every value written arrives from the caller. This module only
decides where it lands.
"""

from __future__ import annotations

import logging
import re
from datetime import date

from cp_engine.render import (
    EXEC_SUMMARY_AUTHORED_FIELDS,
    EXEC_SUMMARY_REGION,
    slice_exec_summary_region,
    splice_managed_region,
)

logger = logging.getLogger(__name__)

# `**Label:** inline value` — the field's own line. Mirrors the reader in
# `render.exec_summary_placeholder_fields`; a value that trips one trips the
# other, which is what keeps writer and reader agreeing about field identity.
_FIELD_RE = re.compile(r"^\*\*(?P<label>[^*]+?):\*\*\s*(?P<value>.*)$")

# The heading immediately above the region, carrying the freshness stamp that
# `summary.exec_summary_updated_on` reads. A merge that changed a field but
# left this stale would report the summary as older than it is.
_STAMP_RE = re.compile(
    r"^(?P<prefix>##\s+Exec Summary\s*·\s*updated\s+)(?P<date>\d{4}-\d{2}-\d{2})",
    re.MULTILINE,
)


class ExecSummaryMergeError(Exception):
    """The merge could not be applied; the file is left untouched."""


def merge_exec_summary_fields(
    cp_md_text: str,
    fields: dict[str, str | list[str]],
    *,
    today: date,
) -> tuple[str, tuple[str, ...]]:
    """Return (new cp.md text, the field labels actually changed).

    `fields` maps a label from ``EXEC_SUMMARY_AUTHORED_FIELDS`` to its new
    content: a string for an inline field (``Status``), or a list of strings
    for a bulleted one (``Where it stands``). A label absent from the mapping
    is left exactly as it was — that is the whole point of this module.

    The `· updated` stamp is advanced to `today` only when something actually
    changed, so a no-op merge does not manufacture freshness.

    Raises `ExecSummaryMergeError` when the region is missing or a label is
    not a recognised field. Both are caller errors worth surfacing, not
    silently absorbing: a typo'd label would otherwise report success while
    writing nothing.
    """
    region = slice_exec_summary_region(cp_md_text)
    if region is None:
        raise ExecSummaryMergeError(
            "no exec-summary region in this cp.md — the region is scaffolded "
            "by `cxp sync`; run it for this project first"
        )

    unknown = [k for k in fields if k not in EXEC_SUMMARY_AUTHORED_FIELDS]
    if unknown:
        raise ExecSummaryMergeError(
            f"unknown exec-summary field(s): {', '.join(sorted(unknown))}. "
            f"Valid: {', '.join(EXEC_SUMMARY_AUTHORED_FIELDS)}"
        )
    if not fields:
        return cp_md_text, ()

    new_region, changed = _apply_fields(region, fields)
    if not changed:
        return cp_md_text, ()

    updated = splice_managed_region(cp_md_text, EXEC_SUMMARY_REGION, new_region)
    updated = _STAMP_RE.sub(
        lambda m: f"{m.group('prefix')}{today.isoformat()}", updated, count=1
    )
    return updated, changed


def _apply_fields(
    region: str, fields: dict[str, str | list[str]]
) -> tuple[str, tuple[str, ...]]:
    """Rewrite only the named fields inside `region`, preserving everything else.

    Walks the region line by line. On a field line whose label is named in
    `fields`, the inline value is replaced and any bullets beneath it are
    dropped (they belong to that field) before the new content is emitted.
    Lines belonging to fields NOT named are copied through byte-for-byte —
    including blank lines, ordering, and any hand-written prose the template
    never produced.
    """
    out: list[str] = []
    changed: list[str] = []
    skipping_bullets_for: str | None = None

    for raw in region.splitlines():
        stripped = raw.strip()
        m = _FIELD_RE.match(stripped)

        if m is not None:
            label = m.group("label").strip()
            skipping_bullets_for = None
            if label in fields:
                value = fields[label]
                rendered = _render_field(label, value)
                if rendered != _existing_field_block(region, label):
                    changed.append(label)
                out.extend(rendered)
                # Its old bullets, if any, are superseded by `rendered`.
                skipping_bullets_for = label
                continue
            out.append(raw)
            continue

        # A bullet under a field being replaced is dropped; anything else
        # (including bullets under untouched fields) is preserved verbatim.
        if skipping_bullets_for is not None:
            if stripped.startswith("- "):
                continue
            if not stripped:
                # A blank line ends the field's block. Keep it, and stop
                # swallowing bullets — the next ones belong to something else.
                skipping_bullets_for = None
                out.append(raw)
                continue
            skipping_bullets_for = None

        out.append(raw)

    return "\n".join(out), tuple(dict.fromkeys(changed))


def _render_field(label: str, value: str | list[str]) -> list[str]:
    """The lines for one field: inline value, or a label with bullets beneath."""
    if isinstance(value, str):
        return [f"**{label}:** {value.strip()}"]
    bullets = [f"- {str(v).strip()}" for v in value if str(v).strip()]
    return [f"**{label}:**", *bullets]


def _existing_field_block(region: str, label: str) -> list[str]:
    """The current lines for `label`, so a no-op merge can be detected.

    Returned in the same shape `_render_field` produces, which is what makes
    the comparison meaningful — an identical value must not count as a change,
    or every sync would advance the freshness stamp without new content.
    """
    block: list[str] = []
    collecting = False
    for raw in region.splitlines():
        stripped = raw.strip()
        m = _FIELD_RE.match(stripped)
        if m is not None:
            if collecting:
                break
            if m.group("label").strip() == label:
                collecting = True
                block.append(stripped)
            continue
        if collecting:
            if stripped.startswith("- "):
                block.append(stripped)
                continue
            if not stripped:
                break
            break
    return block
