"""Warn-only whole-file word-count discipline lint.

`CLAUDE.md` states the tenant's word-count discipline as two thresholds on a
CP file:

    > 2,500 words  →  duplication audit on next wrap-up
    > 3,500 words  →  archive rotation forced before commit

…and claims "The engine enforces both checks during `cp render` and
`wrap up`." NOTE the `wrap up` half is still aspirational: the only caller
is `cxp render` (via `cli_cmds.core`). `/cp-wrapup`'s word-count step directs
the model to act on what `cp render` reported; nothing re-measures at wrap-up.
Until this module, nothing did: the only whole-file measurement
anywhere in the engine was `_MAX_PER_PROJECT_CP_CHARS` in
`plan_from_account_meeting.py`, which is characters, caps a planning payload,
and never surfaces to a human. `exec_summary_lint` budgets *fields inside*
the Exec Summary region — its own docstring defers to "the word-count
discipline checks [that] only look at the whole file", i.e. to this module.

The failure mode that motivated it: a rule documented as machine-enforced
suppresses the manual check that would otherwise catch it. Agents skip the
duplication audit at `wrap up` because they expect a warning that never
comes, so files drift past both thresholds unnoticed.

WHAT IS MEASURED (#308): the AUTHORED text — hand-written prose outside
every engine-managed region, plus the Exec Summary. Every other region —
anything between `<!-- cp-engine:start X -->` and its end marker — is
excluded from the threshold, the way `meetings/` is exempt. The warning
fired on files whose words sat in regions no human may edit: slt-5196 was
5,398 words with ~350 hand-written, and one heavy ingest week tripled its
strips. Rotating hand text cannot clear a strip, so a count that includes
strips gives advice nobody can take.

The `exec-summary` region is the one exception, and it is COUNTED (Drew's
call, after #308 first excluded it). It sits inside a cp-engine marker, but
sync never writes it: it is authored at wrap up, by a human or a model, and
an author can trim it. Excluding it let a bloated Exec Summary grow without
the whole-file check ever noticing — `exec_summary_lint`'s per-field budgets
bound each field, not the region. The marker says who may splice; it does
not say who wrote the words.

Findings carry a contributor breakdown beneath the threshold line — three
buckets (Exec Summary / engine strips / hand-written) as information, then
the biggest hand-written sections, then the biggest entries in the worst
section, since that is where a trim can happen. That exists
because the bare warning sent a human to guess: three files crossed the
threshold in three days, three different guesses were made before anyone
measured, and one guess was written into a CP file as fact and was wrong.

WARN ONLY. Callers echo the lines and always exit 0 — consistent with
`spine-lint`, `exec-lint`, and `commitments-sweep`, none of which block.
Note that `CLAUDE.md` says rotation is "forced before commit"; nothing here
forces anything, and the template wording is corrected to match (see #204).

Exemption: per-meeting artifacts under any `meetings/` directory are
explicitly exempt per `CLAUDE.md` — a fixed per-meeting record (synthesis +
verbatim transcript) is legitimately long and must not be audited or
rotated. `spine/Retrospective/meeting-history.md` is exempt for the same
reason: it is an append-only meeting ledger, not a CP surface. Without these
the lint would fire constantly on files no one should trim.
"""

from __future__ import annotations

import re
from pathlib import Path

# Thresholds, per `CLAUDE.md`'s "Word-count discipline" section.
AUDIT_THRESHOLD_WORDS = 2_500
ROTATE_THRESHOLD_WORDS = 3_500

# Path parts that exempt a file from the discipline. `meetings/` is named in
# `CLAUDE.md`; the retrospective ledger is the same shape under a different
# name.
_EXEMPT_PARTS = frozenset({"meetings", "inactive"})
_EXEMPT_NAMES = frozenset({"meeting-history.md"})

# Any engine-managed region: `<!-- cp-engine:start <name> --> … end`. Matched
# here rather than imported from `render`/`sync` because this module is a pure
# text pass with no MC-2 or config dependency, and `sync._extract_region`
# raises on a missing marker where this must degrade quietly.
_REGION_RE = re.compile(
    r"<!-- cp-engine:start ([\w-]+) -->(.*?)<!-- cp-engine:end \1 -->", re.S
)
# The one region whose words ARE counted: authored at wrap up, not by sync.
_EXEC_SUMMARY_REGION = "exec-summary"

# How many contributors to name. Three buckets plus the worst few entries is
# enough to point at the cause without reprinting the file.
_TOP_SECTIONS = 3
_TOP_ENTRIES = 3


