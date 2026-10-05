"""The digest "Mark closed" button looked like it did nothing (2026-10-05).

Every first click DID close the commitment in MC-2. What broke was the
Slack message rewrite: it was rebuilt from the message AS IT WAS WHEN THAT
BUTTON WAS CLICKED, so click B's update — carrying a copy in which item A
still had its buttons — landed after A's "✅ Closed" and put A's buttons
back. And the label came from "did a file change", so a re-click on a done
row (or a close that changed MC-2 but no file) read "No matching item".

These tests drive the real handler and background coroutine; Slack's
response_url is a recorded fake, MC-2 is the in-memory PostgREST fake.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import git_ops  # noqa: E402
import pipeline  # noqa: E402
from routers import slack as slack_router  # noqa: E402

from cp_engine.attention_digest import PastDueAsk, _render_digest_blocks  # noqa: E402
from tests.test_asks_mc2 import CODE, TODAY, FakeMC2, _render, _row  # noqa: E402

WEEK = "2026-W40"
HASH_A, HASH_B, HASH_C = "aaaa1111", "bbbb2222", "cccc3333"
CHANNEL, TS = "D0B7Q4UQC7J", "1759650000.000100"


@pytest.fixture(autouse=True)
def _fresh_message_state():
    """The per-message overlay is process-local; isolate tests from it.
    (Absent on engines before the fix — nothing to reset there.)"""
    try:
        import digest_message
    except ImportError:
        yield
        return
    digest_message.reset()
    yield
    digest_message.reset()


def _restart() -> None:
    """What a process restart does to the per-message overlay."""
    try:
        import digest_message
    except ImportError:
        return
    digest_message.reset()


class _Posts:
    """Records every response_url POST, in order."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, url, **kw):
        self.calls.append({"url": url, **(kw.get("json") or {})})

        class _R:
            status_code, text, ok = 200, "ok", True
        return _R()

    @property
    def last_blocks(self) -> list[dict]:
        return self.calls[-1]["blocks"]


@pytest.fixture
def posts(monkeypatch) -> _Posts:
    p = _Posts()
    monkeypatch.setattr("requests.post", p)
    return p


@pytest.fixture
def spawned(monkeypatch) -> list:
    """Hold background coroutines so the test decides the order they run."""
    held: list = []
    monkeypatch.setattr(pipeline, "_spawn_background", held.append)
    monkeypatch.setattr(slack_router.pipeline, "_spawn_background", held.append)
    return held


def _digest(*hashes: str) -> list[dict]:
    asks = [
        PastDueAsk(code=CODE, text=f"Ask {h}", who="Rena", asked=date(2026, 9, 1),
                   by=date(2026, 9, 10), days_past=20 - i, hash=h)
        for i, h in enumerate(hashes)
    ]
    return _render_digest_blocks(
        {"past_due": asks, "week_iso": WEEK}, recipient_name="Drew"
    )


def _payload(blocks: list[dict], h: str, verb: str = "close-ask") -> dict:
    return {
        "type": "block_actions",
        "user": {"id": "U02CF0L7M"},
        "container": {"type": "message", "message_ts": TS, "channel_id": CHANNEL},
        "channel": {"id": CHANNEL},
        "actions": [{
            "type": "button",
            "action_id": f"{verb}_{CODE}_{h}",
            "value": f"{verb}|{CODE}|{h}|{WEEK}",
        }],
        "response_url": f"https://hooks.slack.com/actions/T/{h}/{verb}",
        "message": {"ts": TS, "blocks": [dict(b) for b in blocks]},
        "trigger_id": "trg",
    }


def _item_view(blocks: list[dict], h: str) -> str:
    """How item `h` reads in a message: 'buttons', or the text of the
    context line that replaced its buttons."""
    for i, b in enumerate(blocks):
        if b.get("type") == "actions" and any(
            h in (el.get("value") or "") for el in b.get("elements") or []
        ):
            return "buttons"
        if b.get("type") == "section" and f"Ask {h}" in (b.get("text") or {}).get("text", ""):
            # [section, context(days past), <actions|confirmation>]
            nxt = blocks[i + 2]
            if nxt.get("type") == "actions":
                return "buttons"
            return nxt["elements"][0]["text"]
    raise AssertionError(f"item {h} not in message")


