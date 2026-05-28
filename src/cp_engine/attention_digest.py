"""Daily attention digest — past-due asks, escalated risks, allocation pressure.

Lever 2 of cp-engine v0.12.0. See:
  cp/docs/plans/2026-05-27-clickup-bidirectional-and-daily-digest-design.md

Classifiers are pure (no Slack I/O, no LLM calls). The CLI subcommand
wires these up against the live tenant, the markdown composer renders,
and `_post_digest_to_recipients` (invoked by `--post-to-slack`) DMs
the digest to each configured Slack user ID.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


_SNOOZE_MARKER_RE = re.compile(
    r"\s*<!--\s*cp:snoozed-until=(?P<until>\d{4}-\d{2}-\d{2})\s*-->\s*"
)


def _strip_snooze_marker(s: str) -> str:
    """Remove any cp:snoozed-until=... HTML comment from a string."""
    return _SNOOZE_MARKER_RE.sub(" ", s).strip()


# Bullet shape (real production, see ingest._write_ask):
#   - [open · YYYY-MM-DD · Who[ · by YYYY-MM-DD]] text <!-- cp:hash=8hex -->
# Separator is U+00B7 (MIDDLE DOT). `who` may contain spaces, parens,
# commas — anything except `]` or the literal " · " separator. The non-
# greedy `.+?` on text stops at the trailing hash marker.
_OPEN_ASK_RE = re.compile(
    r"^- \[open · (?P<asked>\d{4}-\d{2}-\d{2}) · (?P<who>[^\]]+?)"
    r"(?: · by (?P<by>\d{4}-\d{2}-\d{2}))?\]\s+(?P<text>.+?)\s+<!--\s*cp:hash=(?P<hash>[0-9a-f]{8})\s*-->\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class PastDueAsk:
    """One past-due ask found by scanning a sprint file.

    Fields:
        code:       project/initiative code (e.g., "ggl-5168", "mission-control")
        text:       the ask body, no trailing hash marker
        who:        the assignee field (verbatim from the bullet)
        asked:      asked_date (when the bullet was first written)
        by:         due-date if present; None for "no-by stale" asks
        days_past:  how many days overdue. For by-date asks, today-by.
                    For no-by asks, today-asked.
        hash:       8-char content hash (round-trips with ClickUp)
    """
    code: str
    text: str
    who: str
    asked: date
    by: date | None
    days_past: int
    hash: str


def _find_past_due_asks(
    *,
    sprint_files: Iterable[Path],
    today: date,
    no_by_threshold_days: int = 7,
) -> list[PastDueAsk]:
    """Scan sprint files and return asks that are past due.

    Past-due definition:
      - bullet has `· by <date>` and `<date>` < today  (immediate overdue), OR
      - bullet has NO `by` and asked_date is >= no_by_threshold_days ago (stale).

    `[closed]` bullets are skipped entirely.
    """
    out: list[PastDueAsk] = []
    for path in sprint_files:
        code = path.stem
        body = path.read_text(encoding="utf-8")
        for m in _OPEN_ASK_RE.finditer(body):
            snooze_match = _SNOOZE_MARKER_RE.search(m.group(0))
            if snooze_match:
                snooze_until = date.fromisoformat(snooze_match.group("until"))
                if snooze_until > today:
                    continue  # still snoozed
            asked = date.fromisoformat(m.group("asked"))
            by_str = m.group("by")
            text = _strip_snooze_marker(m.group("text"))
            who = m.group("who").strip()
            cp_hash = m.group("hash")
            if by_str:
                by = date.fromisoformat(by_str)
                if by < today:
                    out.append(PastDueAsk(
                        code=code, text=text, who=who,
                        asked=asked, by=by,
                        days_past=(today - by).days,
                        hash=cp_hash,
                    ))
            else:
                stale_days = (today - asked).days
                if stale_days >= no_by_threshold_days:
                    out.append(PastDueAsk(
                        code=code, text=text, who=who,
                        asked=asked, by=None,
                        days_past=stale_days,
                        hash=cp_hash,
                    ))
    return out


# Bullet shape (real production; see ingest._write_risk):
#   Engine: `- [<severity> · <category> · YYYY-MM-DD] text <!-- cp:hash=8hex -->`
#   Human:  `- [risk · <severity> · <category> · YYYY-MM-DD] text <!-- cp:hash=8hex -->`
# The optional `(?:risk · )?` prefix handles both. Category may contain
# spaces but not `]` or ` · ` (since that's the separator).
_RISK_RE = re.compile(
    r"^- \[(?:risk · )?(?P<sev>[a-z]+) · (?P<cat>[^\]]+?) · (?P<raised>\d{4}-\d{2}-\d{2})\]"
    r"\s+(?P<text>.+?)\s+<!--\s*cp:hash=(?P<hash>[0-9a-f]{8})\s*-->\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class EscalatedRisk:
    """One recently-escalated risk found by scanning a sprint file.

    Fields:
        code:     project/initiative code (sprint file's stem)
        text:     the risk body, no trailing hash marker
        category: the risk's category field (e.g., "scope", "capacity")
        raised:   the date the risk was raised
        hash:     8-char content hash
    """
    code: str
    text: str
    category: str
    raised: date
    hash: str


def _find_escalated_risks(
    *,
    sprint_files: Iterable[Path],
    today: date,
    window_days: int = 7,
) -> list[EscalatedRisk]:
    """Scan sprint files and return risks recently escalated.

    A risk qualifies if severity == "escalated" AND its raised date is
    >= (today - window_days). Boundary is INCLUSIVE (a risk raised
    exactly window_days ago counts).
    """
    out: list[EscalatedRisk] = []
    cutoff = today - timedelta(days=window_days)
    for path in sprint_files:
        code = path.stem
        body = path.read_text(encoding="utf-8")
        for m in _RISK_RE.finditer(body):
            if m.group("sev") != "escalated":
                continue
            snooze_match = _SNOOZE_MARKER_RE.search(m.group(0))
            if snooze_match:
                snooze_until = date.fromisoformat(snooze_match.group("until"))
                if snooze_until > today:
                    continue  # still snoozed
            raised = date.fromisoformat(m.group("raised"))
            if raised < cutoff:
                continue
            out.append(EscalatedRisk(
                code=code,
                text=_strip_snooze_marker(m.group("text")),
                category=m.group("cat").strip(),
                raised=raised,
                hash=m.group("hash"),
            ))
    return out


def attention_digest(
    *,
    config,
    today: date,
    past_due_threshold_days: int = 7,
    escalated_window_days: int = 7,
) -> dict:
    """Run all three classifiers against the current sprint dir.

    Reads sprint files at `<config.root>/sprints/<current ISO week>/*.md`,
    runs `_find_past_due_asks` and `_find_escalated_risks`, returns a dict
    consumed by `compose_digest`.

    Allocation is a stub (empty list) — Task 2.3 leaves it pending per
    the design doc; future tasks may populate from the workload signal.

    Args:
      config: TenantConfig (only `.root: Path` is used).
      today: date for "as-of" computations.
      past_due_threshold_days: stale-no-by threshold for asks.
      escalated_window_days: how recently a risk must have escalated.

    Returns:
      {"past_due": list[PastDueAsk], "escalated": list[EscalatedRisk],
       "allocation": []}
    """
    # current_sprint_week_iso takes a datetime; use today at midnight.
    from cp_engine.sprints import current_sprint_week_iso
    week_iso = current_sprint_week_iso(datetime(today.year, today.month, today.day))
    sprint_dir = config.root / "sprints" / week_iso
    if not sprint_dir.is_dir():
        # Fallback: try the previous ISO week. Mondays-after-fresh-week-start
        # mean today's W## dir often doesn't exist yet — no meeting has scaffolded
        # it — but last week's W## has all the past-due asks we care about.
        log.warning(
            "attention-digest: %s missing; falling back to previous week",
            sprint_dir,
        )
        prev_iso = current_sprint_week_iso(
            datetime(today.year, today.month, today.day) - timedelta(days=7)
        )
        sprint_dir = config.root / "sprints" / prev_iso
        if not sprint_dir.is_dir():
            log.warning("attention-digest: previous week %s also missing; empty digest", sprint_dir)
            return {"past_due": [], "escalated": [], "allocation": []}
    sprint_files = sorted(sprint_dir.glob("*.md"))
    # Exclude tenant-level scaffolding files; only per-project sprint files matter.
    sprint_files = [p for p in sprint_files if not p.name.startswith("_")]
    return {
        "past_due": _find_past_due_asks(
            sprint_files=sprint_files, today=today,
            no_by_threshold_days=past_due_threshold_days,
        ),
        "escalated": _find_escalated_risks(
            sprint_files=sprint_files, today=today,
            window_days=escalated_window_days,
        ),
        "allocation": [],
    }


def compose_digest(digest: dict, *, recipient_name: str, today: date) -> str:
    """Render the digest dict as Slack-flavored markdown.

    Empty digest → "all clear" affirmation (silence is ambiguous; we
    always post something so the recipient knows the cron ran).

    Non-empty digest → one section per non-empty list, ordered:
    past_due, escalated, allocation. Past-due asks sorted by `days_past`
    desc; risks by `raised` desc.
    """
    past_due = digest.get("past_due") or []
    escalated = digest.get("escalated") or []
    allocation = digest.get("allocation") or []

    if not past_due and not escalated and not allocation:
        return (
            f"**{recipient_name}, all clear this morning.** "
            "No past-due asks, no new escalations, allocation within caps."
        )

    lines: list[str] = [f"**{recipient_name}, this morning's cp scan:**", ""]

    if past_due:
        past_due_sorted = sorted(past_due, key=lambda a: a.days_past, reverse=True)
        noun = "ask" if len(past_due_sorted) == 1 else "asks"
        lines.append(f"⏰ **{len(past_due_sorted)} {noun} past due**")
        for ask in past_due_sorted:
            date_str = _md_format_date(ask.by or ask.asked)
            lines.append(
                f"• `{ask.code}` — {ask.text} ({ask.days_past}d past · {date_str})"
            )
        lines.append("")

    if escalated:
        escalated_sorted = sorted(escalated, key=lambda r: r.raised, reverse=True)
        noun = "risk" if len(escalated_sorted) == 1 else "risks"
        lines.append(f"🚨 **{len(escalated_sorted)} {noun} escalated this week**")
        for risk in escalated_sorted:
            lines.append(f"• `{risk.code}` — {risk.text}")
        lines.append("")

    if allocation:
        # Stub for future Task. Just render a heading + each entry as-is.
        lines.append("📊 **Allocation watch**")
        for entry in allocation:
            lines.append(f"• {entry}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _md_format_date(d: date) -> str:
    """Format as M/D (e.g., 5/16)."""
    return f"{d.month}/{d.day}"


def _render_digest_blocks(
    digest: dict,
    *,
    recipient_name: str,
    clickup_task_ids: dict[str, str] | None = None,
) -> list[dict]:
    """Render the daily attention digest as a Block Kit `blocks` array.

    `clickup_task_ids` maps cp_hash → ClickUp task_id for any ask pushed to
    ClickUp via v0.12's pipeline. Those asks get a single 'Open in ClickUp'
    link button instead of the Resolve/Snooze trio; v0.12's bidirectional
    close-loop is the authoritative closure path for them.

    Reference: https://api.slack.com/block-kit
    """
    clickup_task_ids = clickup_task_ids or {}
    past_due = digest.get("past_due") or []
    escalated = digest.get("escalated") or []
    allocation = digest.get("allocation") or []

    if not past_due and not escalated and not allocation:
        return [{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{recipient_name}, all clear this morning.* "
                    "No past-due asks, no new escalations, allocation within caps."
                ),
            },
        }]

    blocks: list[dict] = [{
        "type": "header",
        "text": {"type": "plain_text", "text": f"{recipient_name}, your cp scan"},
    }]

    if past_due:
        past_due_sorted = sorted(past_due, key=lambda a: a.days_past, reverse=True)
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"⏰ *{len(past_due_sorted)} ask{'s' if len(past_due_sorted) != 1 else ''} past due*",
            },
        })
        for ask in past_due_sorted:
            blocks.append({"type": "divider"})
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"`{ask.code}` — {ask.text}"},
            })
            blocks.append({
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": f"{ask.days_past}d past · asked {ask.asked.isoformat()} · {ask.who}",
                }],
            })
            clickup_id = clickup_task_ids.get(ask.hash)
            if clickup_id:
                blocks.append({
                    "type": "actions",
                    "elements": [{
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open in ClickUp"},
                        "url": f"https://app.clickup.com/t/{clickup_id}",
                    }],
                })
            else:
                blocks.append({
                    "type": "actions",
                    "elements": _ask_action_buttons(code=ask.code, cp_hash=ask.hash),
                })

    if escalated:
        escalated_sorted = sorted(escalated, key=lambda r: r.raised, reverse=True)
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🚨 *{len(escalated_sorted)} risk{'s' if len(escalated_sorted) != 1 else ''} escalated this week*",
            },
        })
        for risk in escalated_sorted:
            blocks.append({"type": "divider"})
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"`{risk.code}` — {risk.text}"},
            })
            blocks.append({
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": f"{risk.category} · raised {risk.raised.isoformat()}",
                }],
            })
            blocks.append({
                "type": "actions",
                "elements": _risk_action_buttons(code=risk.code, cp_hash=risk.hash),
            })

    if allocation:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "📊 *Allocation watch*"},
        })
        for entry in allocation:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"• {entry}"},
            })

    return blocks


def _ask_action_buttons(*, code: str, cp_hash: str) -> list[dict]:
    """Three buttons for a non-ClickUp ask: Mark closed, Snooze 7d, Snooze until..."""
    return [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✅ Mark closed"},
            "style": "primary",
            "value": f"close-ask|{code}|{cp_hash}",
            "action_id": f"close-ask_{code}_{cp_hash}",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "💤 Snooze 7d"},
            "value": f"snooze-ask-7d|{code}|{cp_hash}",
            "action_id": f"snooze-ask-7d_{code}_{cp_hash}",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "📅 Snooze until…"},
            "value": f"snooze-ask-pick|{code}|{cp_hash}",
            "action_id": f"snooze-ask-pick_{code}_{cp_hash}",
        },
    ]


def _risk_action_buttons(*, code: str, cp_hash: str) -> list[dict]:
    return [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✅ Resolve"},
            "style": "primary",
            "value": f"resolve-risk|{code}|{cp_hash}",
            "action_id": f"resolve-risk_{code}_{cp_hash}",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "💤 Snooze 7d"},
            "value": f"snooze-risk-7d|{code}|{cp_hash}",
            "action_id": f"snooze-risk-7d_{code}_{cp_hash}",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "📅 Snooze until…"},
            "value": f"snooze-risk-pick|{code}|{cp_hash}",
            "action_id": f"snooze-risk-pick_{code}_{cp_hash}",
        },
    ]


def _post_digest_to_recipients(
    *,
    config,
    digest: dict,
    recipient_name: str = "Drew",
    clickup_task_ids: dict[str, str] | None = None,
) -> list[str]:
    """Post the digest as a Slack DM to each configured recipient.

    Reads recipients from `config.attention_digest.recipients`. Empty
    list → raises `SlackError` with a clear message (caller can decide
    to log + skip). Returns the list of message timestamps for the
    successful posts.

    Sends BOTH the markdown text (notification preview + accessibility
    fallback) AND the Block Kit blocks (rich rendering with per-item
    action buttons). `clickup_task_ids` maps cp_hash → ClickUp task_id
    for asks already pushed to ClickUp; those asks get an "Open in
    ClickUp" link button instead of Resolve/Snooze.

    If ANY post fails, the function raises (doesn't retry, doesn't
    partial-succeed silently). Slack's API is reliable enough that
    partial-success isn't worth the complexity.
    """
    from cp_engine import slack as slack_mod
    from slack_sdk import WebClient

    recipients = config.attention_digest.recipients
    if not recipients:
        raise slack_mod.SlackError(
            "No recipients configured. Set [attention_digest].recipients "
            "in .cp-engine.toml to a list of Slack user IDs."
        )

    text_fallback = compose_digest(
        digest, recipient_name=recipient_name, today=date.today()
    )
    blocks = _render_digest_blocks(
        digest,
        recipient_name=recipient_name,
        clickup_task_ids=clickup_task_ids or {},
    )

    token = slack_mod.load_slack_token(config)
    client = WebClient(token=token)

    timestamps: list[str] = []
    for user_id in recipients:
        ts = slack_mod.post_dm(
            client, user_id=user_id, text=text_fallback, blocks=blocks
        )
        timestamps.append(ts)
    return timestamps
