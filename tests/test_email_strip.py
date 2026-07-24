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


def test_forward_unwraps_to_payload():
    """A forwarded message: drop the fence + header stanza, keep the payload.

    This is the shape that fail-opened before the forward-unwrap fix — the
    ``---- Forwarded message ----`` fence sat on the first content line, so
    the old "keep everything above the first marker" logic returned nothing
    and fell open to the full body (HTML twin and all).
    """
    body = (
        "---------- Forwarded message ---------\n"
        "From: Jaime Mehra <jmehra@example.test>\n"
        "Date: Thu, Jul 23, 2026 at 5:27 PM\n"
        "Subject: Re: AI campaign\n"
        "To: Janet Noe <jnoe@example.test>, Drew <drew@example.test>\n"
        "\n"
        "We are drawing a line in the sand now to get this to sales at SRS.\n"
        "We already have line of sight to a v2 with Kentik value prop.\n"
    )
    result = email_strip.strip_quoted_history(body)
    assert result.stripped is True
    # Payload survives.
    assert "drawing a line in the sand" in result.delta
    assert "Kentik value prop" in result.delta
    # Fence + header stanza are gone.
    assert "Forwarded message" not in result.delta
    assert "jmehra@example.test" not in result.delta
    assert "Subject:" not in result.delta


def test_forward_with_note_drops_note_and_headers():
    """A forwarder's one-line note above the fence is dropped with the wrapper."""
    body = (
        "fyi — see Jaime's note below\n"
        "\n"
        "---------- Forwarded message ---------\n"
        "From: Jaime Mehra <jmehra@example.test>\n"
        "Subject: Re: AI campaign\n"
        "\n"
        "The pillars need external-ready language, not internal framing.\n"
    )
    result = email_strip.strip_quoted_history(body)
    assert result.stripped is True
    assert "pillars need external-ready language" in result.delta
    assert "fyi" not in result.delta
    assert "Forwarded message" not in result.delta


def test_forward_then_inner_quote_keeps_only_forwarded_net_new():
    """Forward whose payload itself quotes an earlier message: cut the inner quote."""
    body = (
        "---------- Forwarded message ---------\n"
        "From: Jaime Mehra <jmehra@example.test>\n"
        "Subject: Re: deck\n"
        "\n"
        "This is important feedback, thanks.\n"
        "\n"
        "On Mon, Jul 20, 2026 at 10:22 AM Janet Noe <jnoe@example.test> wrote:\n"
        "> Here is the older message we should not keep.\n"
    )
    result = email_strip.strip_quoted_history(body)
    assert result.stripped is True
    assert "This is important feedback" in result.delta
    assert "older message we should not keep" not in result.delta


def test_forward_drops_external_banner_in_payload():
    """An External-Sender banner inside the forwarded payload is dropped."""
    body = (
        "---------- Forwarded message ---------\n"
        "From: X <x@example.test>\n"
        "\n"
        "This Message Is From an External Sender\n"
        "The real forwarded content is this sentence.\n"
    )
    result = email_strip.strip_quoted_history(body)
    assert result.stripped is True
    assert "External Sender" not in result.delta
    assert "real forwarded content" in result.delta


def test_forward_real_shape_wrapped_recipients_and_bare_divider():
    """The exact shape of the first real ingested forward (ibx-5192).

    Two things this exercises that the simpler forward tests don't:
      - the To:/Cc: recipient list WRAPS onto an unindented next line inside
        the header stanza (must not leak into the payload);
      - the inner Outlook reply uses a BARE run-of-dashes divider (no
        "Original Message" text) above its From:/Sent: stanza.
    Only Jaime's net-new note should survive.
    """
    body = (
        "---------- Forwarded message ---------\n"
        "From: Jaime Mehra <jmehra@example.test>\n"
        "Subject: Re: AI campaign\n"
        "To: Janet Noe <jnoe@example.test>, Carolina Janovik <cjanovik@example.test>,\n"
        "drew <drew@example.test>\n"
        "Cc: Jarrod Kelsey <jkelsey@example.test>\n"
        "\n"
        "We are drawing a line in the sand now to get this to sales at SRS.\n"
        "\n"
        "------------------------------\n"
        "From: Drew Fiero <drew@example.test>\n"
        "Sent: Wednesday, 22 July 2026 23:46:38\n"
        "Subject: Re: AI campaign\n"
        "\n"
        "Thanks Janet. This is important feedback.\n"
        "On Mon, Jul 20, 2026 at 10:22 AM Janet Noe <jnoe@example.test> wrote:\n"
        "> Hi everyone, the pillars need work.\n"
    )
    result = email_strip.strip_quoted_history(body)
    assert result.stripped is True
    assert result.delta == "We are drawing a line in the sand now to get this to sales at SRS."
    # None of the wrapper / recipient-list / inner-reply noise survives.
    assert "cjanovik@example.test" not in result.delta
    assert "drew <drew@example.test>" not in result.delta
    assert "Sent:" not in result.delta
    assert "pillars need work" not in result.delta


def test_bare_dashes_divider_not_triggered_by_signature():
    """A signature '--' or a 3-dash rule must NOT be treated as a divider."""
    body = "Here is my real reply.\n--\nDrew\n"
    result = email_strip.strip_quoted_history(body)
    # Two-dash signature marker is below the 6-dash threshold → not a boundary.
    assert "Here is my real reply." in result.delta
    assert result.stripped is False


def test_empty_body():
    result = email_strip.strip_quoted_history("")
    assert result.delta == ""
    assert result.stripped is False