def _resolved(*_a, **_kw) -> dict:
    return {"committed": True, "commit_sha": "c0ffee12345678", "errors": [],
            "commitments_resolved": ["row-id"]}


class _RecordingMC2(FakeMC2):
    """FakeMC2 that records the column list of every select."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.selects: list[str] = []

    def table(self, name):
        q = super().table(name)
        real = q.select

        def select(*cols, **kw):
            self.selects.append(",".join(cols))
            return real(*cols, **kw)
        q.select = select
        return q


# ── 1. two overlapping clicks ───────────────────────────────────────────


def test_overlapping_clicks_do_not_resurrect_the_earlier_item(monkeypatch, posts, spawned):
    """Click A, then click B before A's update lands (B's payload carries the
    click-time message in which A still has its buttons). B's final update
    is posted LAST. The message must end with A Closed and B Closed.

    Before the fix B's update replaced the message with B's stale copy: A's
    buttons came back and A "didn't close"."""
    monkeypatch.setattr(slack_router, "_run_plan_for_one_item", _resolved)
    mc2 = _RecordingMC2([_row("Ask a", cp_hash=HASH_A, status="done"),
                         _row("Ask b", cp_hash=HASH_B, status="done"),
                         _row("Ask c", cp_hash=HASH_C)])
    monkeypatch.setattr(slack_router.pipeline, "_create_supabase_client", lambda: mc2)
    s0 = _digest(HASH_A, HASH_B, HASH_C)

    asyncio.run(slack_router._handle_block_action(_payload(s0, HASH_A)))
    asyncio.run(slack_router._handle_block_action(_payload(s0, HASH_B)))  # stale copy
    task_a, task_b = spawned
    asyncio.run(task_a)
    asyncio.run(task_b)  # posted last

    final = posts.last_blocks
    assert _item_view(final, HASH_A).startswith("✅ Closed"), _item_view(final, HASH_A)
    assert _item_view(final, HASH_B).startswith("✅ Closed"), _item_view(final, HASH_B)
    assert _item_view(final, HASH_C) == "buttons"  # untouched, still open
    assert all("*" not in s for s in mc2.selects), mc2.selects


def test_mc2_keeps_an_item_closed_when_this_process_never_saw_it(
    monkeypatch, posts, spawned
):
    """Restart (or another replica) between the two clicks: nothing in
    memory knows A was closed. B's stale copy shows A with buttons; MC-2 says
    A's commitment is done → A renders closed, from ONE batched read."""
    monkeypatch.setattr(slack_router, "_run_plan_for_one_item", _resolved)
    mc2 = _RecordingMC2([_row("Ask a", cp_hash=HASH_A, status="done"),
                         _row("Ask b", cp_hash=HASH_B, status="done")])
    monkeypatch.setattr(slack_router.pipeline, "_create_supabase_client", lambda: mc2)
    s0 = _digest(HASH_A, HASH_B)

    asyncio.run(slack_router._handle_block_action(_payload(s0, HASH_B)))
    _restart()  # the overlay is gone
    mc2.selects.clear()
    asyncio.run(spawned[0])

    final = posts.last_blocks
    assert _item_view(final, HASH_A) == "✅ Closed"
    assert _item_view(final, HASH_B).startswith("✅ Closed")
    commitment_reads = [s for s in mc2.selects if "status" in s]
    assert commitment_reads == ["cp_hash, status"], mc2.selects


def test_an_item_still_in_flight_stays_closing_not_buttons(monkeypatch, posts, spawned):
    """B finishes before A does: A is still open in MC-2 but being worked on
    here — it reads "Closing…", never its buttons. A's own final then lands."""
    monkeypatch.setattr(slack_router, "_run_plan_for_one_item", _resolved)
    mc2 = _RecordingMC2([_row("Ask a", cp_hash=HASH_A),
                         _row("Ask b", cp_hash=HASH_B, status="done")])
    monkeypatch.setattr(slack_router.pipeline, "_create_supabase_client", lambda: mc2)
    s0 = _digest(HASH_A, HASH_B)

    asyncio.run(slack_router._handle_block_action(_payload(s0, HASH_A)))
    asyncio.run(slack_router._handle_block_action(_payload(s0, HASH_B)))
    task_a, task_b = spawned
    asyncio.run(task_b)
    assert _item_view(posts.last_blocks, HASH_A) == "⏳ Closing…"
    asyncio.run(task_a)
    assert _item_view(posts.last_blocks, HASH_A).startswith("✅ Closed")
    assert _item_view(posts.last_blocks, HASH_B).startswith("✅ Closed")


