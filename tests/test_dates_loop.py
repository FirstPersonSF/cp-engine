"""Tests for cp_engine.dates_loop — rendering buckets + ratification math."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from cp_engine.dates_loop import (
    ChannelPost,
    DatesLoopResult,
    _apply_ratification,
    _render_partners_rollup,
    _render_project_post,
)

_TODAY = date(2026, 7, 6)  # a Monday


def _c(
    id: str,
    desc: str,
    due: str | None,
    *,
    date_status: str = "proposed",
    posted_count: int = 0,
    direction: str = "us_to_them",
) -> dict:
    return {
        "id": id,
        "description": desc,
        "owner_email": "drew@firstperson.is",
        "owner_name": "Drew",
        "direction": direction,
        "due_date": due,
        "date_status": date_status,
        "status": "open",
        "posted_count": posted_count,
    }


def test_render_buckets() -> None:
    text, included = _render_project_post(
        code="ggl-5168",
        name="GGL 5168 Activation",
        commitments=[
            _c("a", "Slipped thing", "2026-07-01"),
            _c("b", "This week thing", "2026-07-08"),
            _c("c", "Upcoming thing", "2026-07-15"),
            _c("d", "Beyond window", "2026-09-01"),
            _c("e", "Undated thing", None),
        ],
        milestones=[(date(2026, 7, 9), "Deliver Messaging System")],
        today=_TODAY,
        window_days=14,
    )
    assert "Slipped" in text and "Slipped thing" in text
    assert "Due this week" in text and "This week thing" in text
    assert "Deliver Messaging System" in text  # milestone rides in-week
    assert "next 14 days" in text and "Upcoming thing" in text
    assert "Needs a date" in text and "Undated thing" in text
    # Beyond-window items don't appear and aren't in the ratification set.
    assert "Beyond window" not in text
    assert set(included) == {"a", "b", "c", "e"}
    # Agreed dates carry no status marker; proposed ones do.
    assert "[proposed]" in text


def test_render_empty_project_returns_nothing() -> None:
    text, included = _render_project_post(
        code="x", name="X",
        commitments=[_c("d", "Far future", "2027-01-01")],
        milestones=[], today=_TODAY, window_days=14,
    )
    assert text == "" and included == []


def test_partners_rollup_pileup_detection() -> None:
    events = [
        (date(2026, 7, 20), "ibx-5153", "Messaging system"),
        (date(2026, 7, 27), "sap-5171", "Final delivery"),
        (date(2026, 7, 27), "ibx-5192", "Delivery"),
        (date(2026, 7, 27), "snt-5193", "R1 feedback"),
        (date(2026, 7, 30), "sap-5174", "Workshop"),
    ]
    text = _render_partners_rollup(
        dated_events=events, slipped_total=1, undated_total=3,
        today=_TODAY, window_days=30,
    )
    assert text is not None
    assert "Pile-up: 4 dated events" in text or "Pile-up: 5 dated events" in text
    assert "1 slipped" in text
    assert "3 open commitments with no date" in text


def test_partners_rollup_none_when_quiet() -> None:
    assert (
        _render_partners_rollup(
            dated_events=[], slipped_total=0, undated_total=0,
            today=_TODAY, window_days=14,
        )
        is None
    )


def _fake_update_client() -> tuple[MagicMock, list[tuple[dict, str]]]:
    """Client whose .update(fields).eq('id', cid).execute() calls are captured."""
    updates: list[tuple[dict, str]] = []
    client = MagicMock()

    def _update(fields):
        chain = MagicMock()

        def _eq(_col, cid):
            updates.append((fields, cid))
            return MagicMock()

        chain.eq.side_effect = _eq
        return chain

    client.table.return_value.update.side_effect = _update
    return client, updates


def test_ratification_promotes_after_second_post() -> None:
    client, updates = _fake_update_client()
    commitments = [
        _c("a", "First post", "2026-07-10", posted_count=0),
        _c("b", "Second post", "2026-07-10", posted_count=1),
        _c("c", "Already agreed", "2026-07-10", date_status="agreed", posted_count=5),
    ]
    result = DatesLoopResult(
        posts=[
            ChannelPost(
                code="ggl-5168", name="", channel_ids=("C1",),
                text="…", commitment_ids=("a", "b", "c"), posted=True,
            )
        ]
    )
    _apply_ratification(client, result, commitments, today=_TODAY)
    by_id = {cid: fields for fields, cid in updates}
    assert by_id["a"]["posted_count"] == 1
    assert "date_status" not in by_id["a"]  # first post — still proposed
    assert by_id["b"]["posted_count"] == 2
    assert by_id["b"]["date_status"] == "agreed"  # second unchanged post
    assert "date_status" not in by_id["c"]  # already agreed — just bumped
    assert result.agreed_promoted == 1
    assert result.posted_count_bumped == 3


def test_ratification_skips_unposted_and_stamps_slipped() -> None:
    client, updates = _fake_update_client()
    commitments = [
        _c("a", "Post failed", "2026-07-10", posted_count=1),
        _c("s", "Past due", "2026-07-01"),
    ]
    result = DatesLoopResult(
        posts=[
            ChannelPost(
                code="x", name="", channel_ids=("C1",),
                text="…", commitment_ids=("a",), posted=False,  # send failed
            )
        ]
    )
    _apply_ratification(client, result, commitments, today=_TODAY)
    by_id = {cid: fields for fields, cid in updates}
    # Failed post: no counter bump, no promotion.
    assert "a" not in by_id
    # Past-due open row stamped slipped regardless of posting.
    assert by_id["s"]["date_status"] == "slipped"
    assert result.slipped_stamped == 1
    assert result.posted_count_bumped == 0


def test_partners_channel_from_app_config() -> None:
    from cp_engine.dates_loop import _partners_channel

    client = MagicMock()
    chain = client.table.return_value.select.return_value.eq.return_value
    # Bare-string value
    chain.execute.return_value.data = [
        {"key": "dates_loop_partners_channel", "value": "C0PARTNERS"}
    ]
    assert _partners_channel(client) == "C0PARTNERS"
    # Object value
    chain.execute.return_value.data = [
        {"key": "dates_loop_partners_channel", "value": {"channel": "C0X"}}
    ]
    assert _partners_channel(client) == "C0X"
    # Absent key → None (rollup skipped)
    chain.execute.return_value.data = []
    assert _partners_channel(client) is None
    # Lookup failure → None, never raises
    chain.execute.side_effect = RuntimeError("boom")
    assert _partners_channel(client) is None
