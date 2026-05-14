"""Slack integration for the weekly digest pipeline.

This module is the glue between MC-2 (where `slack_channel_id` lives per
project) and the Slack Web API (where the actual messages live). Two
public entry points:

- `list_channel_map(config)` — returns one row per active engagement
  project: canonical CP code + company code + Slack channel id (or None
  if unmapped) + whether the project has `enable_slack=true`. Backing
  data for `cp slack-channels`.

- `fetch_week(token, channel_id, week_start)` — pulls one ISO week of
  top-level messages from a channel, filtering bots/joins/system noise
  and resolving user mentions to display names. Backing data for
  `cp slack-fetch` and the digest pipeline.

The canonical CP code (`ggl-5168`, `tel-5113`, ...) is constructed
exactly the same way as `sync_mc2._engagement_canonical_id`:
`<companies.code.lower()>-<projects.number>`. MC-2's `projects.code`
column is unreliable (mixed legacy `5113` and newer `GGL-activation`
strings) and explicitly not used.

Slack credentials come from `SLACK_BOT_TOKEN` in:
  1. `os.environ` (CI / explicit shell exports)
  2. `<mc-2 clone>/backend/.env` (canonical local-dev location)

Same dotenv fallback pattern as `_load_supabase_creds`.
"""

from __future__ import annotations

import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cp_engine.config import TenantConfig
from cp_engine.sync_mc2 import _read_dotenv

_SLACK_TOKEN_KEYS = ("SLACK_BOT_TOKEN",)


class SlackError(Exception):
    """Raised for Slack API or credential failures."""


@dataclass(frozen=True)
class ChannelMapRow:
    """One project's Slack-mapping status, as surfaced by `cp slack-channels`."""

    code: str            # canonical CP code, e.g. "ggl-5168"
    name: str            # project name
    company_code: str    # e.g. "GGL"
    status: str          # MC-2 mc_status (Open, Deal, Closed, Holding)
    enable_slack: bool   # MC-2's per-project toggle
    channel_id: str | None
    channel_name: str | None


@dataclass(frozen=True)
class SlackMessage:
    """One Slack message, normalized for plan-generation."""

    ts: str              # Slack timestamp string, e.g. "1715638400.001200"
    iso: str             # ISO-8601 UTC, e.g. "2026-05-13T22:00:00Z"
    user_name: str       # resolved display name, or "(unknown)"
    text: str            # message text with <@U…> mentions resolved to names


# ──────────────────────────────────────────────────────────────────────
#  Channel map
# ──────────────────────────────────────────────────────────────────────


def list_channel_map(config: TenantConfig) -> list[ChannelMapRow]:
    """Return one ChannelMapRow per non-archived, non-internal engagement project.

    Uses MC-2 directly (not the cached ProjectState from `read_projects`)
    because the Slack-mapping columns aren't part of the standard project
    sync — they're only relevant to this pipeline.
    """
    from cp_engine.sync_mc2 import _load_supabase_creds
    from supabase import create_client

    url, key = _load_supabase_creds(config)
    client = create_client(url, key)

    rows = (
        client.schema("public")
        .table("projects")
        .select(
            "number, name, mc_status, is_internal, enable_slack, "
            "slack_channel_id, slack_channel_name, "
            "companies!inner(code)"
        )
        .neq("mc_status", "Archived")
        .order("mc_status")
        .execute()
        .data
        or []
    )

    out: list[ChannelMapRow] = []
    for row in rows:
        if row.get("is_internal"):
            continue
        company = row.get("companies") or {}
        company_code = (company.get("code") or "").strip()
        number = row.get("number")
        if not company_code or number is None:
            continue
        code = f"{company_code.lower()}-{number}"
        out.append(
            ChannelMapRow(
                code=code,
                name=row.get("name") or "",
                company_code=company_code,
                status=row.get("mc_status") or "",
                enable_slack=bool(row.get("enable_slack")),
                channel_id=row.get("slack_channel_id") or None,
                channel_name=row.get("slack_channel_name") or None,
            )
        )
    out.sort(key=lambda r: (r.company_code, r.code))
    return out


# ──────────────────────────────────────────────────────────────────────
#  Credentials
# ──────────────────────────────────────────────────────────────────────


