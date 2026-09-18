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
from dataclasses import dataclass
from datetime import date, timedelta

from cp_engine.render import (
    EXEC_SUMMARY_AUTHORED_FIELDS,
    EXEC_SUMMARY_END,
    EXEC_SUMMARY_REGION,
    EXEC_SUMMARY_START,
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
    """The lines for one field: inline value, or a label with bullets beneath.

    A MULTI-LINE STRING IS TREATED AS BULLETS, not as an inline value. On
    2026-09-15 a hosted caller sent `Blockers` as one string with embedded
    newlines — five `- ` bullets in a single blob — because the verb's
    signature typed it `str`. Rendered inline, the label swallowed the first
    bullet:

        **Blockers:** - **Scoper P1 baseline has not run.** …
        - **Scoper P3+ is gated…**

    The remaining four survived only because they already carried their own
    prefixes. Nothing was lost, but the field read wrong in a file six
    consumers treat as project truth.

    A string containing a newline can only be a caller who meant a list, so it
    is split rather than refused: this is the same judgement as coercing a
    bare `account_summary` string (#254) — a caller who is 99% right should
    not lose a write to a wrapper.
    """
    if isinstance(value, str):
        text = value.strip()
        if "\n" in text:
            # Split on lines, stripping any leading bullet marker the caller
            # already supplied so it is not doubled.
            items = [
                re.sub(r"^[-*]\s+", "", line.strip())
                for line in text.splitlines()
                if line.strip()
            ]
            return [f"**{label}:**", *(f"- {i}" for i in items if i)]
        return [f"**{label}:** {text}"]
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


# The date of an Updates entry. The convention is `- YYYY-MM-DD — prose`,
# but real files carry `- 2026-09-15 (eve) — …`, the rolled-off compaction
# `- (2026-09-15 Scoper's baseline ran …` and `- (Older updates — 2026-08-17 …`.
# So the date is SEARCHED for — first in the prefix before the ` — ` dash,
# then anywhere in the first line — rather than anchored at column 0.
_ENTRY_DATE_RE = re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})")


def _normalise(text: str) -> str:
    """Whitespace-insensitive form for comparing entry prose.

    Collapses every run of whitespace (spaces, tabs, newlines) to one space
    and strips the ends. An internal double space, a trailing space, or a
    continuation line re-wrapped by an editor must not defeat the dedupe —
    the entry is identified by what it SAYS, and none of those change that.
    """
    return " ".join(text.split())


def _entry_prose(first_line_body: str) -> str:
    """The prose of an entry line, with the date prefix removed.

    `body` is the bullet line without its `- ` marker. The prefix ends at the
    first ` — `; a line with no dash is all prose (a hand-written entry that
    skipped the date convention still deduplicates on its text).
    """
    head, sep, tail = first_line_body.partition(" — ")
    return tail if sep else head


@dataclass(frozen=True)
class RollOffReport:
    """Entries older than the roll-off threshold — REPORTED, never removed.

    `dates` is in file order (newest first when the file follows its own
    convention). `count` is what a caller surfaces as the cue to run a local
    wrap-up; the entries themselves stay put.
    """

    threshold: date
    dates: tuple[date, ...]

    @property
    def count(self) -> int:
        return len(self.dates)


@dataclass(frozen=True)
class UpdateAppendResult:
    """Result of `append_update_entry`.

    UNPACKS AS A 2-TUPLE. The verb shipped (#281) returning `(text, changed)`
    and the webhook caller destructures it that way. Adding the roll-off
    report as a third tuple member would turn every `text, changed = ...` into
    a `ValueError`, so the report rides as an attribute and iteration keeps
    the two-value contract: `text, changed = result` still works, and
    `result.roll_off` is there for a caller that wants the advisory.
    """

    text: str
    changed: bool
    roll_off: RollOffReport

    def __iter__(self):
        return iter((self.text, self.changed))


