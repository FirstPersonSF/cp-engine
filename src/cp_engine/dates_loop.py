"""Weekly Slack dates loop — the delivery-date agreement mechanism.

Every Monday, each active project/initiative with a mapped Slack channel
gets a post: what's due this week, due in the next window, undated open
commitments ("needs a date"), and slipped items. A tenant-wide rollup
goes to the partners channel. Commitments consolidation, cp-engine #38;
design: cp/docs/plans/2026-07-07-commitments-consolidation-design.md.

The post is not just a reminder — it's the RATIFICATION step: a date that
has been posted twice without change auto-promotes ``date_status``
``proposed → agreed`` (any due-date change resets the counter via the
mc-2 PATCH endpoint). Past-due open items are stamped ``slipped``.
Post-only v1 — no Slack interactivity.

Data sources: MC-2 ``public.commitments`` (the dated-obligations store)
plus the estimator schedule's milestone/feedback items (via
``prep_planning._fetch_mc2_schedule_milestones``) so client delivery
dates and team commitments appear in one post.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from cp_engine import mc2_db
from cp_engine.config import TenantConfig
from cp_engine.mc2_db import Tables

log = logging.getLogger(__name__)

_COMMITMENT_COLUMNS = (
    "id, description, owner_email, owner_name, direction, due_date, "
    "date_status, project_id, initiative_id, status, posted_count, "
    "source_kind, created_at"
)

# Posts with an unchanged date needed before proposed → agreed. The mc-2
# PATCH endpoint resets posted_count on any due_date change, so
# posted_count is by construction "posts at the current date".
_RATIFY_AFTER_POSTS = 2

# TTL for undated auto-ingested commitments (#136). A transcript rarely
# states a date, so meeting-ingest rows land undated — an UNCONFIRMED
# PROPOSAL, not an agreed obligation, and structurally invisible to
# 'slipped' (which needs a past due date). Rows still undated + 'proposed'
# after _EXPIRE_AFTER_DAYS auto-close to status='expired' (mc-2 mig 133);
# rows entering the warn window are flagged in the posts one loop-run
# ahead so nothing dies silently. Dating the row or any date_status
# change cancels the TTL. Only source_kind='meeting_ingest' — session,
# manual, and migration rows were human-authored on purpose.
_EXPIRE_AFTER_DAYS = 14
_EXPIRE_WARN_AFTER_DAYS = 7


def _ttl_bucket(c: dict, today: date) -> str | None:
    """'expire' | 'warn' | None for one open commitment row.

    Age counts from created_at. warn = would expire by the next weekly
    run; expire = past the TTL now.
    """
    if (
        c.get("due_date")
        or c.get("source_kind") != "meeting_ingest"
        or (c.get("date_status") or "proposed") != "proposed"
    ):
        return None
    raw = c.get("created_at") or ""
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None
    age = (today - created).days
    if age >= _EXPIRE_AFTER_DAYS:
        return "expire"
    if age >= _EXPIRE_WARN_AFTER_DAYS:
        return "warn"
    return None

# MC-2 app_config key holding the partners-rollup channel id. Channel
# configuration lives in MC-2 (one home), not in .cp-engine.toml — same
# principle as the per-project channel map.
_PARTNERS_CHANNEL_KEY = "dates_loop_partners_channel"


def _partners_channel(client: Any) -> str | None:
    """The tenant-wide rollup channel id from MC-2 app_config, or None.

    The jsonb value is accepted as either a bare string ("C0…") or an
    object with a "channel" key. Absent/blank → the rollup is skipped
    (per-project posts still go out)."""
    try:
        rows = (
            client.table(Tables.APP_CONFIG)
            .select("key, value")
            .eq("key", _PARTNERS_CHANNEL_KEY)
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001 — rollup is optional
        log.warning("partners-channel lookup failed: %s", exc)
        return None
    if not rows:
        return None
    value = rows[0].get("value")
    if isinstance(value, dict):
        value = value.get("channel")
    return value if isinstance(value, str) and value else None


@dataclass
class ChannelPost:
    """One rendered per-project post, ready for chat.postMessage."""

    code: str
    name: str
    channel_ids: tuple[str, ...]
    text: str
    commitment_ids: tuple[str, ...]  # commitments included (ratification set)
    posted: bool = False


@dataclass
class DatesLoopResult:
    posts: list[ChannelPost] = field(default_factory=list)
    partners_channel: str | None = None  # resolved from MC-2 app_config
    partners_text: str | None = None
    partners_posted: bool = False
    skipped_no_channel: list[str] = field(default_factory=list)
    slipped_stamped: int = 0
    agreed_promoted: int = 0
    posted_count_bumped: int = 0
    expired_stamped: int = 0  # undated meeting-ingest rows past the TTL (#136)
    expire_warned: int = 0  # rows entering the warn window this run
    errors: list[str] = field(default_factory=list)


def _fmt_day(d: date) -> str:
    return d.strftime("%a %b %-d")


def _fmt_commitment(c: dict, *, with_date: bool = True) -> str:
    who = c.get("owner_name") or c.get("owner_email") or "unowned"
    arrow = {
        "us_to_them": "we owe",
        "them_to_us": "they owe",
        "internal": "internal",
    }.get(c.get("direction") or "", "")
    bits = [c["description"], f"({who}"]
    if arrow:
        bits[-1] += f", {arrow}"
    bits[-1] += ")"
    if with_date and c.get("due_date"):
        d = date.fromisoformat(c["due_date"])
        status = c.get("date_status") or "proposed"
        marker = "" if status == "agreed" else f" _[{status}]_"
        bits.append(f"— {_fmt_day(d)}{marker}")
    return "• " + " ".join(bits)


def _render_project_post(
    *,
    code: str,
    name: str,
    commitments: list[dict],
    milestones: list[tuple[date, str]],
    today: date,
    window_days: int,
    ttl_buckets: dict[str, str] | None = None,
) -> tuple[str, list[str]]:
    """Render one project's post; returns (text, commitment_ids_included).

    Empty sections are dropped; a project with nothing in any section
    returns ("", []) and gets no post.
    """
    week_end = today + timedelta(days=6)
    horizon = today + timedelta(days=window_days)

    slipped: list[dict] = []
    this_week: list[dict] = []
    upcoming: list[dict] = []
    undated: list[dict] = []
    for c in commitments:
        if not c.get("due_date"):
            undated.append(c)
            continue
        d = date.fromisoformat(c["due_date"])
        if d < today:
            slipped.append(c)
        elif d <= week_end:
            this_week.append(c)
        elif d <= horizon:
            upcoming.append(c)

    ms_this_week = [(d, label) for d, label in milestones if today <= d <= week_end]
    ms_upcoming = [(d, label) for d, label in milestones if week_end < d <= horizon]

    sections: list[str] = []
    if slipped:
        lines = "\n".join(_fmt_commitment(c) for c in sorted(slipped, key=lambda c: c["due_date"]))
        sections.append(f":rotating_light: *Slipped — date passed, still open*\n{lines}")
    if this_week or ms_this_week:
        lines = [
            f"• {label} — {_fmt_day(d)} _(milestone)_"
            for d, label in sorted(ms_this_week)
        ]
        lines += [
            _fmt_commitment(c)
            for c in sorted(this_week, key=lambda c: c["due_date"])
        ]
        sections.append("*Due this week*\n" + "\n".join(lines))
    if upcoming or ms_upcoming:
        lines = [
            f"• {label} — {_fmt_day(d)} _(milestone)_"
            for d, label in sorted(ms_upcoming)
        ]
        lines += [
            _fmt_commitment(c)
            for c in sorted(upcoming, key=lambda c: c["due_date"])
        ]
        sections.append(
            f"*Due in the next {window_days} days*\n" + "\n".join(lines)
        )
    buckets = ttl_buckets or {}
    expiring_now = [c for c in undated if buckets.get(c["id"]) == "expire"]
    undated = [c for c in undated if buckets.get(c["id"]) != "expire"]
    if undated or expiring_now:
        lines = []
        for c in undated:
            line = _fmt_commitment(c, with_date=False)
            if buckets.get(c["id"]) == "warn":
                line += " — _expires next Monday unless dated_"
            lines.append(line)
        if expiring_now:
            lines.append(
                f"_{len(expiring_now)} undated meeting-ingest "
                f"commitment{'s' if len(expiring_now) != 1 else ''} auto-expired "
                f"(14+ days without a date)_"
            )
        sections.append("*Needs a date*\n" + "\n".join(lines))

    if not sections:
        return "", []

    header = (
        f":date: *{name or code} — dates check* (week of {_fmt_day(today)})\n"
        "_A date posted twice without objection is agreed. Reply here or "
        "fix it in Mission Control._"
    )
    included = [c["id"] for c in (slipped + this_week + upcoming + undated)]
    return header + "\n\n" + "\n\n".join(sections), included


def _render_partners_rollup(
    *,
    dated_events: list[tuple[date, str, str]],  # (date, code, label)
    slipped_total: int,
    undated_total: int,
    today: date,
    window_days: int,
    expire_warn: list[tuple[str, str]] | None = None,  # (code, description)
    expired_total: int = 0,
) -> str | None:
    """Tenant-wide pile-up view for the partners channel."""
    expire_warn = expire_warn or []
    if (
        not dated_events
        and not slipped_total
        and not undated_total
        and not expire_warn
        and not expired_total
    ):
        return None
    lines = [
        f":date: *Tenant dates — next {window_days} days* (week of {_fmt_day(today)})"
    ]
    for d, code, label in sorted(dated_events):
        lines.append(f"• {_fmt_day(d)} — `{code}` {label}")
    tail = []
    if slipped_total:
        tail.append(f":rotating_light: {slipped_total} slipped")
    if undated_total:
        tail.append(f":grey_question: {undated_total} open commitments with no date")
    if tail:
        lines.append(" · ".join(tail))
    if expire_warn:
        lines.append(
            f":hourglass_flowing_sand: *{len(expire_warn)} undated "
            f"commitment{'s' if len(expire_warn) != 1 else ''} expire next "
            "Monday unless dated:*"
        )
        lines.extend(f"• `{code}` {desc}" for code, desc in expire_warn)
    if expired_total:
        lines.append(
            f"_{expired_total} undated meeting-ingest "
            f"commitment{'s' if expired_total != 1 else ''} auto-expired this "
            "week (14+ days without a date)_"
        )
    # Name the pile-up when one exists: >3 events inside any 10-day span.
    dates = sorted(d for d, _, _ in dated_events)
    for i in range(len(dates)):
        span = [d for d in dates if dates[i] <= d <= dates[i] + timedelta(days=9)]
        if len(span) > 3:
            lines.append(
                f":warning: *Pile-up: {len(span)} dated events between "
                f"{_fmt_day(span[0])} and {_fmt_day(span[-1])}* — what ships "
                "early, what gets a buffer?"
            )
            break
    return "\n".join(lines)


def _fetch_open_commitments(client: Any) -> list[dict]:
    resp = (
        client.table(Tables.COMMITMENTS)
        .select(_COMMITMENT_COLUMNS)
        .eq("status", "open")
        .execute()
    )
    return resp.data or []


def _fetch_milestones(client: Any, code: str) -> list[tuple[date, str]]:
    """Undone MC-2 schedule milestones/feedback for one engagement code."""
    from cp_engine.prep_planning import _fetch_mc2_schedule_milestones

    shim = SimpleNamespace(code=code)
    out: list[tuple[date, str]] = []
    for m in _fetch_mc2_schedule_milestones(client, shim):
        # Milestone is a TypedDict — dict access, not attributes.
        try:
            out.append(
                (
                    date.fromisoformat(m.get("date") or ""),
                    m.get("deliverable") or "(untitled)",
                )
            )
        except (TypeError, ValueError):
            continue
    return out


def run_dates_loop(
    config: TenantConfig,
    *,
    today: date | None = None,
    post: bool = False,
    window_days: int | None = None,
) -> DatesLoopResult:
    """Build (and optionally post) the weekly dates-loop messages.

    ``post=False`` is a dry run: everything is rendered, nothing is sent,
    and no ratification state changes. ``post=True`` sends each project
    post to its mapped channel(s), sends the partners rollup, then applies
    the ratification write-backs (posted_count/last_posted_at bumps,
    proposed→agreed promotions, slipped stamps) for commitments that
    actually reached a channel.
    """
    from cp_engine import slack as slack_mod

    result = DatesLoopResult()
    today = today or date.today()
    window_days = window_days or config.dates_loop.window_days

    client = mc2_db.get_client(config)
    result.partners_channel = _partners_channel(client)
    commitments = _fetch_open_commitments(client)
    by_owner: dict[str, list[dict]] = {}
    for c in commitments:
        owner_id = c.get("project_id") or c.get("initiative_id")
        if owner_id:
            by_owner.setdefault(owner_id, []).append(c)

    # TTL buckets for undated meeting-ingest rows (#136) — computed over
    # ALL open commitments, mapped channel or not, so expiry can't be
    # dodged by an unmapped project.
    ttl_buckets = {
        c["id"]: b for c in commitments if (b := _ttl_bucket(c, today))
    }
    result.expire_warned = sum(1 for b in ttl_buckets.values() if b == "warn")

    rows = [r for r in slack_mod.list_channel_map(config) if r.owner_id]
    code_by_owner = {r.owner_id: r.code for r in rows}
    horizon = today + timedelta(days=window_days)
    dated_events: list[tuple[date, str, str]] = []
    slipped_total = 0
    undated_total = 0
    expire_warn: list[tuple[str, str]] = []
    expired_total = 0
    for c in commitments:
        bucket = ttl_buckets.get(c["id"])
        if bucket is None:
            continue
        owner_id = c.get("project_id") or c.get("initiative_id")
        code = code_by_owner.get(owner_id, "?")
        if bucket == "warn":
            expire_warn.append((code, c["description"]))
        else:
            expired_total += 1

    for row in rows:
        row_commitments = by_owner.get(row.owner_id, [])
        milestones = (
            _fetch_milestones(client, row.code)
            if row.kind == "engagement"
            else []
        )
        # Tenant rollup accumulates across ALL mapped rows, channel or not.
        for c in row_commitments:
            if not c.get("due_date"):
                # Rows expiring this run are terminal — not "needs a date".
                if ttl_buckets.get(c["id"]) != "expire":
                    undated_total += 1
                continue
            d = date.fromisoformat(c["due_date"])
            if d < today:
                slipped_total += 1
            elif d <= horizon:
                dated_events.append((d, row.code, c["description"]))
        dated_events.extend(
            (d, row.code, label) for d, label in milestones if today <= d <= horizon
        )

        text, included = _render_project_post(
            code=row.code,
            name=row.name,
            commitments=row_commitments,
            milestones=milestones,
            today=today,
            window_days=window_days,
            ttl_buckets=ttl_buckets,
        )
        if not text:
            continue
        if not (row.enable_slack and row.channel_ids):
            result.skipped_no_channel.append(row.code)
            continue
        result.posts.append(
            ChannelPost(
                code=row.code,
                name=row.name,
                channel_ids=row.channel_ids,
                text=text,
                commitment_ids=tuple(included),
            )
        )

    result.partners_text = _render_partners_rollup(
        dated_events=dated_events,
        slipped_total=slipped_total,
        undated_total=undated_total,
        today=today,
        window_days=window_days,
        expire_warn=expire_warn,
        expired_total=expired_total,
    )

    if not post:
        return result

    try:
        from slack_sdk import WebClient
    except ImportError as exc:
        raise slack_mod.SlackError(
            "slack_sdk not installed. Run: pip install 'slack-sdk>=3.27'"
        ) from exc

    web = WebClient(token=slack_mod.load_slack_token(config))
    for cpost in result.posts:
        ok = False
        for channel_id in cpost.channel_ids:
            try:
                slack_mod.post_channel(web, channel_id=channel_id, text=cpost.text)
                ok = True
            except slack_mod.SlackError as exc:
                result.errors.append(f"{cpost.code}/{channel_id}: {exc}")
        cpost.posted = ok

    if result.partners_text and result.partners_channel:
        try:
            slack_mod.post_channel(
                web,
                channel_id=result.partners_channel,
                text=result.partners_text,
            )
            result.partners_posted = True
        except slack_mod.SlackError as exc:
            result.errors.append(
                f"partners/{result.partners_channel}: {exc}"
            )

    _apply_ratification(client, result, commitments, today=today)
    _apply_expiry(client, result, commitments, ttl_buckets)
    return result


def _apply_expiry(
    client: Any,
    result: DatesLoopResult,
    commitments: list[dict],
    ttl_buckets: dict[str, str],
) -> None:
    """Close undated meeting-ingest rows past the TTL as status='expired'
    (#136, mc-2 mig 133). Post-send only, like ratification — a dry run
    reports what would expire and writes nothing. Expired rows keep their
    cp_hash, so re-ingesting the same meeting cannot resurrect them."""
    now = datetime.now(timezone.utc).isoformat()
    for c in commitments:
        if ttl_buckets.get(c["id"]) != "expire":
            continue
        try:
            client.table(Tables.COMMITMENTS).update(
                {"status": "expired", "updated_at": now}
            ).eq("id", c["id"]).eq("status", "open").execute()
            result.expired_stamped += 1
        except Exception as exc:  # noqa: BLE001 — count and continue
            result.errors.append(f"expire/{c['id']}: {exc}")


def _apply_ratification(
    client: Any,
    result: DatesLoopResult,
    commitments: list[dict],
    *,
    today: date,
) -> None:
    """Post-send write-backs: bump posted_count, promote agreed, stamp slipped.

    Only commitments that reached at least one channel get their
    ratification counters bumped — a failed post must not count toward
    agreement. Slipped stamping applies to every past-due open row (it's
    a fact about the date, not about the post).
    """
    by_id = {c["id"]: c for c in commitments}
    now = datetime.now(timezone.utc).isoformat()

    posted_ids = {
        cid
        for cpost in result.posts
        if cpost.posted
        for cid in cpost.commitment_ids
    }
    for cid in posted_ids:
        c = by_id.get(cid)
        if c is None:
            continue
        fields: dict[str, Any] = {
            "posted_count": (c.get("posted_count") or 0) + 1,
            "last_posted_at": now,
            "updated_at": now,
        }
        if (
            c.get("due_date")
            and c.get("date_status") == "proposed"
            and fields["posted_count"] >= _RATIFY_AFTER_POSTS
            and date.fromisoformat(c["due_date"]) >= today
        ):
            fields["date_status"] = "agreed"
            result.agreed_promoted += 1
        try:
            client.table(Tables.COMMITMENTS).update(fields).eq("id", cid).execute()
            result.posted_count_bumped += 1
        except Exception as exc:  # noqa: BLE001 — count and continue
            result.errors.append(f"ratify/{cid}: {exc}")

    for c in commitments:
        if not c.get("due_date") or c.get("date_status") == "slipped":
            continue
        if date.fromisoformat(c["due_date"]) < today:
            try:
                client.table(Tables.COMMITMENTS).update(
                    {"date_status": "slipped", "updated_at": now}
                ).eq("id", c["id"]).execute()
                result.slipped_stamped += 1
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"slip/{c['id']}: {exc}")