def test_a_stale_closing_row_gets_its_buttons_back(monkeypatch, posts, spawned):
    """A "Closing…" row nobody is working on (lost to a restart) and still
    open in MC-2 is offered again, not left spinning forever."""
    monkeypatch.setattr(slack_router, "_run_plan_for_one_item", _resolved)
    mc2 = _RecordingMC2([_row("Ask a", cp_hash=HASH_A),
                         _row("Ask b", cp_hash=HASH_B, status="done")])
    monkeypatch.setattr(slack_router.pipeline, "_create_supabase_client", lambda: mc2)
    s0 = _digest(HASH_A, HASH_B)

    asyncio.run(slack_router._handle_block_action(_payload(s0, HASH_A)))
    s1 = posts.last_blocks  # A shows "Closing…"
    for coro in spawned:     # A's task died with the process
        coro.close()
    spawned.clear()
    _restart()
    asyncio.run(slack_router._handle_block_action(_payload(s1, HASH_B)))
    asyncio.run(spawned[-1])
    assert _item_view(posts.last_blocks, HASH_A) == "buttons"


# ── 2/3. labels from MC-2, not from files ──────────────────────────────


@contextmanager
def _tenant(tmp_path):
    yield tmp_path


def _run_real_close(monkeypatch, tmp_path, posts, mc2) -> str:
    """Real `_run_plan_for_one_item` + `_run_action_in_background` against a
    sprint file that does NOT carry the ask (it left the open-asks region on
    the first click). Returns how the clicked item reads afterwards."""
    _render(tmp_path, WEEK, None)
    monkeypatch.setattr(git_ops, "_cloned_tenant", lambda: _tenant(tmp_path))
    monkeypatch.setattr(git_ops, "_commit_and_push", lambda **kw: "deadbeef00")
    monkeypatch.setattr(slack_router.pipeline, "_create_supabase_client", lambda: mc2)
    monkeypatch.setattr(slack_router, "tenant_today", lambda: TODAY)
    s0 = _digest(HASH_A)
    asyncio.run(slack_router._run_action_in_background(
        verb="close-ask", code=CODE, cp_hash=HASH_A,
        extras={"closed_by": "slack", "user": "U1"},
        response_url="https://hooks.slack.com/actions/T/a/close",
        original_message={"ts": TS, "blocks": s0},
        clicked_action_id=f"close-ask_{CODE}_{HASH_A}",
        week_iso=WEEK,
    ))
    return _item_view(posts.last_blocks, HASH_A)


def test_reclick_on_an_already_done_row_says_already_closed(monkeypatch, tmp_path, posts):
    mc2 = _RecordingMC2([_row("Ask a", cp_hash=HASH_A, status="done")])
    view = _run_real_close(monkeypatch, tmp_path, posts, mc2)
    assert view.startswith("✅ Already closed"), view
    assert "No matching item" not in view


def test_mc2_resolved_with_no_file_change_says_closed(monkeypatch, tmp_path, posts):
    mc2 = _RecordingMC2([_row("Ask a", cp_hash=HASH_A)])
    view = _run_real_close(monkeypatch, tmp_path, posts, mc2)
    assert mc2.commitments[0]["status"] == "done"  # the write side, unchanged
    assert view.startswith("✅ Closed"), view


def test_no_row_for_the_hash_still_says_no_matching_item(monkeypatch, tmp_path, posts):
    mc2 = _RecordingMC2([])
    view = _run_real_close(monkeypatch, tmp_path, posts, mc2)
    assert "No matching item" in view


# ── 4. immediate feedback inside the ack ───────────────────────────────


