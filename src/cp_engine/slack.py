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

The canonical CP code is `sync_mc2._engagement_canonical_id(row)` —
the slugified `full_job_name`
(`ibx-5192-platform-sales-readiness-summit`). Using the shared function
guarantees the Slack channel-map keys match the project codes used
everywhere else in the tenant. MC-2's `projects.code` column is
unreliable (mixed legacy `5113` and newer `GGL-activation` strings) and
explicitly not used.

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
from cp_engine import mc2_db
from cp_engine.mc2_db import Tables

_SLACK_TOKEN_KEYS = ("SLACK_BOT_TOKEN",)


class SlackError(Exception):
    """Raised for Slack API or credential failures."""


@dataclass(frozen=True)
class ChannelMapRow:
    """One row in the Slack-channel map — either an engagement project or
    an internal initiative — as surfaced by `cp slack-channels`.

    `channel_ids` is the canonical source of truth — entries can have
    one or more channels (e.g. a main channel plus a team-internal one).
    The digest pipeline iterates each channel and merges the output into
    a single weekly bullet with one paragraph per channel.

    `primary_channel_id` mirrors MC-2's legacy scalar `slack_channel_id`
    column (engagement projects only — initiatives never had a scalar).
    Used for display only; the digest pipeline does NOT special-case it.
    """

    code: str                              # canonical CP code (engagements: "ggl-5168"; initiatives: "mission-control")
    name: str                              # project / initiative name
    company_code: str                      # e.g. "GGL", "1PI", "CNC"
    status: str                            # mc_status (Open/Deal/...) or initiative status (Active/On hold/...)
    enable_slack: bool                     # per-row toggle
    channel_ids: tuple[str, ...]           # all channels, including primary
    primary_channel_id: str | None         # legacy scalar; None for initiatives
    primary_channel_name: str | None       # legacy scalar; None for initiatives
    kind: str = "engagement"               # "engagement" or "initiative"
    owner_id: str | None = None            # MC-2 uuid (projects.id / initiatives.id)


@dataclass(frozen=True)
class SlackMessage:
    """One Slack message, normalized for plan-generation."""

    ts: str              # Slack timestamp string, e.g. "1715638400.001200"
    iso: str             # ISO-8601 UTC, e.g. "2026-05-13T22:00:00Z"
    user_name: str       # resolved display name, or "(unknown)"
    text: str            # message text with <@U…> mentions resolved to names


@dataclass(frozen=True)
class FetchedChannel:
    """One week of messages from a single Slack channel, with metadata."""

    channel_id: str
    channel_name: str    # e.g. "ibx_5167_ddi_platform_video_team", or "" if lookup failed
    messages: tuple[SlackMessage, ...]


# ──────────────────────────────────────────────────────────────────────
#  Channel map
# ──────────────────────────────────────────────────────────────────────


