"""Tests for webhook/email_strip.py — quoted-history delta extraction.

The headline case runs against a SYNTHETIC thread message
(tests/fixtures/reply_with_quoted_history.txt) shaped exactly like the real
ibx-5192 "Deck r01" reply that proved Phase-1 distill quality — 3 net-new
deck notes sitting above a From:-header quote block, with an "External
Sender" banner in the quoted region. The content is fabricated (this repo is
public; no real client mail lives in fixtures), but the STRUCTURE is
identical so the strip behavior under test is the same.
"""
from __future__ import annotations

import sys
from pathlib import Path

# webhook/ is a sibling of src/; not on the import path by default.
_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import email_strip  # noqa: E402

_FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "reply_with_quoted_history.txt"
)


def test_reply_strips_to_net_new():
    """Thread-shaped fixture: keep the 3 notes, drop the quoted message."""
    body = _FIXTURE.read_text(encoding="utf-8")
    result = email_strip.strip_quoted_history(body)

    assert result.stripped is True
    # Net-new content is present.
    assert "push down the overview slide" in result.delta
    assert "capabilities > outcomes" in result.delta
    assert "roadmap then you have to include the other 2" in result.delta
    # Quoted history is gone.
    assert "Please find the first round of our blocking deck" not in result.delta
    assert "example.test/share" not in result.delta
    # Banner is gone.
    assert "External Sender" not in result.delta
    # Massive reduction: net-new is a small fraction of the raw body.
    assert len(result.delta) < len(body) / 2


def test_gmail_on_wrote_marker():
    body = (
        "Sounds good, let's ship it.\n"
        "\n"
        "On Mon, Jul 20, 2026 at 3:38 AM Mehul Patel <m@x.com> wrote:\n"
        "> Can you do today at noon?\n"
        "> - Mehul\n"
    )
    result = email_strip.strip_quoted_history(body)
    assert result.stripped is True
    assert result.delta == "Sounds good, let's ship it."


def test_gt_quote_block_marker():
    body = "My actual reply here.\n\n> old line one\n> old line two\n"
    result = email_strip.strip_quoted_history(body)
    assert result.stripped is True
    assert result.delta == "My actual reply here."


def test_outlook_original_message_marker():
    body = (
        "Here is the update.\n"
        "-----Original Message-----\n"
        "From: someone\n"
        "Subject: old\n"
    )
    result = email_strip.strip_quoted_history(body)
    assert result.stripped is True
    assert result.delta == "Here is the update."


def test_no_quote_fails_open_but_drops_banner():
    body = (
        "This Message Is From an External Sender\n"
        "A short standalone note with no quoted history.\n"
    )
    result = email_strip.strip_quoted_history(body)
    assert result.stripped is False  # nothing quoted was cut
    assert "External Sender" not in result.delta
    assert "standalone note" in result.delta


def test_top_quoted_message_fails_open():
    """A message that opens with a quote has no net-new — return original."""
    body = "On Jul 1, 2026 at 9:00 AM X <x@y.com> wrote:\n> hi\n> there\n"
    result = email_strip.strip_quoted_history(body)
    assert result.stripped is False
    # Fail-open returns the (banner-free) body rather than empty.
    assert result.delta != ""


def test_empty_body():
    result = email_strip.strip_quoted_history("")
    assert result.delta == ""
    assert result.stripped is False
