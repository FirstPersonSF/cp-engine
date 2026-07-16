"""Digest rendering for cross-project routing proposals (#88): markdown
fallback section, Block Kit section with Route/Dismiss buttons, and the
all-clear override when proposals are pending."""
from __future__ import annotations

from datetime import date

from cp_engine.attention_digest import _render_digest_blocks, compose_digest


def _proposal(**over) -> dict:
    base = {
        "id": "prop-1",
        "meeting_id": "0123456789abcdef",
        "source_code": "ibx-5192",
        "target_code": "sap-5174",
        "verb": "decisions",
        "text": "Customer sessions on the 28th",
        "confidence": "high",
        "status": "pending",
        "cp_hash": "deadbeef",
        "created_at": "2026-07-16T00:00:00Z",
    }
    base.update(over)
    return base


def _digest(**over) -> dict:
    base = {
        "past_due": [], "escalated": [], "allocation": [],
        "week_iso": "2026-W29", "cross_project": [_proposal()],
    }
    base.update(over)
    return base


def test_compose_digest_renders_cross_project_section() -> None:
    md = compose_digest(_digest(), recipient_name="Drew", today=date(2026, 7, 16))
    assert "1 cross-project routing proposal" in md
    assert "`ibx-5192` → `sap-5174` — Customer sessions on the 28th" in md
    assert "(decisions · high)" in md


def test_pending_proposals_defeat_all_clear() -> None:
    md = compose_digest(_digest(), recipient_name="Drew", today=date(2026, 7, 16))
    assert "all clear" not in md
    blocks = _render_digest_blocks(_digest(), recipient_name="Drew")
    assert blocks[0]["type"] == "header"


def test_blocks_carry_route_and_dismiss_buttons() -> None:
    blocks = _render_digest_blocks(_digest(), recipient_name="Drew")
    actions = [b for b in blocks if b["type"] == "actions"]
    assert len(actions) == 1
    buttons = actions[0]["elements"]
    accept, dismiss = buttons[0], buttons[1]
    assert accept["action_id"] == "xproj-accept_sap-5174_deadbeef"
    assert accept["value"] == "xproj-accept|sap-5174|deadbeef|2026-W29"
    assert dismiss["action_id"] == "xproj-dismiss_sap-5174_deadbeef"
    assert dismiss["value"] == "xproj-dismiss|sap-5174|deadbeef|2026-W29"
    # Context line carries verb · confidence · meeting ref.
    contexts = [b for b in blocks if b["type"] == "context"]
    assert any(
        "decisions · high · meeting 01234567" in c["elements"][0]["text"]
        for c in contexts
    )


def test_empty_digest_without_proposals_still_all_clear() -> None:
    md = compose_digest(
        _digest(cross_project=[]), recipient_name="Drew", today=date(2026, 7, 16)
    )
    assert "all clear" in md