def list_channel_map(config: TenantConfig) -> list[ChannelMapRow]:
    """Return ChannelMapRows for non-archived engagement projects AND initiatives.

    Two streams: engagements (`projects` table, non-internal) and
    initiatives (`initiatives` table — internal workstreams parallel to
    engagements per docs/plans/2026-05-14-internal-initiatives.md).

    Uses MC-2 directly (not the cached ProjectState from `read_projects`)
    because the Slack-mapping columns aren't part of the standard project
    sync — they're only relevant to this pipeline.
    """
    from cp_engine import mc2_db
    from cp_engine.mc2_bindings import (
        fetch_binding_rows,
        hydrate_initiative_row,
        hydrate_project_row,
    )
    from cp_engine.sync_mc2 import _engagement_canonical_id

    client = mc2_db.get_client(config)

    # Stream A: engagement projects.
    engagement_rows = (
        client.schema("public")
        .table(Tables.PROJECTS)
        .select(mc2_db.PROJECTS_SLACK_COLUMNS)
        .neq("mc_status", "Archived")
        .order("mc_status")
        .execute()
        .data
        or []
    )
    # Channel ids live in project_integrations bindings (read-flip): the ''
    # singleton is the primary channel, labeled rows are related channels.
    # One batch fetch, then overlay the legacy slack keys onto each row.
    project_bindings = fetch_binding_rows(
        client.schema("public"),
        project_ids=[r["id"] for r in engagement_rows if r.get("id")],
    )
    for row in engagement_rows:
        hydrate_project_row(row, project_bindings.get(row.get("id"), []))

    out: list[ChannelMapRow] = []
    for row in engagement_rows:
        if row.get("is_internal"):
            continue
        company = row.get("companies") or {}
        company_code = (company.get("code") or "").strip()
        number = row.get("number")
        if not company_code or number is None:
            continue
        code = _engagement_canonical_id(row)

        primary = row.get("slack_channel_id") or None
        raw_ids = row.get("slack_channel_ids") or []
        if not isinstance(raw_ids, list):
            raw_ids = []
        channel_ids = tuple(c for c in raw_ids if isinstance(c, str) and c)
        if not channel_ids and primary:
            channel_ids = (primary,)

        out.append(
            ChannelMapRow(
                code=code,
                name=row.get("name") or "",
                company_code=company_code,
                status=row.get("mc_status") or "",
                enable_slack=bool(row.get("enable_slack")),
                channel_ids=channel_ids,
                primary_channel_id=primary,
                primary_channel_name=row.get("slack_channel_name") or None,
                kind="engagement",
                owner_id=row.get("id") or None,
            )
        )

    # Stream B: initiatives (internal workstreams). Channel ids come from
    # initiative-owned bindings ('' singleton + labeled extras). Status uses
    # the initiative vocabulary ("Active", "On hold", "Done", "Archived").
    initiative_rows = (
        client.schema("public")
        .table(Tables.INITIATIVES)
        .select(mc2_db.INITIATIVES_SLACK_COLUMNS)
        .neq("status", "Archived")
        .order("status")
        .execute()
        .data
        or []
    )
    initiative_bindings = fetch_binding_rows(
        client.schema("public"),
        initiative_ids=[r["id"] for r in initiative_rows if r.get("id")],
    )
    for row in initiative_rows:
        hydrate_initiative_row(row, initiative_bindings.get(row.get("id"), []))

    for row in initiative_rows:
        company = row.get("companies") or {}
        company_code = (company.get("code") or "").strip()
        init_code = (row.get("code") or "").strip()
        if not init_code or not company_code:
            continue

        raw_ids = row.get("slack_channel_ids") or []
        if not isinstance(raw_ids, list):
            raw_ids = []
        channel_ids = tuple(c for c in raw_ids if isinstance(c, str) and c)

        out.append(
            ChannelMapRow(
                code=init_code,
                name=row.get("name") or "",
                company_code=company_code,
                status=row.get("status") or "",
                enable_slack=bool(row.get("enable_slack")),
                channel_ids=channel_ids,
                primary_channel_id=None,
                primary_channel_name=None,
                kind="initiative",
                owner_id=row.get("id") or None,
            )
        )

    out.sort(key=lambda r: (r.company_code, r.kind, r.code))
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


def post_dm(
    client,
    *,
    user_id: str,
    text: str,
    blocks: list[dict] | None = None,
) -> str:
    """Post a Slack DM via chat.postMessage with channel=<user_id>.

    Slack allows DMing a user by passing their user ID as the `channel`
    parameter — the API auto-opens a DM if one doesn't already exist.
    Returns the message timestamp (ts) for traceability.

    `text` is always passed (notification preview + accessibility
    fallback). `blocks` is optional — when provided, Slack renders the
    rich layout and falls back to `text` for notifications/screen-readers.

    Raises `SlackError` on any API failure with the Slack-returned
    reason verbatim.
    """
    kwargs = {"channel": user_id, "text": text}
    if blocks is not None:
        kwargs["blocks"] = blocks
    try:
        resp = client.chat_postMessage(**kwargs)
    except Exception as exc:  # slack_sdk.errors.SlackApiError or transport
        raise SlackError(
            f"chat_postMessage failed for user={user_id}: {exc}"
        ) from exc
    if not resp.get("ok"):
        raise SlackError(
            f"chat_postMessage returned ok=false for user={user_id}: "
            f"{resp.get('error', 'unknown')}"
        )
    return resp.get("ts", "")


