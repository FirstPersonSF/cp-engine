"""Word-count rotation: roll aged Exec Summary Updates into the archive file.

WHAT "ROTATION" MEANS HERE (#280 tier 3). The tenant's word-count discipline
says a project `cp.md` over 3,500 authored words is due an "archive rotation",
and `/cp-wrapup` step 1 says to "roll off Updates older than ~4 weeks". Until
this module neither had an implementation: rotation was a hand edit, which is
why a hosted session — no checkout — could report it (`word_count_check`) and
never perform it.

What the tenant has actually been doing by hand, read off its own history
(`1pi-9005-mission-control/cp-archive-2026-09.md`,
`cnc-9004-storyos/cp-archive-2026-0{8,9}.md`): Updates entries leave the
`exec-summary` region VERBATIM and land in `cp-archive-<YYYY-MM>.md` beside the
`cp.md`, under a dated "Rolled off" section. Nothing is rewritten, compacted or
summarised — the archive is the same bytes in a second file. That is the one
move this module automates, and deliberately the only one:

* **Updates entries older than `ROLL_OFF_AFTER_DAYS` (28) move.** The same
  threshold `exec_summary_merge.append_update_entry` already REPORTS as
  `roll_off`, so the advisory and the action agree on what "aged" means.
* **Nothing else moves.** Hand-written sections (`## Project Notes`,
  `## Decisions`) are over budget for reasons only a reader can judge —
  which thread is resolved, which decision still binds. Rotating them by rule
  is the "analysis yields observations, not decisions" failure. The result
  reports what is still over threshold after the move so a human can take it
  from there.
* **Undated and placeholder entries stay.** An entry with no date cannot be
  aged, and a scaffold placeholder is not history.
* **The `· updated` stamp does NOT move.** Rotation changes no field's truth;
  advancing the stamp would manufacture freshness (#249).

NO-LOSS IS CHECKED, NOT ASSUMED. Every non-blank line present before the
rotation, across both files, must be present after it (a multiset
comparison — the moved lines are simply in the other file), and the only
lines allowed to appear from nowhere are the archive's own scaffolding. The
check runs twice: in memory here before anything is written, and again by
the webhook against the clone's `HEAD` before it pushes (`check_rotation_loss`,
modelled on `cxp merge-check`'s ref-vs-working-tree comparison). Either
refuses on a single lost line.

Pure where it can be: `plan_rotation` is text in, text out. `rotate_cp` does
the file I/O for one working dir and never touches git.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from cp_engine.exec_summary_merge import _parse_entries, _updates_block
from cp_engine.render import EXEC_SUMMARY_END, EXEC_SUMMARY_START
from cp_engine.word_count_lint import (
    AUDIT_THRESHOLD_WORDS,
    ROTATE_THRESHOLD_WORDS,
    counted_words,
)

# Same threshold `append_update_entry` reports as `roll_off` (#294).
ROLL_OFF_AFTER_DAYS = 28


class RotationError(Exception):
    """The rotation could not be planned; nothing was written."""


class RotationLossError(RotationError):
    """The planned or written rotation would lose content; nothing may land.

    `lost` names each line present before and missing after; `unexpected`
    names each line that appeared from nowhere (neither moved nor scaffold).
    """

    def __init__(self, lost: list[str], unexpected: list[str]):
        self.lost = lost
        self.unexpected = unexpected
        parts = []
        if lost:
            parts.append(f"{len(lost)} line(s) would be lost: {lost[:3]!r}")
        if unexpected:
            parts.append(
                f"{len(unexpected)} unexpected line(s) would appear: {unexpected[:3]!r}"
            )
        super().__init__("rotation refused — " + "; ".join(parts))


@dataclass(frozen=True)
class MovedEntry:
    """One Updates entry that left `cp.md` for the archive."""

    entry_date: date
    headline: str  # the bullet's first line, trimmed for display


@dataclass(frozen=True)
class RotationPlan:
    """What `plan_rotation` decided. `changed` False means an honest no-op."""

    cp_md_text: str
    archive_text: str
    archive_name: str
    moved: tuple[MovedEntry, ...]
    # Lines the archive gained that did not come from cp.md — the anchor
    # block, title, and the dated section heading. The loss check allows
    # exactly these to appear and nothing else.
    scaffold_lines: tuple[str, ...]
    words_before: int
    words_after: int
    threshold: date

    @property
    def changed(self) -> bool:
        return bool(self.moved)

    def summary(self) -> dict:
        """JSON-able report for a route or CLI."""
        return {
            "changed": self.changed,
            "moved": [
                {"date": m.entry_date.isoformat(), "headline": m.headline}
                for m in self.moved
            ],
            "archive": self.archive_name,
            "older_than": self.threshold.isoformat(),
            "words_before": self.words_before,
            "words_after": self.words_after,
            "over_audit_threshold": self.words_after > AUDIT_THRESHOLD_WORDS,
            "over_rotation_threshold": self.words_after > ROTATE_THRESHOLD_WORDS,
        }


def archive_name_for(today: date) -> str:
    """`cp-archive-<YYYY-MM>.md` — the month the rotation ran, which is how
    the tenant's hand rotations named theirs (entries rolled off 09-17 from
    August and September both went to `cp-archive-2026-09.md`)."""
    return f"cp-archive-{today:%Y-%m}.md"


def _significant(text: str) -> list[str]:
    """Non-blank lines, trailing whitespace dropped — the unit of the loss
    check. Blank lines are layout, and a move legitimately changes them."""
    return [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]


def loss_between(
    before: dict[str, str],
    after: dict[str, str],
    allowed_additions: tuple[str, ...] | list[str] = (),
) -> tuple[list[str], list[str]]:
    """(lost, unexpected) across a set of files, before vs after.

    A multiset comparison over every non-blank line of every file: a line
    that moved from one file to another is neither lost nor unexpected. A
    line present N times before must be present N times after — a duplicated
    entry collapsing to one copy is a loss. `allowed_additions` is the only
    content permitted to appear from nowhere (each listed occurrence once).
    """
    b = Counter(ln for text in before.values() for ln in _significant(text))
    a = Counter(ln for text in after.values() for ln in _significant(text))
    lost = sorted((b - a).elements())
    extra = a - b
    extra.subtract(Counter(ln.rstrip() for ln in allowed_additions if ln.strip()))
    unexpected = sorted(ln for ln, n in extra.items() for _ in range(max(n, 0)))
    return lost, unexpected


def _archive_scaffold(project_label: str, name: str, today: date) -> str:
    return (
        "---\n"
        f"Project: {project_label}\n"
        f"Provenance: Version 01 | {today.isoformat()}\n"
        f"Filename: {name}\n"
        "Author: cp-engine\n"
        "---\n\n"
        f"# Updates archive — {today:%Y-%m}\n\n"
        "Exec Summary Updates rolled off `cp.md` once older than "
        f"{ROLL_OFF_AFTER_DAYS} days. Moved verbatim — nothing here is "
        "superseded. Newest first within each section.\n"
    )


# The entry's OWN date: leading, optionally behind `(`/`*`/`_` decoration
# (`- 2026-09-15 (eve) — …`, `- (2026-09-15 Scoper's baseline …`,
# `- **2026-09-15** — …`). Stricter than `_Entry.entry_date`, which searches
# the whole first line: a pointer bullet like `- _Entries 2026-08-07 and
# earlier rotated to [archive]_` mentions a date without being dated, and
# moving it would strand the link that says where the old entries went.
# An entry whose date is not leading STAYS — the conservative direction.
_LEADING_DATE_RE = re.compile(r"^[\s(*_]*(\d{4}-\d{2}-\d{2})")


def _leading_date(first_body: str) -> date | None:
    m = _LEADING_DATE_RE.match(first_body)
    if m is None:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _entry_stop(lines: list[str], i: int, end: int) -> int:
    """One past the last line of the entry whose bullet is `lines[i]`.

    The same membership rule as `exec_summary_merge._parse_entries`: indented
    lines belong to the entry, and a blank belongs only when indented text
    follows it. An unindented non-bullet line ends the entry — it is not the
    entry's to take into the archive.
    """
    j = i + 1
    while j < end:
        nxt = lines[j]
        if not nxt.strip():
            k = j + 1
            while k < end and not lines[k].strip():
                k += 1
            if k < end and lines[k][:1].isspace() and lines[k].strip():
                j = k
                continue
            break
        if nxt[:1].isspace():
            j += 1
            continue
        break
    return j


def plan_rotation(
    cp_md_text: str,
    archive_text: str | None,
    *,
    today: date,
    project_label: str,
    older_than_days: int = ROLL_OFF_AFTER_DAYS,
) -> RotationPlan:
    """Plan the move of aged Updates entries from `cp_md_text` to the archive.

    `archive_text` is this month's archive file's current content, or None
    when it does not exist yet (it is scaffolded). Pure: nothing is read or
    written. The plan has already passed the loss check — a plan that would
    lose a line raises `RotationLossError` instead of being returned.

    Raises `RotationError` when the file has no exec-summary region or no
    `**Updates:**` field — there is nothing to rotate FROM, and saying so is
    more honest than a silent no-op on a file sync has not scaffolded.
    """
    name = archive_name_for(today)
    threshold = today - timedelta(days=older_than_days)
    words_before = counted_words(cp_md_text)

    start = cp_md_text.find(EXEC_SUMMARY_START)
    end = cp_md_text.find(EXEC_SUMMARY_END, start) if start != -1 else -1
    if start == -1 or end == -1:
        raise RotationError(
            "no exec-summary region in this cp.md — `cxp sync` scaffolds it"
        )
    region_start = start + len(EXEC_SUMMARY_START)
    head, region, tail = (
        cp_md_text[:region_start],
        cp_md_text[region_start:end],
        cp_md_text[end:],
    )
    lines = region.splitlines(keepends=True)
    field_idx, block_end = _updates_block(lines)
    if field_idx is None:
        raise RotationError("no `**Updates:**` field in the exec-summary region")

    entries = _parse_entries(lines, field_idx + 1, block_end)

    # Each entry's RAW span. `_parse_entries` stores continuations stripped;
    # the archive must receive the bytes as written, nested bullets and
    # indentation included — so the span is re-walked here with the same
    # rule it uses for what belongs to an entry.
    spans = [(e.index, _entry_stop(lines, e.index, block_end)) for e in entries]

    drop: set[int] = set()
    moved: list[MovedEntry] = []
    moved_chunks: list[str] = []
    for e, (s, t) in zip(entries, spans):
        d = _leading_date(e.first_body)
        if e.is_placeholder or d is None or (today - d).days <= older_than_days:
            continue
        drop.update(range(s, t))
        chunk = "".join(lines[s:t])
        if not chunk.endswith("\n"):
            chunk += "\n"
        moved_chunks.append(chunk)
        moved.append(MovedEntry(d, e.first_body.strip()[:160]))

    if not moved:
        return RotationPlan(
            cp_md_text, archive_text or "", name, (), (), words_before,
            words_before, threshold,
        )

    new_region = "".join(ln for i, ln in enumerate(lines) if i not in drop)
    new_cp = f"{head}{new_region}{tail}"

    section_heading = f"## Rolled off {today.isoformat()}"
    scaffold = "" if archive_text else _archive_scaffold(project_label, name, today)
    base = (archive_text if archive_text else scaffold).rstrip("\n")
    new_archive = f"{base}\n\n{section_heading}\n\n{''.join(moved_chunks)}"

    scaffold_lines = tuple(_significant(scaffold)) + (section_heading,)
    plan = RotationPlan(
        new_cp, new_archive, name, tuple(moved), scaffold_lines,
        words_before, counted_words(new_cp), threshold,
    )

    lost, unexpected = loss_between(
        {"cp.md": cp_md_text, name: archive_text or ""},
        {"cp.md": plan.cp_md_text, name: plan.archive_text},
        plan.scaffold_lines,
    )
    if lost or unexpected:
        raise RotationLossError(lost, unexpected)
    return plan


def rotate_cp(
    working_dir: Path,
    *,
    today: date,
    older_than_days: int = ROLL_OFF_AFTER_DAYS,
) -> RotationPlan:
    """Rotate one working dir's `cp.md` in place. Returns the plan it applied.

    Writes BOTH files or neither: the plan is computed and loss-checked in
    memory first, so a refusal leaves the directory exactly as found. A
    no-op plan writes nothing. Never touches git — the caller commits.
    """
    cp_md = working_dir / "cp.md"
    if not cp_md.is_file():
        raise RotationError(f"no cp.md in {working_dir}")
    text = cp_md.read_text(encoding="utf-8")
    archive_path = working_dir / archive_name_for(today)
    archive_text = (
        archive_path.read_text(encoding="utf-8") if archive_path.is_file() else None
    )

    plan = plan_rotation(
        text, archive_text, today=today, project_label=working_dir.name,
        older_than_days=older_than_days,
    )
    if plan.changed:
        archive_path.write_text(plan.archive_text, encoding="utf-8")
        cp_md.write_text(plan.cp_md_text, encoding="utf-8")
    return plan


def check_rotation_loss(
    repo_root: Path,
    rel_paths: list[str],
    allowed_additions: tuple[str, ...] | list[str] = (),
    ref: str = "HEAD",
) -> tuple[list[str], list[str]]:
    """(lost, unexpected) for `rel_paths`: `ref`'s version vs the working tree.

    The on-disk half of the no-loss guarantee, modelled on `cxp merge-check`
    (`merge_check.check_merge`): read each file as the reference commit has
    it, read it as the working tree has it, compare. A file absent at `ref`
    (a new archive) contributes nothing before; a file deleted from the tree
    contributes nothing after, so every line it had reports as lost.

    Independent of `plan_rotation`'s in-memory check on purpose — this one
    reads what was actually WRITTEN, so a bug between planning and writing
    (or a write that raced another) is still caught before a push.
    """
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    for rel in rel_paths:
        shown = subprocess.run(
            ["git", "show", f"{ref}:{rel}"],
            cwd=repo_root, capture_output=True, text=True, check=False,
        )
        before[rel] = shown.stdout if shown.returncode == 0 else ""
        path = repo_root / rel
        after[rel] = path.read_text(encoding="utf-8") if path.is_file() else ""
    return loss_between(before, after, allowed_additions)