def load_slack_token(config: TenantConfig) -> str:
    """Resolve SLACK_BOT_TOKEN from env or `<mc-2 clone>/backend/.env`.

    Raises SlackError if neither source supplies it.
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if token:
        return token

    clone = config.local_repos.get("mc-2")
    if clone:
        env_file = Path(clone) / "backend" / ".env"
        file_creds = _read_dotenv(env_file, _SLACK_TOKEN_KEYS)
        token = file_creds.get("SLACK_BOT_TOKEN")
        if token:
            print(f"Loaded SLACK_BOT_TOKEN from {env_file}", file=sys.stderr)
            return token

    raise SlackError(
        "SLACK_BOT_TOKEN not found. Tried os.environ and "
        "`<mc-2 clone>/backend/.env`. Set the env var, or ensure "
        "`[local-repos].\"mc-2\"` in .cp-engine.local.toml points at "
        "a clone whose backend/.env declares SLACK_BOT_TOKEN."
    )


# ──────────────────────────────────────────────────────────────────────
#  Slack API client (thin wrapper over slack_sdk)
# ──────────────────────────────────────────────────────────────────────


def fetch_week(
    token: str,
    channel_id: str,
    week_start: datetime,
    *,
    week_end: datetime | None = None,
) -> list[SlackMessage]:
    """Pull one week of top-level messages from a Slack channel.

    Filters out:
      - bot messages (`subtype == "bot_message"`, or sent by a bot user)
      - channel join/leave messages (`subtype` in `channel_join`, `channel_leave`, etc.)
      - file_share rows that have no text body
      - thread replies (top-level only, `thread_ts != ts` is a reply)

    Resolves `<@U123>` mentions to `@DisplayName`. User lookups are
    cached for the duration of the call.

    `week_start` is the inclusive lower bound (UTC); `week_end` is the
    exclusive upper bound (defaults to week_start + 7 days).

    Raises SlackError on API failure or auth error.
    """
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError as exc:
        raise SlackError(
            "slack_sdk not installed. Run: pip install 'slack-sdk>=3.27'"
        ) from exc

    if week_end is None:
        from datetime import timedelta

        week_end = week_start + timedelta(days=7)

    oldest = _to_unix(week_start)
    latest = _to_unix(week_end)

    client = WebClient(token=token)
    user_cache: dict[str, str] = {}

    raw_messages: list[dict] = []
    cursor: str | None = None
    while True:
        try:
            resp = client.conversations_history(
                channel=channel_id,
                oldest=oldest,
                latest=latest,
                inclusive=False,
                limit=200,
                cursor=cursor,
            )
        except SlackApiError as exc:
            raise SlackError(
                f"conversations.history failed for {channel_id}: {exc.response.get('error')}"
            ) from exc

        raw_messages.extend(resp.get("messages") or [])
        if not resp.get("has_more"):
            break
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
        # Gentle pacing for Slack rate limits — Tier 3 is 50/min, sleeping
        # 1s between paged calls is overkill but cheap insurance.
        time.sleep(1.0)

    out: list[SlackMessage] = []
    for msg in raw_messages:
        if not _is_keepable(msg):
            continue
        text = _resolve_mentions(msg.get("text") or "", client, user_cache)
        if not text.strip():
            continue
        ts = msg.get("ts") or ""
        user_id = msg.get("user") or ""
        user_name = _resolve_user(user_id, client, user_cache) if user_id else "(unknown)"
        out.append(
            SlackMessage(
                ts=ts,
                iso=_ts_to_iso(ts),
                user_name=user_name,
                text=text,
            )
        )
    out.sort(key=lambda m: m.ts)
    return out


_SKIPPED_SUBTYPES = {
    "bot_message",
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "channel_archive",
    "channel_unarchive",
    "pinned_item",
    "unpinned_item",
    "reminder_add",
}


def _is_keepable(msg: dict) -> bool:
    if msg.get("subtype") in _SKIPPED_SUBTYPES:
        return False
    if msg.get("bot_id") and not msg.get("user"):
        return False
    # Thread replies (parent's ts != this msg's ts AND thread_ts is set) → skip.
    # Top-level thread starters have thread_ts == ts and should be kept.
    thread_ts = msg.get("thread_ts")
    if thread_ts and thread_ts != msg.get("ts"):
        return False
    return True


_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)>")


def _resolve_mentions(text: str, client, user_cache: dict[str, str]) -> str:
    def sub(match):
        uid = match.group(1)
        return "@" + _resolve_user(uid, client, user_cache)

    return _MENTION_RE.sub(sub, text)


def _resolve_user(user_id: str, client, user_cache: dict[str, str]) -> str:
    if user_id in user_cache:
        return user_cache[user_id]
    try:
        resp = client.users_info(user=user_id)
        profile = (resp.get("user") or {}).get("profile") or {}
        name = (
            profile.get("display_name")
            or profile.get("real_name")
            or (resp.get("user") or {}).get("name")
            or user_id
        )
    except Exception:
        # Any failure (Slack API error, permission denied, network) →
        # fall back to the raw ID. Better than crashing the whole digest.
        name = user_id
    user_cache[user_id] = name
    return name


def _to_unix(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"{dt.timestamp():.6f}"


def _ts_to_iso(ts: str) -> str:
    try:
        seconds = float(ts)
    except ValueError:
        return ""
    return (
        datetime.fromtimestamp(seconds, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