def test_closing_update_is_posted_within_the_ack_path(posts, spawned):
    """The click's own request posts "⏳ Closing…" for the clicked row (and
    only it) BEFORE returning — the background task has not run at all."""
    s0 = _digest(HASH_A, HASH_B)
    resp = asyncio.run(slack_router._handle_block_action(_payload(s0, HASH_A)))
    assert resp.get("queued") is True
    assert len(spawned) == 1  # queued, not run
    assert len(posts.calls) == 1, posts.calls
    call = posts.calls[0]
    assert call["replace_original"] is True
    assert call["url"].endswith(f"/{HASH_A}/close-ask")
    assert _item_view(call["blocks"], HASH_A) == "⏳ Closing…"
    assert _item_view(call["blocks"], HASH_B) == "buttons"
    for coro in spawned:
        coro.close()


def test_response_url_uses_per_click_stay_within_slacks_limit(monkeypatch, posts, spawned):
    """Slack allows 5 uses of a response_url in 30 min. One click = the
    pending post + the final post = 2 uses of THAT click's url."""
    monkeypatch.setattr(slack_router, "_run_plan_for_one_item", _resolved)
    monkeypatch.setattr(slack_router.pipeline, "_create_supabase_client", lambda: None)
    s0 = _digest(HASH_A, HASH_B)
    asyncio.run(slack_router._handle_block_action(_payload(s0, HASH_A)))
    asyncio.run(slack_router._handle_block_action(_payload(s0, HASH_B)))
    for t in list(spawned):
        asyncio.run(t)
    by_url: dict[str, int] = {}
    for c in posts.calls:
        by_url[c["url"]] = by_url.get(c["url"], 0) + 1
    assert sorted(by_url.values()) == [2, 2], by_url


# ── D. the other buttons in the same digest ────────────────────────────


def test_a_snooze_after_a_close_does_not_reopen_the_close(monkeypatch, posts, spawned):
    """Snooze B (7d) from a copy where A still has buttons: A stays closed."""
    monkeypatch.setattr(slack_router, "_run_plan_for_one_item", _resolved)
    monkeypatch.setattr(slack_router.pipeline, "_create_supabase_client", lambda: None)
    s0 = _digest(HASH_A, HASH_B)
    asyncio.run(slack_router._handle_block_action(_payload(s0, HASH_A)))
    asyncio.run(slack_router._handle_block_action(_payload(s0, HASH_B, "snooze-ask-7d")))
    task_a, task_b = spawned
    asyncio.run(task_a)
    asyncio.run(task_b)
    assert _item_view(posts.last_blocks, HASH_A).startswith("✅ Closed")
    assert _item_view(posts.last_blocks, HASH_B).startswith("💤 Snoozed until")


def test_modal_snooze_splices_into_the_digest_instead_of_replacing_it(
    monkeypatch, posts, spawned
):
    """Snooze-until… opens a modal; the submission carries no message. It
    used to replace the whole digest with one line of text. With the -pick
    click's copy remembered, only that row changes."""
    import json as _json

    opened = {}
    monkeypatch.setattr(slack_router, "_open_snooze_modal",
                        lambda **kw: opened.update(kw))
    monkeypatch.setattr(slack_router, "_run_plan_for_one_item", _resolved)
    monkeypatch.setattr(slack_router.pipeline, "_create_supabase_client", lambda: None)
    s0 = _digest(HASH_A, HASH_B)
    asyncio.run(slack_router._handle_block_action(_payload(s0, HASH_B, "snooze-ask-pick")))
    meta = {"verb": "snooze-ask", "code": CODE, "hash": HASH_B,
            "response_url": "https://hooks.slack.com/actions/T/b/pick",
            "week_iso": WEEK, "message_key": opened["message_key"],
            "action_id": opened["action_id"]}
    asyncio.run(slack_router._handle_view_submission({"view": {
        "callback_id": "snooze_until_modal",
        "private_metadata": _json.dumps(meta),
        "state": {"values": {"date_block": {"until_date": {"selected_date": "2026-10-20"}}}},
    }}))
    asyncio.run(spawned[-1])
    final = posts.last_blocks
    assert _item_view(final, HASH_A) == "buttons"
    assert _item_view(final, HASH_B).startswith("💤 Snoozed until 2026-10-20")
