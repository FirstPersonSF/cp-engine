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


# ──────────────────────────────────────────────────────────────────────
#  Multi-channel digest formatting (plan_from_slack)
# ──────────────────────────────────────────────────────────────────────


def test_format_multi_channel_emits_per_channel_headers() -> None:
    """Each channel renders with a `## Channel: #<name> (<id>)` header."""
    from cp_engine.plan_from_slack import _format_multi_channel
    from cp_engine.slack import FetchedChannel, SlackMessage

    msg_a = SlackMessage(ts="100.0", iso="2026-05-13T00:00:00Z", user_name="Maria", text="hi")
    msg_b = SlackMessage(ts="101.0", iso="2026-05-13T00:01:00Z", user_name="Geoff", text="hey")
    channels = [
        FetchedChannel(
            channel_id="C123", channel_name="ibx_5167_main", messages=(msg_a,)
        ),
        FetchedChannel(
            channel_id="C456", channel_name="ibx_5167_team", messages=(msg_b,)
        ),
    ]
    out = _format_multi_channel(channels)
    assert "## Channel: #ibx_5167_main (C123)" in out
    assert "## Channel: #ibx_5167_team (C456)" in out
    assert "[2026-05-13T00:00:00Z · Maria] hi" in out
    assert "[2026-05-13T00:01:00Z · Geoff] hey" in out


def test_format_multi_channel_falls_back_to_id_when_name_missing() -> None:
    """If conversations.info failed and channel_name is '', use the raw ID."""
    from cp_engine.plan_from_slack import _format_multi_channel
    from cp_engine.slack import FetchedChannel

    channels = [FetchedChannel(channel_id="C789", channel_name="", messages=())]
    out = _format_multi_channel(channels)
    # Header uses the raw ID twice (label + parens) when name is empty.
    assert "## Channel: C789 (C789)" in out
    assert "(no messages this week)" in out


def test_digest_shape_instructions_single_vs_multi_channel() -> None:
    """Single-channel: one paragraph, no labels.
    Multi-channel: one labeled paragraph per channel."""
    from cp_engine.plan_from_slack import _digest_shape_instructions
    from cp_engine.slack import FetchedChannel

    one = [FetchedChannel(channel_id="C1", channel_name="main", messages=())]
    two = [
        FetchedChannel(channel_id="C1", channel_name="main", messages=()),
        FetchedChannel(channel_id="C2", channel_name="team", messages=()),
    ]
    assert "Single channel" in _digest_shape_instructions(one)
    assert "No channel label" in _digest_shape_instructions(one) or "no channel label" in _digest_shape_instructions(one).lower()
    multi = _digest_shape_instructions(two)
    assert "2 channels" in multi
    assert "**#main**" in multi
    assert "**#team**" in multi


def test_slack_message_is_frozen_and_hashable() -> None:
    m = SlackMessage(
        ts="100.0", iso="2026-05-11T00:00:00Z", user_name="Maria", text="hi"
    )
    with pytest.raises(Exception):
        m.text = "changed"  # type: ignore[misc]
    # Frozen dataclasses are hashable by default — useful for dedup sets.
    assert hash(m) == hash(m)


# ──────────────────────────────────────────────────────────────────────
#  post_dm — chat.postMessage to a user ID
# ──────────────────────────────────────────────────────────────────────


def test_post_dm_calls_chat_postmessage_with_user_id_as_channel() -> None:
    """post_dm passes user_id as the `channel` arg — Slack auto-opens the DM."""
    from cp_engine.slack import post_dm

    received: dict = {}

    class _FakeClient:
        def chat_postMessage(self, *, channel, text):
            received["channel"] = channel
            received["text"] = text
            return {"ok": True, "ts": "9999.1111"}

    ts = post_dm(_FakeClient(), user_id="U12ABCDE", text="hello")
    assert received["channel"] == "U12ABCDE"
    assert received["text"] == "hello"
    assert ts == "9999.1111"


