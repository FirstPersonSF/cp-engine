"""Warn-only whole-file word-count discipline lint.

`CLAUDE.md` states the tenant's word-count discipline as two thresholds on a
CP file:

    > 2,500 words  →  duplication audit on next wrap-up
    > 3,500 words  →  archive rotation forced before commit

…and claims "The engine enforces both checks during `cp render` and
`wrap up`." Until this module, nothing did: the only whole-file measurement
anywhere in the engine was `_MAX_PER_PROJECT_CP_CHARS` in
`plan_from_account_meeting.py`, which is characters, caps a planning payload,
and never surfaces to a human. `exec_summary_lint` budgets *fields inside*
the Exec Summary region — its own docstring defers to "the word-count
discipline checks [that] only look at the whole file", i.e. to this module.

The failure mode that motivated it: a rule documented as machine-enforced
suppresses the manual check that would otherwise catch it. Agents skip the
duplication audit at `wrap up` because they expect a warning that never
comes, so files drift past both thresholds unnoticed.

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

from pathlib import Path

# Thresholds, per `CLAUDE.md`'s "Word-count discipline" section.
AUDIT_THRESHOLD_WORDS = 2_500
ROTATE_THRESHOLD_WORDS = 3_500

# Path parts that exempt a file from the discipline. `meetings/` is named in
# `CLAUDE.md`; the retrospective ledger is the same shape under a different
# name.
_EXEMPT_PARTS = frozenset({"meetings", "inactive"})
_EXEMPT_NAMES = frozenset({"meeting-history.md"})


def _word_count(text: str) -> int:
    """Words in `text`, markdown emphasis/backticks stripped.

    Mirrors `exec_summary_lint._word_count` so a file's whole-file count and
    its per-field counts are measured the same way.
    """
    cleaned = text.replace("**", "").replace("__", "").replace("`", "")
    return len(cleaned.split())


def is_exempt(rel: Path) -> bool:
    """True if `rel` (tenant-relative) is outside the discipline."""
    return bool(_EXEMPT_PARTS.intersection(rel.parts)) or rel.name in _EXEMPT_NAMES


def lint_word_count(text: str, label: str) -> list[str]:
    """Threshold warnings for one file's text. Empty when under budget.

    `label` prefixes the finding so tenant-wide output stays attributable.
    Pure function: text in, display-ready strings out. Never edits.
    """
    words = _word_count(text)
    if words > ROTATE_THRESHOLD_WORDS:
        return [
            f"{label}: ⚠ word-count {words:,} words (over {ROTATE_THRESHOLD_WORDS:,}) "
            "— archive rotation due before the next commit; "
            "roll resolved threads into the archive file"
        ]
    if words > AUDIT_THRESHOLD_WORDS:
        return [
            f"{label}: ⚠ word-count {words:,} words (over {AUDIT_THRESHOLD_WORDS:,}) "
            "— duplication audit due at next wrap-up; "
            "look for the same thread restated in two sections"
        ]
    return []


def word_count_warnings(root: Path) -> list[str]:
    """Whole-file word-count warnings across the tenant's live CP files.

    Scans every `cp.md` below `root`, skipping `inactive/` dirs, the tenant's
    own top-level files, and anything `is_exempt` rejects. Pure read — never
    edits. Ordered worst-first so the file most over budget leads.
    """
    found: list[tuple[int, str]] = []
    for cp_md in sorted(root.rglob("cp.md")):
        rel = cp_md.relative_to(root)
        if len(rel.parts) < 2 or is_exempt(rel):
            continue
        try:
            text = cp_md.read_text(encoding="utf-8")
        except OSError:
            continue
        for warning in lint_word_count(text, str(rel.parent)):
            found.append((_word_count(text), warning))
    return [w for _, w in sorted(found, key=lambda p: -p[0])]