def hand_authored(text: str) -> str:
    """`text` with every engine-managed region removed, markers included.

    An unterminated start marker is NOT a region (the pattern needs its end),
    so a malformed file keeps its words counted rather than hiding them.
    """
    return _REGION_RE.sub("", text or "")


def hand_authored_words(text: str) -> int:
    """Words outside every engine-managed region — the hand-written bucket."""
    return _word_count(hand_authored(text))


def _exec_summary_words(text: str) -> int:
    """Words inside the `exec-summary` region(s); 0 when there is none."""
    return sum(
        _word_count(m.group(2))
        for m in _REGION_RE.finditer(text or "")
        if m.group(1) == _EXEC_SUMMARY_REGION
    )


def counted_words(text: str) -> int:
    """The number the thresholds compare against: hand-written words plus the
    Exec Summary (#308). Every other engine strip is left out."""
    return hand_authored_words(text) + _exec_summary_words(text)


def _word_count(text: str) -> int:
    """Words in `text`, markdown emphasis/backticks stripped.

    Mirrors `exec_summary_lint._word_count` so a file's whole-file count and
    its per-field counts are measured the same way.
    """
    cleaned = text.replace("**", "").replace("__", "").replace("`", "")
    return len(cleaned.split())


def _split_entries(body: str) -> list[str]:
    """Top-level `- ` entries with their indented continuations attached.

    `exec_summary_lint._bullets` is top-level-ONLY by design and silently drops
    sub-bullets, which under-counts exactly the entries that need trimming: the
    mission-control 08-20 Update was 395 words alone and 912 with its
    sub-detail, and 912 is the number that made the file overrun. Splitting
    here (rather than importing `sprints.bullets`, which would pull in `sync`)
    keeps this module dependency-free.
    """
    entries: list[str] = []
    current: list[str] | None = None
    for line in body.splitlines():
        if line.startswith("- "):
            if current is not None:
                entries.append("\n".join(current))
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        entries.append("\n".join(current))
    return entries


def _entry_label(entry: str) -> str:
    """A short handle for one entry — its leading date, else its first words."""
    head = entry.splitlines()[0] if entry.splitlines() else ""
    head = head[2:].strip() if head.startswith("- ") else head.strip()
    # Rolled-up entries are written parenthesised — "(2026-08-25 — …)" and
    # "(Older updates — …)" — so the date still leads once the paren is off.
    head = head.lstrip("(").strip()
    match = re.match(r"(\d{4}-\d{2}-\d{2})", head)
    if match:
        return match.group(1)
    words = head.replace("**", "").split()
    return " ".join(words[:4]) + ("…" if len(words) > 4 else "")


def _sections(text: str) -> list[tuple[str, str]]:
    """Split hand-authored text into `## ` sections: (heading, body).

    Text before the first `## ` heading is returned under "(preamble)".
    """
    out: list[tuple[str, str]] = []
    name, lines = "(preamble)", []
    for line in text.splitlines():
        if line.startswith("## "):
            out.append((name, "\n".join(lines)))
            name, lines = line[3:].strip(), []
        else:
            lines.append(line)
    out.append((name, "\n".join(lines)))
    return out


def contributors(text: str) -> list[str]:
    """Indented lines naming what is actually spending the words.

    Three buckets — the Exec Summary, the other engine-managed strips, and
    hand-written prose — then the biggest hand-written sections, then the
    biggest entries inside the worst one. The Exec Summary and hand-written
    buckets are compared against the thresholds (#308); the strips are printed
    so the reader can see the whole file without mistaking a strip for the
    problem.
    Returns `[]` when nothing can be parsed, so a malformed file degrades to
    the bare threshold warning.

    Why this exists: the warning named a problem and left the diagnosis to a
    human. Three files crossed the threshold in three days, three different
    guesses were made about the cause before measuring, and one of them was
    written into a CP file as fact and was wrong. Measuring takes one pass.
    """
    total = _word_count(text)
    if not total:
        return []

    regions = {m.group(1): m.group(2) for m in _REGION_RE.finditer(text)}
    exec_words = _exec_summary_words(text)
    strip_words = sum(
        _word_count(body)
        for name, body in regions.items()
        if name != _EXEC_SUMMARY_REGION
    )
    hand = hand_authored(text)
    hand_words = _word_count(hand)

    def pct(n: int) -> str:
        return f"{n * 100 // total}%"

    out = [
        f"    exec-summary {exec_words:>6,} ({pct(exec_words)}) — counted",
        f"    engine strips{strip_words:>6,} ({pct(strip_words)}) — not counted",
        f"    hand-written {hand_words:>6,} ({pct(hand_words)}) — counted",
    ]

    sections = [(n, _word_count(b), b) for n, b in _sections(hand)]
    sections = [s for s in sections if s[1]]
    if not sections:
        return out
    sections.sort(key=lambda s: -s[1])

    named = ", ".join(f"{n} {w:,}" for n, w, _ in sections[:_TOP_SECTIONS])
    out.append(f"      hand-written sections: {named}")

    # The worst section, broken down by entry — this is where the trim happens.
    worst_name, _, worst_body = sections[0]
    entries = [(e, _word_count(e)) for e in _split_entries(worst_body)]
    entries = [e for e in entries if e[1]]
    if len(entries) > 1:
        entries.sort(key=lambda e: -e[1])
        listed = ", ".join(
            f"{_entry_label(e)} ({w:,})" for e, w in entries[:_TOP_ENTRIES]
        )
        out.append(f"      {worst_name} by entry: {listed}")
    return out