@dataclass(frozen=True)
class _Entry:
    """One top-level bullet in the Updates block, with its continuation."""

    index: int  # line index (within the region) of the bullet line
    first_body: str  # the bullet line minus `- `
    continuation: tuple[str, ...]  # nested/indented lines that belong to it
    is_placeholder: bool

    @property
    def entry_date(self) -> date | None:
        head, sep, _ = self.first_body.partition(" — ")
        m = _ENTRY_DATE_RE.search(head if sep else "") or _ENTRY_DATE_RE.search(
            self.first_body
        )
        if m is None:
            return None
        try:
            return date.fromisoformat(m.group("date"))
        except ValueError:
            return None

    @property
    def normalised_first_line(self) -> str:
        return _normalise(_entry_prose(self.first_body))

    @property
    def normalised_full(self) -> str:
        return _normalise(
            " ".join([_entry_prose(self.first_body), *self.continuation])
        )

    def matches(self, wanted: str) -> bool:
        """`wanted` is already normalised.

        Either shape counts: a caller re-posting a multi-paragraph entry sends
        the whole thing; a caller re-posting its headline sends only the
        first line. Both are the same entry, and neither may land twice.
        """
        return wanted in (self.normalised_first_line, self.normalised_full)


def _updates_block(lines: list[str]) -> tuple[int | None, int]:
    """(index of the `**Updates:**` line or None, index where its block ends).

    The block runs to the next AUTHORED field line or the end of the region —
    NOT to the first blank line. Real files carry blanks mid-block (a
    multi-paragraph entry, an editor's spacing) and every entry after one is
    still an Updates entry; a window that stopped there let a verbatim
    re-post of the last entry through as "new".

    A field line is one at column 0 whose label is in
    `EXEC_SUMMARY_AUTHORED_FIELDS`. An indented `  **Also:** …` paragraph
    inside a long entry has the field SHAPE, and cp-context-protocol has one
    — reading it as a boundary hid six entries behind it.
    """
    start: int | None = None
    for i, raw in enumerate(lines):
        if raw[:1].isspace():
            continue
        m = _FIELD_RE.match(raw.strip())
        if m is None:
            continue
        label = m.group("label").strip()
        if start is None:
            if label == "Updates":
                start = i
            continue
        if label in EXEC_SUMMARY_AUTHORED_FIELDS:
            return start, i
    return start, len(lines)


def _parse_entries(lines: list[str], begin: int, end: int) -> list[_Entry]:
    """Top-level bullets in `lines[begin:end]`, each with its continuation.

    A top-level entry is an UNINDENTED `- ` line. Anything indented beneath
    it — a nested `  - sub` bullet, a wrapped continuation, an indented
    paragraph after a blank — belongs to that entry, which is how markdown
    reads it and how a reader does too. Those lines are never entries of
    their own, so a nested bullet is not compared, counted, or re-emitted
    as one.
    """
    entries: list[_Entry] = []
    i = begin
    while i < end:
        raw = lines[i].rstrip("\r\n")
        if not raw.startswith("- "):
            i += 1
            continue
        body = raw[2:]
        cont: list[str] = []
        j = i + 1
        while j < end:
            nxt = lines[j].rstrip("\r\n")
            if nxt.strip() == "":
                # A blank continues the entry only if indented text follows.
                k = j + 1
                while k < end and lines[k].strip() == "":
                    k += 1
                if k < end and lines[k][:1].isspace() and lines[k].strip():
                    j = k
                    continue
                break
            if nxt[:1].isspace():
                cont.append(nxt.strip())
                j += 1
                continue
            break
        entries.append(
            _Entry(
                index=i,
                first_body=body,
                continuation=tuple(cont),
                is_placeholder=body.strip().startswith("_<") and body.strip().endswith(">_"),
            )
        )
        i = j
    return entries


