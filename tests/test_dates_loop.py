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


# ──────────────────────────────────────────────────────────────────────
#  #85: unmasked Slack error codes + partial-success exit semantics
# ──────────────────────────────────────────────────────────────────────


def test_post_channel_surfaces_slack_error_code() -> None:
    """SlackApiError's str() is generic; the actionable code lives in
    exc.response and must reach the SlackError message (the first live
    dates-loop failure was undiagnosable without it)."""
    import pytest

    from cp_engine import slack as slack_mod

    class FakeApiError(Exception):
        def __init__(self):
            super().__init__("The request to the Slack API failed.")
            self.response = {"ok": False, "error": "channel_not_found"}

    class FakeClient:
        def chat_postMessage(self, **kwargs):
            raise FakeApiError()

    with pytest.raises(slack_mod.SlackError, match=r"\[slack error: channel_not_found\]"):
        slack_mod.post_channel(FakeClient(), channel_id="C123", text="hi")


def _cli_result(*, project_posted: bool, partners_posted: bool, errors: list[str]):
    from cp_engine.dates_loop import ChannelPost, DatesLoopResult

    r = DatesLoopResult()
    post = ChannelPost(
        code="ggl-5136", name="go/safety", channel_ids=("C0AA",),
        text=":date: post", commitment_ids=(),
    )
    post.posted = project_posted
    r.posts = [post]
    r.partners_channel = "C09RY4D451U"
    r.partners_text = "rollup"
    r.partners_posted = partners_posted
    r.errors = errors
    return r


def _run_cli(monkeypatch, result):
    from click.testing import CliRunner

    from cp_engine.cli import main  # full CLI import avoids the partial-init cycle
    import cp_engine.cli_cmds.planning as planning

    monkeypatch.setattr(planning, "load", lambda p: object())
    monkeypatch.setattr(
        "cp_engine.dates_loop.run_dates_loop",
        lambda config, **kw: result,
    )
    return CliRunner().invoke(main, ["dates-loop", "--post"])


def test_cli_partial_success_exits_zero(monkeypatch) -> None:
    """Delivered per-project posts + failed partners rollup = exit 0 with
    loud errors — a rerun would double-post the delivered messages."""
    result = _cli_result(
        project_posted=True, partners_posted=False,
        errors=["partners/C09RY4D451U: chat_postMessage failed [slack error: channel_not_found]"],
    )
    out = _run_cli(monkeypatch, result)
    assert out.exit_code == 0
    assert "partial success: 1 post(s) delivered, 1 failed" in out.output
    assert "channel_not_found" in out.output


def test_cli_nothing_delivered_exits_one(monkeypatch) -> None:
    result = _cli_result(
        project_posted=False, partners_posted=False,
        errors=["ggl-5136/C0AA: boom", "partners/C09RY4D451U: boom"],
    )
    out = _run_cli(monkeypatch, result)
    assert out.exit_code == 1


# ── TTL expiry for undated meeting-ingest rows (#136) ──────────────────


def _ingest_c(id: str, desc: str, created: str, **kw) -> dict:
    c = _c(id, desc, None, **kw)
    c["source_kind"] = "meeting_ingest"
    c["created_at"] = created + "T12:00:00+00:00"
    return c


def test_ttl_bucket_eligibility() -> None:
    from cp_engine.dates_loop import _ttl_bucket

    # 14+ days undated meeting-ingest proposed → expire.
    assert _ttl_bucket(_ingest_c("a", "old", "2026-06-20"), _TODAY) == "expire"
    # 7–13 days → warn.
    assert _ttl_bucket(_ingest_c("b", "warm", "2026-06-28"), _TODAY) == "warn"
    # Fresh → None.
    assert _ttl_bucket(_ingest_c("c", "new", "2026-07-05"), _TODAY) is None
    # A due date cancels the TTL.
    dated = _ingest_c("d", "dated", "2026-06-01")
    dated["due_date"] = "2026-08-01"
    assert _ttl_bucket(dated, _TODAY) is None
    # A ratified/changed date_status cancels it.
    agreed = _ingest_c("e", "agreed", "2026-06-01", date_status="agreed")
    assert _ttl_bucket(agreed, _TODAY) is None
    # Human-authored rows are exempt regardless of age.
    session = _ingest_c("f", "session row", "2026-06-01")
    session["source_kind"] = "session"
    assert _ttl_bucket(session, _TODAY) is None
    # Unparseable created_at → never expire.
    broken = _ingest_c("g", "broken", "2026-06-01")
    broken["created_at"] = "not-a-date"
    assert _ttl_bucket(broken, _TODAY) is None


def test_render_marks_warn_and_excludes_expiring() -> None:
    warn = _ingest_c("w", "Warn thing", "2026-06-28")
    gone = _ingest_c("x", "Expiring thing", "2026-06-20")
    fresh = _ingest_c("f", "Fresh thing", "2026-07-05")
    text, included = _render_project_post(
        code="sap-5174", name="SAP 5174",
        commitments=[warn, gone, fresh],
        milestones=[], today=_TODAY, window_days=14,
        ttl_buckets={"w": "warn", "x": "expire"},
    )
    assert "Warn thing" in text
    assert "expires next Monday unless dated" in text
    assert "Expiring thing" not in text  # terminal this run, not "needs a date"
    assert "1 undated meeting-ingest commitment auto-expired" in text
    # Expiring row is out of the ratification set; the others stay.
    assert set(included) == {"w", "f"}


def test_partners_rollup_expiry_lines() -> None:
    text = _render_partners_rollup(
        dated_events=[], slipped_total=0, undated_total=2,
        today=_TODAY, window_days=14,
        expire_warn=[("sap-5174", "Warn thing"), ("ibx-5153", "Other thing")],
        expired_total=3,
    )
    assert text is not None
    assert "2 undated commitments expire next Monday unless dated" in text
    assert "`sap-5174` Warn thing" in text
    assert "3 undated meeting-ingest commitments auto-expired this week" in text


def test_apply_expiry_writes_only_expire_bucket() -> None:
    from cp_engine.dates_loop import _apply_expiry

    client, updates = _fake_update_client()
    rows = [
        _ingest_c("x", "Expiring", "2026-06-20"),
        _ingest_c("w", "Warned", "2026-06-28"),
        _c("d", "Dated", "2026-07-10"),
    ]
    result = DatesLoopResult()
    _apply_expiry(client, result, rows, {"x": "expire", "w": "warn"})
    by_id = {cid: fields for fields, cid in updates}
    assert by_id["x"]["status"] == "expired"
    assert "w" not in by_id and "d" not in by_id
    assert result.expired_stamped == 1
