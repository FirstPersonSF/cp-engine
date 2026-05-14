"""Tests for cp_engine.slack — message filtering, mention resolution.

The fetch_week + list_channel_map functions hit live external APIs
(Slack, MC-2), so those paths are covered by integration testing
(`cp slack-channels`, `cp slack-fetch`). Here we lock down the pure-
function logic: which messages get filtered, how mentions are resolved.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cp_engine.slack import (
    SlackMessage,
    _is_keepable,
    _resolve_mentions,
    _to_unix,
    _ts_to_iso,
)


# ──────────────────────────────────────────────────────────────────────
#  Message filtering
# ──────────────────────────────────────────────────────────────────────


def test_keepable_drops_bot_messages_with_no_user() -> None:
    assert not _is_keepable({"bot_id": "B123", "text": "Hi from bot"})


def test_keepable_drops_channel_join_and_leave() -> None:
    assert not _is_keepable({"subtype": "channel_join", "user": "U1", "text": "joined"})
    assert not _is_keepable({"subtype": "channel_leave", "user": "U1", "text": "left"})


def test_keepable_drops_thread_replies() -> None:
    """A thread reply has `thread_ts` pointing at the parent's ts;
    a top-level message has `thread_ts == ts` (or no thread_ts)."""
    parent = {"ts": "100.0", "thread_ts": "100.0", "user": "U1", "text": "top-level"}
    reply = {"ts": "101.0", "thread_ts": "100.0", "user": "U1", "text": "reply"}
    assert _is_keepable(parent)
    assert not _is_keepable(reply)


def test_keepable_keeps_top_level_messages_without_thread_ts() -> None:
    assert _is_keepable({"ts": "100.0", "user": "U1", "text": "hi"})


def test_keepable_drops_pinned_and_topic_changes() -> None:
    assert not _is_keepable({"subtype": "pinned_item", "user": "U1"})
    assert not _is_keepable({"subtype": "channel_topic", "user": "U1"})


# ──────────────────────────────────────────────────────────────────────
#  Mention resolution
# ──────────────────────────────────────────────────────────────────────


class _FakeUsersInfoClient:
    """Stub Slack client that returns canned users_info responses."""

    def __init__(self, users: dict[str, str]) -> None:
        self._users = users

    def users_info(self, *, user: str) -> dict:
        return {
            "user": {
                "name": user,
                "profile": {"display_name": self._users.get(user, ""), "real_name": ""},
            }
        }


def test_resolve_mentions_substitutes_user_ids_with_display_names() -> None:
    client = _FakeUsersInfoClient({"U123": "Maria", "U456": "Geoff"})
    cache: dict[str, str] = {}
    out = _resolve_mentions(
        "<@U123> can you ping <@U456>?", client, cache
    )
    assert out == "@Maria can you ping @Geoff?"


def test_resolve_mentions_uses_cache() -> None:
    """Same user mentioned twice → only one API call (verified by counting)."""
    calls: list[str] = []

    class Counting(_FakeUsersInfoClient):
        def users_info(self, *, user: str) -> dict:
            calls.append(user)
            return super().users_info(user=user)

    client = Counting({"U123": "Maria"})
    cache: dict[str, str] = {}
    _resolve_mentions("<@U123> and <@U123>", client, cache)
    assert calls == ["U123"]


# ──────────────────────────────────────────────────────────────────────
#  Timestamp conversions
# ──────────────────────────────────────────────────────────────────────


def test_to_unix_renders_microsecond_precision() -> None:
    dt = datetime(2026, 5, 11, 0, 0, 0, tzinfo=timezone.utc)
    assert _to_unix(dt) == f"{dt.timestamp():.6f}"


def test_to_unix_treats_naive_as_utc() -> None:
    naive = datetime(2026, 5, 11, 0, 0, 0)
    aware = naive.replace(tzinfo=timezone.utc)
    assert _to_unix(naive) == _to_unix(aware)


def test_ts_to_iso_renders_z_suffix() -> None:
    dt = datetime(2026, 5, 11, 0, 0, 0, tzinfo=timezone.utc)
    ts = f"{dt.timestamp():.6f}"
    assert _ts_to_iso(ts) == "2026-05-11T00:00:00Z"


def test_ts_to_iso_handles_garbage_input() -> None:
    assert _ts_to_iso("not-a-number") == ""


# ──────────────────────────────────────────────────────────────────────
#  SlackMessage dataclass
# ──────────────────────────────────────────────────────────────────────


def test_slack_message_is_frozen_and_hashable() -> None:
    m = SlackMessage(
        ts="100.0", iso="2026-05-11T00:00:00Z", user_name="Maria", text="hi"
    )
    with pytest.raises(Exception):
        m.text = "changed"  # type: ignore[misc]
    # Frozen dataclasses are hashable by default — useful for dedup sets.
    assert hash(m) == hash(m)