def append_update_entry(
    cp_md_text: str,
    entry: str,
    *,
    today: date,
    roll_off_after_days: int = 28,
) -> UpdateAppendResult:
    """Add ONE dated entry to `Updates`, newest first.

    Returns an `UpdateAppendResult` that unpacks as `(text, changed)` and
    carries `roll_off` (see `RollOffReport`).

    WHY THIS IS NOT JUST `merge_exec_summary_fields({"Updates": [...]})` (#281).
    Every other authored field REPLACES wholesale — that is the documented
    contract, and it is right for them, because a rewrite of `Next up` means
    the old list is wrong. `Updates` is the one field where the old entries are
    the point: they are the project's narrative. Handing it replace semantics
    makes the caller resend the entire history to add one line, and dropping
    one is silent.

    So the hosted verb takes an APPEND, not a list. The failure it removes is
    the one #251 is about — a caller who cannot safely add to the log stops
    adding to it.

    WHY IT IS AN INSERTION, NOT A RE-RENDER (#294). The first cut collected the
    existing bullets and re-rendered the field through the replace path. A
    round-trip of real tenant files showed what that costs: a nested
    `  - sub` bullet came back at top level, and the blank line before the
    end marker vanished. Neither was a lost entry, but a verb that rewrites
    lines it did not touch is one the next reader cannot trust. So the new
    line is spliced in directly after `**Updates:**` and every other byte of
    the file is left exactly where it was.

    Entries are `- <YYYY-MM-DD> — <prose>`; the date is stamped here rather
    than accepted, so a caller cannot backdate the record.

    ROLL-OFF IS REPORTED, NOT PERFORMED. The CLI ritual rolls entries older
    than ~4 weeks into the sprint file — a move BETWEEN two files, which needs
    a checkout this server does not have. Doing half of it (deleting here,
    writing nowhere) would destroy the narrative it exists to keep, so old
    entries stay and `roll_off_after_days` only shapes the advisory a caller
    can surface: `result.roll_off` counts the entries older than the
    threshold and lists their dates. The count is the caller's cue to run a
    local wrap-up, never this function's licence to delete.

    A duplicate entry (same text, whitespace-insensitive, anywhere in the
    Updates block) is a NO-OP: it neither rewrites the file nor advances the
    `· updated` stamp, so a retry after a timeout is safe and a scheduled
    caller cannot manufacture freshness.

    The scaffold's `- _<dated — …>_` seed bullet is dropped the first time a
    real entry lands; a placeholder is not history.
    """
    entry = (entry or "").strip().lstrip("-").strip()
    if not entry:
        raise ExecSummaryMergeError(
            "an empty Updates entry would advance the freshness stamp while "
            "saying nothing"
        )

    start = cp_md_text.find(EXEC_SUMMARY_START)
    end = cp_md_text.find(EXEC_SUMMARY_END, start) if start != -1 else -1
    if start == -1 or end == -1:
        raise ExecSummaryMergeError(
            "no exec-summary region in this cp.md — the region is scaffolded "
            "by `cxp sync`; run it for this project first"
        )
    region_start = start + len(EXEC_SUMMARY_START)
    before, region, after = (
        cp_md_text[:region_start],
        cp_md_text[region_start:end],
        cp_md_text[end:],
    )

    lines = region.splitlines(keepends=True)
    field_idx, block_end = _updates_block(lines)
    if field_idx is None:
        raise ExecSummaryMergeError(
            "no `**Updates:**` field in the exec-summary region — the field is "
            "scaffolded by `cxp sync`; run it for this project first"
        )

    entries = _parse_entries(lines, field_idx + 1, block_end)
    threshold = today - timedelta(days=roll_off_after_days)
    roll_off = RollOffReport(
        threshold=threshold,
        dates=tuple(
            d for e in entries
            if not e.is_placeholder
            and (d := e.entry_date) is not None
            and (today - d).days > roll_off_after_days
        ),
    )

    # Dedupe on the TEXT, not the whole line. Comparing the dated line meant a
    # retry that crossed midnight appended a second copy of the same entry —
    # exactly the case a scheduled caller hits, and the one a no-op guard
    # exists to cover. An entry is identified by what it says.
    wanted = _normalise(entry)
    if any(e.matches(wanted) for e in entries if not e.is_placeholder):
        return UpdateAppendResult(cp_md_text, False, roll_off)

    field_line = lines[field_idx]
    eol = field_line[len(field_line.rstrip("\r\n")):] or "\n"
    new_line = f"- {today.isoformat()} — {entry}{eol}"

    # Drop the scaffold seed(s) — the placeholder bullet and a placeholder
    # inline value on the field line. Real content is left exactly as found.
    drop = {e.index for e in entries if e.is_placeholder}
    m = _FIELD_RE.match(field_line.strip())
    if m is not None and "_<" in m.group("value"):
        indent = field_line[: len(field_line) - len(field_line.lstrip())]
        field_line = f"{indent}**Updates:**{eol}"

    out: list[str] = []
    for i, raw in enumerate(lines):
        if i in drop:
            continue
        if i == field_idx:
            out.append(field_line)
            out.append(new_line)
            continue
        out.append(raw)

    updated = f"{before}{''.join(out)}{after}"
    updated = _STAMP_RE.sub(
        lambda m: f"{m.group('prefix')}{today.isoformat()}", updated, count=1
    )
    return UpdateAppendResult(updated, True, roll_off)
