"""The snooze contract — what `cp:snoozed-until` means, and to whom.

A snooze is a Slack button (Snooze 7d / Snooze until…) on an ask or a risk in
the attention digest. It writes one marker onto the item's bullet in the sprint
file that OWNS it (``ingest._write_snooze`` → ``_origin_sprint_path``)::

    - [open · 2026-09-01 · Janet] Send the brand guide <!-- cp:snoozed-until=2026-09-15 --> <!-- cp:hash=1a2b3c4d -->

Until this module existed the marker had exactly one reader, the digest, so a
snoozed item vanished from Slack and stayed on every other surface — the cp.md
``current-sprint`` strip went on rendering it as though nothing had happened
(cp-engine #323). Nobody had written down what snooze was supposed to affect,
so every surface was individually "right".

**What snooze means.** "Don't chase this until <date>." It is a statement about
ESCALATION, not about state: a snoozed ask is still open and a snoozed risk is
still a risk. Nothing is resolved, closed or hidden from the record.

**For how long.** Active while ``today < until``; the item resurfaces ON the
``until`` date. The marker is never removed automatically — an expired marker
is inert, and a re-snooze replaces it rather than stacking. Because the marker
lives in the bullet's text, it travels with the item: a carried-forward copy in
the next week's ``carry-forward`` region carries it too, so every week that
shows the item agrees about its snooze.

**Which surfaces honour it, and how.** Three kinds of surface list snoozable
items, and they honour snooze differently on purpose:

1. **Escalation surfaces OMIT a snoozed item.** Their only job is "look at this
   now", which is exactly what the snooze declined. They are:

   - the attention digest (``attention_digest._find_past_due_asks`` /
     ``_find_escalated_risks``);
   - the stale-ask and escalated-risk rollup behind master-cp.md's ``agenda``
     and ``sprint-facts-strip`` counts, and a parent node's subtree
     ``carry-forward`` region (``aggregators._carry_forward_rollup``);
   - the per-project prep agenda's "Open asks aged > 7d" (``agenda``);
   - the /cp-prep bundle's urgent flags — past-due ask, stale dependency,
     escalated risk (``prep_planning._detect_urgent``).

2. **Summary strips MARK a snoozed item** ``(snoozed until <date>)`` and keep
   it, listed after the live items. They are an inventory of what is open, and
   an inventory that silently drops open items is the defect #323 is about —
   "Open client asks (0)" beside a list that isn't empty. So the count still
   includes the item, the header says how many of those are snoozed, and the
   item stops taking a top-of-list slot from something live. They are:

   - cp.md ``current-sprint`` (asks and active risks);
   - cp.md ``open-asks-strip`` (the stale flag is suppressed while snoozed);
   - the /cp-prep bundle's "Open commitments" table.

3. **The record keeps a snoozed item verbatim.** The sprint file's hand-written
   sections and its ``carry-forward`` region reproduce the bullet, marker and
   all; the sprint-index README counts it as open. Snooze never edits what was
   said — only whether a summary nags about it.

Every reader goes through :func:`snoozed_until` / :func:`is_snoozed` below, so
the window rule (strictly before ``until``) lives in one place.
"""

from __future__ import annotations

import re
from datetime import date

# The marker ``ingest._write_snooze`` writes. Tolerant of inner whitespace, like
# every other cp:* marker reader.
SNOOZE_MARKER_RE = re.compile(
    r"\s*<!--\s*cp:snoozed-until=(?P<until>\d{4}-\d{2}-\d{2})\s*-->\s*"
)


def snoozed_until(text: str | None) -> date | None:
    """The date a snooze marker in ``text`` names, or ``None``.

    Returns the date whether or not it has passed — callers that need "is it
    in force" use :func:`is_snoozed`. A malformed date reads as no snooze
    rather than raising: a hand-typo'd marker must not break a render.
    """
    if not text:
        return None
    m = SNOOZE_MARKER_RE.search(text)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group("until"))
    except ValueError:
        return None


def is_snoozed(text: str | None, today: date) -> bool:
    """True while the snooze on ``text`` is in force (``today < until``)."""
    until = snoozed_until(text)
    return until is not None and today < until


def active_snooze(text: str | None, today: date) -> date | None:
    """The ``until`` date when the snooze is in force, else ``None``."""
    until = snoozed_until(text)
    return until if until is not None and today < until else None


def strip_snooze_marker(text: str) -> str:
    """``text`` with any snooze marker removed (whitespace collapsed to one)."""
    return SNOOZE_MARKER_RE.sub(" ", text).strip()


def snooze_label(until: date) -> str:
    """The visible mark a summary strip appends to a snoozed item."""
    return f"(snoozed until {until.isoformat()})"