def post_channel(
    client,
    *,
    channel_id: str,
    text: str,
    blocks: list[dict] | None = None,
) -> str:
    """Post to a Slack CHANNEL via chat.postMessage.

    Same contract as :func:`post_dm` (which passes a user id as the
    channel param); this variant exists so call sites read correctly and
    so channel-specific failures carry the channel id in the error.
    The bot must be a member of the channel (`/invite`) or Slack returns
    ``not_in_channel``.
    """
    kwargs = {"channel": channel_id, "text": text}
    if blocks is not None:
        kwargs["blocks"] = blocks
    try:
        resp = client.chat_postMessage(**kwargs)
    except Exception as exc:  # slack_sdk.errors.SlackApiError or transport
        raise SlackError(
            f"chat_postMessage failed for channel={channel_id}: {exc}"
        ) from exc
    if not resp.get("ok"):
        raise SlackError(
            f"chat_postMessage returned ok=false for channel={channel_id}: "
            f"{resp.get('error', 'unknown')}"
        )
    return resp.get("ts", "")


def fetch_channels(
    token: str,
    channel_ids: tuple[str, ...] | list[str],
    week_start: datetime,
    *,
    week_end: datetime | None = None,
) -> list[FetchedChannel]:
    """Fan out `fetch_week` across multiple channels.

    Used by multi-channel projects (e.g. ibx-5167 has both a main and a
    `_team` channel). Returns one FetchedChannel per ID, in the same
    order as the input. Channels with zero messages are still returned
    (caller decides whether to skip them).

    Reuses one WebClient across channels so user-info lookups are cached.
    """
    try:
        from slack_sdk import WebClient
    except ImportError as exc:
        raise SlackError(
            "slack_sdk not installed. Run: pip install 'slack-sdk>=3.27'"
        ) from exc

    client = WebClient(token=token)
    user_cache: dict[str, str] = {}
    name_cache: dict[str, str] = {}
    out: list[FetchedChannel] = []
    for cid in channel_ids:
        messages = _fetch_one(client, cid, week_start, week_end, user_cache)
        name = _resolve_channel_name(client, cid, name_cache)
        out.append(
            FetchedChannel(channel_id=cid, channel_name=name, messages=tuple(messages))
        )
    return out


def fetch_week(
    token: str,
    channel_id: str,
    week_start: datetime,
    *,
    week_end: datetime | None = None,
) -> list[SlackMessage]:
    """Pull one week of top-level messages from a single Slack channel.

    Single-channel convenience wrapper around `fetch_channels`. Returns
    a plain message list; channel name + ID metadata are dropped on the
    floor (use `fetch_channels` if you need them).

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
    fetched = fetch_channels(token, (channel_id,), week_start, week_end=week_end)
    return list(fetched[0].messages)


def _fetch_one(
    client,
    channel_id: str,
    week_start: datetime,
    week_end: datetime | None,
    user_cache: dict[str, str],
) -> list[SlackMessage]:
    """Pull + filter one week of messages from one channel.

    Internal helper. The public surface is `fetch_week` (single channel)
    and `fetch_channels` (fan out across many).
    """
    try:
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


def _resolve_channel_name(client, channel_id: str, name_cache: dict[str, str]) -> str:
    """Resolve a Slack channel ID to its name via `conversations.info`.

    Cached for the duration of the run. Returns "" on failure so callers
    can fall back to displaying the raw ID.
    """
    if channel_id in name_cache:
        return name_cache[channel_id]
    try:
        resp = client.conversations_info(channel=channel_id)
        name = (resp.get("channel") or {}).get("name") or ""
    except Exception:
        name = ""
    name_cache[channel_id] = name
    return name


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
