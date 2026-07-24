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
  - Two shapes, handled in order:
      1. FORWARD (the common case — the whole design is "Drew forwards a
         thread to cp+<code>@"): the payload sits BELOW a
         ``---- Forwarded message ----`` fence, not above it. We unwrap to
         the forwarded body first (dropping the forwarder's note, the fence,
         and the From:/To:/… header stanza), then quote-strip that body.
      2. REPLY: net-new text on top, quoted history below. We cut at the
         FIRST reliable quote marker and keep everything above it.
  - Tolerant, not perfect. Client mail wraps quotes many ways (Gmail
    ``On <date> … wrote:``, Outlook ``-----Original Message-----``, a bare
    run-of-dashes divider, ``From:``-header blocks, ``>``-prefixed lines).
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
#
# NOTE: the forward fence is handled SEPARATELY (see _FORWARD_FENCE and
# _unwrap_forward) — for a forward the payload sits BELOW the fence, so it
# must not be treated as a plain "cut here, keep above" quote marker.
_QUOTE_MARKERS: tuple[re.Pattern[str], ...] = (
    # Gmail / Apple Mail attribution line:
    #   "On Mon, Jul 20, 2026 at 3:38 AM Mehul Patel <m@x> wrote:"
    #   "On Jul 20, 2026, at 9:24 PM, Eric <e@x> wrote:"
    re.compile(r"^\s*On\b.{0,200}\bwrote:\s*$", re.IGNORECASE),
    # Outlook original-message separator.
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    # Outlook's bare divider between a reply and the quoted message: a line of
    # only dashes (≥6 so a signature "--" or a 3-dash markdown rule can't
    # false-trigger). Outlook emits this above the From:/Sent:/To: stanza.
    re.compile(r"^\s*-{6,}\s*$"),
    # Outlook / forwarded header block: a "From:" line immediately starting
    # a quoted header stanza. Anchored to line start; must be followed by an
    # email-ish sender to avoid catching "From: the desk of" prose.
    re.compile(r"^\s*From:\s*.+<[^>]+@[^>]+>\s*$"),
    re.compile(r"^\s*From:\s*.+@.+\s*$"),
)

# The Gmail/Apple "forwarded message" fence. Unlike a quote marker, the
# content we WANT is below this line (the forwarded thread is the payload),
# so a forward is unwrapped first, then the forwarded body is quote-stripped
# normally. Forwarding is the PRIMARY shape here — the whole design is "Drew
# forwards a thread to cp+<code>@" — so this is the common case, not an edge.
_FORWARD_FENCE = re.compile(
    r"^\s*-{3,}\s*Forwarded message\s*-{3,}\s*$", re.IGNORECASE
)

# A forwarded-header stanza line: the From:/To:/Cc:/Date:/Sent:/Subject:
# block that sits directly under the fence. Dropped entirely (metadata, not
# prose) — distill reads the body, not the recipient list.
_FWD_HEADER_LINE = re.compile(
    r"^\s*(From|To|Cc|Bcc|Date|Sent|Subject|Reply-To):\s", re.IGNORECASE
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


def _unwrap_forward(lines: list[str]) -> list[str] | None:
    """If the body is a forwarded message, return the forwarded PAYLOAD lines.

    A forward looks like::

        [optional short note from the forwarder]
        ---------- Forwarded message ---------
        From: … / To: … / Date: … / Subject: …      (header stanza)
                                                     (blank line)
        <the forwarded body — the payload we want>

    We drop the forwarder's note, the fence, and the header stanza, returning
    the forwarded body. The caller then quote-strips THAT normally (so any
    ``On … wrote:`` / ``>`` quotes inside the forwarded thread still get cut).
    Returns None if there's no forward fence (not a forward).

    Only the FIRST fence is unwrapped: a chain of nested forwards collapses to
    the outermost forwarded body, whose own inner quotes the quote-strip pass
    then trims — the net-new of the innermost message survives at the top.
    """
    fence_idx = next(
        (i for i, ln in enumerate(lines) if _FORWARD_FENCE.match(ln)), None
    )
    if fence_idx is None:
        return None

    # Skip the fence, then the contiguous header stanza (From:/To:/… lines)
    # up to the first BLANK line, which ends the stanza. Within the stanza a
    # non-header line is a wrapped continuation of the previous header (a long
    # To:/Cc: recipient list often wraps, and Gmail does NOT indent the wrap),
    # so — once we've seen a header — any non-blank line stays part of the
    # stanza until the blank separator. This is why we key the stanza's end on
    # the blank line, not on "line is no longer a header".
    i = fence_idx + 1
    n = len(lines)
    saw_header = False
    while i < n:
        line = lines[i]
        if not line.strip():
            break  # blank line terminates the header stanza
        if _FWD_HEADER_LINE.match(line):
            saw_header = True
            i += 1
            continue
        if saw_header:
            # Non-header, non-blank line inside the stanza → wrapped
            # continuation of the recipient list. Consume it.
            i += 1
            continue
        break
    # Swallow the blank separator(s) between the header stanza and the body.
    while i < n and not lines[i].strip():
        i += 1
    return lines[i:]


def strip_quoted_history(body: str) -> StripResult:
    """Reduce a single message body to its net-new text.

    See module docstring for the fail-open contract. Forwarded messages are
    unwrapped to their payload FIRST (the forward is the common shape here —
    Drew forwards threads to ``cp+<code>@``), then quote-stripped normally.
    """
    if not body or not body.strip():
        return StripResult(delta=body or "", stripped=False, cut_at=None)

    lines = body.splitlines()

    # If this is a forward, replace the working lines with the forwarded
    # payload (fence + forwarder note + header stanza dropped), then fall
    # through to the ordinary quote-strip on that payload.
    unwrapped = _unwrap_forward(lines)
    is_forward = unwrapped is not None
    if is_forward:
        payload = [ln for ln in unwrapped if not _is_banner(ln)]
        if not "\n".join(payload).strip():
            # Forward with an empty payload (rare) — fail open to the whole
            # banner-stripped body rather than emit nothing.
            kept = [ln for ln in lines if not _is_banner(ln)]
            return StripResult(
                delta="\n".join(kept).strip(), stripped=False, cut_at=None
            )
        lines = payload

    boundary = _first_quote_boundary(lines)

    if boundary is None:
        # No quoted history found — drop banners, keep the rest. If we
        # unwrapped a forward, we DID strip (the wrapper), so report it as
        # such even though the forwarded payload had no inner quote to cut.
        kept = [ln for ln in lines if not _is_banner(ln)]
        return StripResult(
            delta="\n".join(kept).strip(), stripped=is_forward, cut_at=None
        )

    head = [ln for ln in lines[:boundary] if not _is_banner(ln)]
    delta = "\n".join(head).strip()

    if not delta:
        # Cutting left nothing real. For a forward whose payload opens with an
        # inner quote, keep the banner-stripped payload (still a real strip —
        # the wrapper came off). Otherwise fail open to the whole body.
        kept = [ln for ln in lines if not _is_banner(ln)]
        return StripResult(
            delta="\n".join(kept).strip(), stripped=is_forward, cut_at=None
        )

    return StripResult(delta=delta, stripped=True, cut_at=boundary)