def is_exempt(rel: Path) -> bool:
    """True if `rel` (tenant-relative) is outside the discipline."""
    return bool(_EXEMPT_PARTS.intersection(rel.parts)) or rel.name in _EXEMPT_NAMES


def lint_word_count(text: str, label: str) -> list[str]:
    """Threshold warnings for one file's text. Empty when under budget.

    `label` prefixes the finding so tenant-wide output stays attributable.
    Pure function: text in, display-ready strings out. Never edits.
    """
    # Authored words only (#308): hand-written text plus the Exec Summary.
    # Sync's strips can't be trimmed by a human, so counting them produced a
    # warning — and a remedy — that no one could act on; a file whose authored
    # share is under budget is silent.
    words = counted_words(text)
    total = _word_count(text)
    whole = f"; {total:,} whole file" if total != words else ""
    if words > ROTATE_THRESHOLD_WORDS:
        head = (
            f"{label}: ⚠ word-count {words:,} words authored "
            f"(over {ROTATE_THRESHOLD_WORDS:,}{whole}) "
            "— archive rotation due before the next commit; "
            "roll resolved threads into the archive file"
        )
    elif words > AUDIT_THRESHOLD_WORDS:
        head = (
            f"{label}: ⚠ word-count {words:,} words authored "
            f"(over {AUDIT_THRESHOLD_WORDS:,}{whole}) "
            "— duplication audit due at next wrap-up; "
            "look for the same thread restated in two sections"
        )
    else:
        return []

    # Name the contributors. Defensive: a malformed region or an unparseable
    # Exec Summary must degrade to the bare warning, never suppress it — the
    # whole advisory pass in `cli_cmds.core` sits inside a bare
    # `except Exception: pass`, so an exception raised here would silently take
    # the exec-summary warnings down with it too.
    try:
        detail = contributors(text)
    except Exception:  # noqa: BLE001 — diagnosis is a bonus, the warning is not
        detail = []
    # ONE finding per file: the breakdown rides on the threshold line as
    # continuation lines. Callers treat a finding as a unit (they echo it, and
    # `word_count_warnings` sorts by it), so returning extra list entries would
    # interleave one file's breakdown into another's ordering.
    return ["\n".join([head, *detail])] if detail else [head]


def word_count_warnings(root: Path) -> list[str]:
    """Whole-file word-count warnings across the tenant's live CP files.

    Scans every `cp.md` below `root` (sprint files are not measured), skipping `inactive/` dirs, the tenant's
    own top-level files, and anything `is_exempt` rejects. Pure read — never
    edits. Ordered worst-first so the file most over budget leads.

    Each finding is a single string whose first line carries the threshold and
    whose continuation lines carry the contributor breakdown — one finding per
    file, so this ordering stays per-file.
    """
    found: list[tuple[int, str]] = []
    from cp_engine.tenant_walk import walk_tenant

    # Never `root.rglob`: a worktree under the tenant is a second copy of
    # every cp.md and doubled each warning (#325).
    for cp_md in walk_tenant(root, "cp.md"):
        rel = cp_md.relative_to(root)
        if len(rel.parts) < 2 or is_exempt(rel):
            continue
        try:
            text = cp_md.read_text(encoding="utf-8")
        except OSError:
            continue
        for warning in lint_word_count(text, str(rel.parent)):
            found.append((counted_words(text), warning))
    return [w for _, w in sorted(found, key=lambda p: -p[0])]
