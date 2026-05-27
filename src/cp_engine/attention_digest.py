"""Daily attention digest — past-due asks, escalated risks, allocation pressure.

Lever 2 of cp-engine v0.12.0. See:
  cp/docs/plans/2026-05-27-clickup-bidirectional-and-daily-digest-design.md

Pure classifiers — no Slack I/O, no LLM calls. The CLI subcommand
(Task 2.4) wires these up against the live tenant, the markdown
composer (Task 2.3) renders, and `--post-to-slack` (Task 2.6) sends.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable


# Bullet shape (real production, see ingest._write_ask):
#   - [open · YYYY-MM-DD · Who[ · by YYYY-MM-DD]] text <!-- cp:hash=8hex -->
# Separator is U+00B7 (MIDDLE DOT). `who` may contain spaces, parens,
# commas — anything except `]` or the literal " · " separator. The non-
# greedy `.+?` on text stops at the trailing hash marker.
_OPEN_ASK_RE = re.compile(
    r"^- \[open · (?P<asked>\d{4}-\d{2}-\d{2}) · (?P<who>[^\]]+?)"
    r"(?: · by (?P<by>\d{4}-\d{2}-\d{2}))?\]\s+(?P<text>.+?)\s+<!--\s*cp:hash=(?P<hash>[0-9a-f]{8})\s*-->\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class PastDueAsk:
    """One past-due ask found by scanning a sprint file.

    Fields:
        code:       project/initiative code (e.g., "ggl-5168", "mission-control")
        text:       the ask body, no trailing hash marker
        who:        the assignee field (verbatim from the bullet)
        asked:      asked_date (when the bullet was first written)
        by:         due-date if present; None for "no-by stale" asks
        days_past:  how many days overdue. For by-date asks, today-by.
                    For no-by asks, today-asked.
        hash:       8-char content hash (round-trips with ClickUp)
    """
    code: str
    text: str
    who: str
    asked: date
    by: date | None
    days_past: int
    hash: str


def _find_past_due_asks(
    *,
    sprint_files: Iterable[Path],
    today: date,
    no_by_threshold_days: int = 7,
) -> list[PastDueAsk]:
    """Scan sprint files and return asks that are past due.

    Past-due definition:
      - bullet has `· by <date>` and `<date>` < today  (immediate overdue), OR
      - bullet has NO `by` and asked_date is >= no_by_threshold_days ago (stale).

    `[closed]` bullets are skipped entirely.
    """
    out: list[PastDueAsk] = []
    for path in sprint_files:
        code = path.stem
        body = path.read_text(encoding="utf-8")
        for m in _OPEN_ASK_RE.finditer(body):
            asked = date.fromisoformat(m.group("asked"))
            by_str = m.group("by")
            text = m.group("text").strip()
            who = m.group("who").strip()
            cp_hash = m.group("hash")
            if by_str:
                by = date.fromisoformat(by_str)
                if by < today:
                    out.append(PastDueAsk(
                        code=code, text=text, who=who,
                        asked=asked, by=by,
                        days_past=(today - by).days,
                        hash=cp_hash,
                    ))
            else:
                stale_days = (today - asked).days
                if stale_days >= no_by_threshold_days:
                    out.append(PastDueAsk(
                        code=code, text=text, who=who,
                        asked=asked, by=None,
                        days_past=stale_days,
                        hash=cp_hash,
                    ))
    return out


# Bullet shape (real production; see ingest._write_risk):
#   Engine: `- [<severity> · <category> · YYYY-MM-DD] text <!-- cp:hash=8hex -->`
#   Human:  `- [risk · <severity> · <category> · YYYY-MM-DD] text <!-- cp:hash=8hex -->`
# The optional `(?:risk · )?` prefix handles both. Category may contain
# spaces but not `]` or ` · ` (since that's the separator).
_RISK_RE = re.compile(
    r"^- \[(?:risk · )?(?P<sev>[a-z]+) · (?P<cat>[^\]]+?) · (?P<raised>\d{4}-\d{2}-\d{2})\]"
    r"\s+(?P<text>.+?)\s+<!--\s*cp:hash=(?P<hash>[0-9a-f]{8})\s*-->\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class EscalatedRisk:
    """One recently-escalated risk found by scanning a sprint file.

    Fields:
        code:     project/initiative code (sprint file's stem)
        text:     the risk body, no trailing hash marker
        category: the risk's category field (e.g., "scope", "capacity")
        raised:   the date the risk was raised
        hash:     8-char content hash
    """
    code: str
    text: str
    category: str
    raised: date
    hash: str


def _find_escalated_risks(
    *,
    sprint_files: Iterable[Path],
    today: date,
    window_days: int = 7,
) -> list[EscalatedRisk]:
    """Scan sprint files and return risks recently escalated.

    A risk qualifies if severity == "escalated" AND its raised date is
    >= (today - window_days). Boundary is INCLUSIVE (a risk raised
    exactly window_days ago counts).
    """
    out: list[EscalatedRisk] = []
    cutoff = today - timedelta(days=window_days)
    for path in sprint_files:
        code = path.stem
        body = path.read_text(encoding="utf-8")
        for m in _RISK_RE.finditer(body):
            if m.group("sev") != "escalated":
                continue
            raised = date.fromisoformat(m.group("raised"))
            if raised < cutoff:
                continue
            out.append(EscalatedRisk(
                code=code,
                text=m.group("text").strip(),
                category=m.group("cat").strip(),
                raised=raised,
                hash=m.group("hash"),
            ))
    return out
