"""Quoted-history stripping for inbound email → net-new delta text.

The hard part of email ingest (Phase-1 finding #1): a forwarded/replied
thread re-quotes its entire history in every message, so the raw body is
mostly repetition. A 5-message thread measured 294KB raw for ~1.2KB of
net-new text. This module reduces a single message's plaintext body to the
sender's net-new contribution — the delta the distill pass should see.

Design notes:
  - Operates on ONE message's plaintext body (the Worker already picked
    `parsed.text` per message; postal-mime hands us the top message's text
    with the quoted history inline below it).
  - Tolerant, not perfect. Client mail wraps quotes many ways (Gmail
    ``On <date> … wrote:``, Outlook ``-----Original Message-----`` and
    ``From:``-header blocks, ``>``-prefixed lines). We cut at the FIRST
    reliable quote marker and keep everything above it.
  - Fail-OPEN: if stripping would leave nothing (marker at the very top, or
    an unrecognized layout), return the original body and let the distill
    dedup at the fact level. Losing a real fact is worse than re-showing
    quoted text. `stripped` in the result says which path ran.

This is deliberately server-side (not in the Cloudflare Worker) so strip
logic lives in ONE place with the distill it feeds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Quote-boundary markers, tried top-to-bottom on each line. The first line
# that matches any of these ends the net-new region; everything from that
# line down is treated as quoted history.
#
# Ordering doesn't matter (we take the earliest matching LINE, not the
# earliest pattern), but keep the most specific patterns anchored so a
# stray "On" or "From:" mid-sentence can't false-trigger.
_QUOTE_MARKERS: tuple[re.Pattern[str], ...] = (
    # Gmail / Apple Mail attribution line:
    #   "On Mon, Jul 20, 2026 at 3:38 AM Mehul Patel <m@x> wrote:"
    #   "On Jul 20, 2026, at 9:24 PM, Eric <e@x> wrote:"
    re.compile(r"^\s*On\b.{0,200}\bwrote:\s*$", re.IGNORECASE),
    # Outlook original-message separator.
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    # Outlook / forwarded header block: a "From:" line immediately starting
    # a quoted header stanza. Anchored to line start; must be followed by an
    # email-ish sender to avoid catching "From: the desk of" prose.
    re.compile(r"^\s*From:\s*.+<[^>]+@[^>]+>\s*$"),
    re.compile(r"^\s*From:\s*.+@.+\s*$"),
    # Gmail "forwarded message" fence.
    re.compile(r"^\s*-{3,}\s*Forwarded message\s*-{3,}\s*$", re.IGNORECASE),
)

# A run of ``>``-quoted lines also marks quoted history — but only when it's
# a genuine block (≥1 line starting with ">"), handled separately so a lone
# ">" inside prose doesn't cut the message.
_GT_QUOTE = re.compile(r"^\s*>")

# Banners some corporate MTAs inject above the real body — dropped from the
# net-new text (noise, never a fact).
_BANNER_LINES = (
    re.compile(r"This Message Is From an External Sender", re.IGNORECASE),
    re.compile(r"This message came from outside your organization", re.IGNORECASE),
    re.compile(r"CAUTION:.*external", re.IGNORECASE),
)


@dataclass
class StripResult:
    """Outcome of a strip pass.

    delta:     the net-new text (or the original body if we failed open).
    stripped:  True if a quote boundary was found and history was removed;
               False if we failed open (nothing removed).
    cut_at:    0-based line index where the quoted history began, or None.
    """

    delta: str
    stripped: bool
    cut_at: int | None


def _is_banner(line: str) -> bool:
    return any(p.search(line) for p in _BANNER_LINES)


def _first_quote_boundary(lines: list[str]) -> int | None:
    """Index of the first line that begins quoted history, or None.

    A boundary is either an attribution/header marker OR the start of a
    contiguous ``>``-quoted block. We ignore a boundary on the very first
    non-blank line (a message that opens with a quote has no net-new text
    to protect — better to fail open than return empty).
    """
    first_content_seen = False
    for i, line in enumerate(lines):
        if line.strip() and not _is_banner(line):
            # Marker-based boundary.
            if any(p.match(line) for p in _QUOTE_MARKERS):
                # Don't cut before any real content exists.
                return i if first_content_seen else None
            # ">"-quote block boundary: require the current line AND that we
            # already have content above it (a top-quoted message fails open).
            if _GT_QUOTE.match(line):
                return i if first_content_seen else None
            first_content_seen = True
    return None


def strip_quoted_history(body: str) -> StripResult:
    """Reduce a single message body to its net-new text.

    See module docstring for the fail-open contract.
    """
    if not body or not body.strip():
        return StripResult(delta=body or "", stripped=False, cut_at=None)

    lines = body.splitlines()
    boundary = _first_quote_boundary(lines)

    if boundary is None:
        # No quoted history found — drop banners, keep the rest.
        kept = [ln for ln in lines if not _is_banner(ln)]
        return StripResult(delta="\n".join(kept).strip(), stripped=False, cut_at=None)

    head = [ln for ln in lines[:boundary] if not _is_banner(ln)]
    delta = "\n".join(head).strip()

    if not delta:
        # Cutting left nothing real — fail open to the (banner-stripped)
        # whole body rather than emit empty.
        kept = [ln for ln in lines if not _is_banner(ln)]
        return StripResult(delta="\n".join(kept).strip(), stripped=False, cut_at=None)

    return StripResult(delta=delta, stripped=True, cut_at=boundary)