def test_post_dm_raises_on_ok_false() -> None:
    from cp_engine.slack import post_dm, SlackError

    class _FakeClient:
        def chat_postMessage(self, *, channel, text):
            return {"ok": False, "error": "channel_not_found"}

    with pytest.raises(SlackError, match="channel_not_found"):
        post_dm(_FakeClient(), user_id="U", text="hi")


def test_post_dm_wraps_underlying_exceptions_as_slack_error() -> None:
    """Any exception thrown by the client is wrapped in SlackError."""
    from cp_engine.slack import post_dm, SlackError

    class _FakeClient:
        def chat_postMessage(self, *, channel, text):
            raise RuntimeError("network down")

    with pytest.raises(SlackError, match="chat_postMessage failed"):
        post_dm(_FakeClient(), user_id="U", text="hi")


# ──────────────────────────────────────────────────────────────────────
#  Channel map — canonical id
# ──────────────────────────────────────────────────────────────────────


class _FakeQuery:
    """Minimal stand-in for the supabase query-builder chain. Returns
    canned rows for the named table; every chained method returns self."""

    def __init__(self, rows_by_table: dict[str, list[dict]]) -> None:
        self._rows_by_table = rows_by_table
        self._rows: list[dict] = []

    def schema(self, _name):
        return self

    def table(self, name):
        self._rows = self._rows_by_table.get(name, [])
        return self

    def select(self, *_a, **_k):
        return self

    def neq(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def execute(self):
        class _Resp:
            def __init__(self, data):
                self.data = data

        return _Resp(self._rows)


def _patch_channel_map(monkeypatch, rows_by_table: dict[str, list[dict]]) -> None:
    import cp_engine.slack as slack_mod
    import cp_engine.sync_mc2 as sync_mod

    monkeypatch.setattr(
        sync_mod, "_load_supabase_creds", lambda config: ("http://x", "key")
    )
    fake = _FakeQuery(rows_by_table)
    import supabase

    monkeypatch.setattr(supabase, "create_client", lambda url, key: fake)
    # `from supabase import create_client` resolves at call time inside
    # list_channel_map, so patching the module attribute is sufficient.
    _ = slack_mod  # keep import side effect explicit


def test_channel_map_code_is_slugified_full_job_name(monkeypatch) -> None:
    """An engagement row with full_job_name produces a ChannelMapRow whose
    code is the slugified full_job_name — matching _engagement_canonical_id,
    NOT the legacy <company>-<number> form."""
    from cp_engine.slack import list_channel_map

    _patch_channel_map(
        monkeypatch,
        {
            "projects": [
                {
                    "number": 5192,
                    "name": "Platform Sales Readiness Summit",
                    "mc_status": "Open",
                    "is_internal": False,
                    "enable_slack": True,
                    "slack_channel_id": "C999",
                    "slack_channel_name": "ibx-srs",
                    "slack_channel_ids": ["C999"],
                    "full_job_name": "IBX 5192 Platform Sales Readiness Summit",
                    "companies": {"code": "IBX"},
                }
            ],
            "initiatives": [],
        },
    )

    rows = list_channel_map(config=None)  # type: ignore[arg-type]
    assert len(rows) == 1
    row = rows[0]
    assert row.code == "ibx-5192-platform-sales-readiness-summit"
    assert row.company_code == "IBX"
    assert row.enable_slack is True
    assert row.channel_ids == ("C999",)


def test_channel_map_code_falls_back_when_no_full_job_name(monkeypatch) -> None:
    """Defensive: a row missing full_job_name falls back to the legacy
    <company>-<number> form via _engagement_canonical_id."""
    from cp_engine.slack import list_channel_map

    _patch_channel_map(
        monkeypatch,
        {
            "projects": [
                {
                    "number": 5168,
                    "name": "Activation",
                    "mc_status": "Open",
                    "is_internal": False,
                    "enable_slack": True,
                    "slack_channel_id": "C111",
                    "slack_channel_name": "ggl",
                    "slack_channel_ids": ["C111"],
                    "full_job_name": None,
                    "companies": {"code": "GGL"},
                }
            ],
            "initiatives": [],
        },
    )

    rows = list_channel_map(config=None)  # type: ignore[arg-type]
    assert len(rows) == 1
    assert rows[0].code == "ggl-5168"
