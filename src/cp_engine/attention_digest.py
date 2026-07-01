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
    resolved_week_iso = week_iso  # the week the items actually come from (see fallback below)
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
        resolved_week_iso = prev_iso
        if not sprint_dir.is_dir():
            log.warning("attention-digest: previous week %s also missing; empty digest", sprint_dir)
            return {"past_due": [], "escalated": [], "allocation": [], "week_iso": None}
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
        # The sprint week these items were read from (may be the fallback
        # previous week). Embedded into action-button payloads so a click
        # resolves against THIS file even after a sprint rollover.
        "week_iso": resolved_week_iso,
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


# Slack rejects messages with more than 50 blocks. The daily digest emits
# ~4 blocks per past-due ask (section + context + divider + actions) and
# ~4 per escalated risk. On busy weeks (W22 had 24 past-due asks) the
# unbounded loop would silently kill the whole digest send.
#
# Two caps:
#  - MAX_ITEMS_PER_SECTION (10): per-section soft cap; sorted by urgency
#    so the most important items survive. A single full section emits
#    ~10*4 + section heading + truncation context ≈ 42 blocks, leaving
#    headroom under 50.
#  - MAX_TOTAL_BLOCKS (45): hard cap across all sections. If multiple
#    sections are populated, we stop emitting items (and append a
#    truncation context block to whichever section we're in) once we'd
#    breach the budget. Leaves ~5 blocks of headroom for the header +
#    final dividers.
#
# Truncated sections append a "+N more not shown (see weekly-cp.md or
# run /cp-prep for full list)" context block so the recipient knows
# something was dropped and where to look for the full list.
MAX_ITEMS_PER_SECTION = 10
MAX_TOTAL_BLOCKS = 45


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

    Each section is capped at ``MAX_ITEMS_PER_SECTION`` items (sorted by
    urgency) to keep total block count safely under Slack's 50-block hard
    limit. Truncation is communicated via a "+ N more …" context block.

    Reference: https://api.slack.com/block-kit
    """
    clickup_task_ids = clickup_task_ids or {}
    past_due = digest.get("past_due") or []
    escalated = digest.get("escalated") or []
    allocation = digest.get("allocation") or []
    # Sprint week the items were read from; embedded into button payloads so
    # a click resolves against the right sprint file after a rollover.
    week_iso = digest.get("week_iso")

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

    # Per-item block cost (used by the global budget):
    #   past-due ask / escalated risk: 4 blocks (divider + section + context + actions)
    #   allocation entry:              1 block  (section only)
    # Plus per-section overhead: 1 heading + optional 1 truncation context.

    def _remaining_budget() -> int:
        # +1 reserved for a possible truncation block we may need to append.
        return MAX_TOTAL_BLOCKS - len(blocks) - 1

    if past_due:
        past_due_sorted = sorted(past_due, key=lambda a: a.days_past, reverse=True)
        # Per-section cap.
        section_cap = MAX_ITEMS_PER_SECTION
        # Global cap. Section heading is one of the first blocks we'll
        # emit; budget for items is whatever's left after that, divided
        # by 4 blocks/item.
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"⏰ *{len(past_due_sorted)} ask{'s' if len(past_due_sorted) != 1 else ''} past due*",
            },
        })
        budget_items = max(0, _remaining_budget() // 4)
        cap = min(section_cap, budget_items)
        shown = past_due_sorted[:cap]
        dropped = len(past_due_sorted) - len(shown)
        for ask in shown:
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
                    "elements": _ask_action_buttons(
                        code=ask.code, cp_hash=ask.hash, week_iso=week_iso
                    ),
                })
        if dropped > 0:
            blocks.append({
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": (
                        f"_+ {dropped} more past-due ask{'s' if dropped != 1 else ''} "
                        "not shown (see weekly-cp.md or run /cp-prep for full list)_"
                    ),
                }],
            })

    if escalated:
        # severity is uniformly "escalated" here (the classifier already
        # filtered); secondary key is most-recently raised first.
        escalated_sorted = sorted(escalated, key=lambda r: r.raised, reverse=True)
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🚨 *{len(escalated_sorted)} risk{'s' if len(escalated_sorted) != 1 else ''} escalated this week*",
            },
        })
        budget_items = max(0, _remaining_budget() // 4)
        cap = min(MAX_ITEMS_PER_SECTION, budget_items)
        shown_risks = escalated_sorted[:cap]
        dropped_risks = len(escalated_sorted) - len(shown_risks)
        for risk in shown_risks:
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
                "elements": _risk_action_buttons(
                    code=risk.code, cp_hash=risk.hash, week_iso=week_iso
                ),
            })
        if dropped_risks > 0:
            blocks.append({
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": (
                        f"_+ {dropped_risks} more escalated risk"
                        f"{'s' if dropped_risks != 1 else ''} not shown "
                        "(see weekly-cp.md or run /cp-prep for full list)_"
                    ),
                }],
            })

    if allocation:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "📊 *Allocation watch*"},
        })
        # Allocation entries are 1 block each.
        budget_items = max(0, _remaining_budget())
        cap = min(MAX_ITEMS_PER_SECTION, budget_items)
        shown_alloc = allocation[:cap]
        dropped_alloc = len(allocation) - len(shown_alloc)
        for entry in shown_alloc:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"• {entry}"},
            })
        if dropped_alloc > 0:
            blocks.append({
                "type": "context",
                "elements": [{
                    "type": "mrkdwn",
                    "text": (
                        f"_+ {dropped_alloc} more allocation item"
                        f"{'s' if dropped_alloc != 1 else ''} not shown_"
                    ),
                }],
            })

    return blocks


def _pipe(verb: str, code: str, cp_hash: str, week_iso: str | None) -> str:
    """Button `value` payload: `verb|code|hash` plus an optional 4th
    `|week_iso` part so the click handler resolves against the sprint
    file for the week the digest was rendered for (not the click's week).

    Omitting week_iso when None keeps the 3-part shape old buttons had —
    but every live render now passes it, so this is belt-and-suspenders.
    """
    base = f"{verb}|{code}|{cp_hash}"
    return f"{base}|{week_iso}" if week_iso else base


def _ask_action_buttons(*, code: str, cp_hash: str, week_iso: str | None = None) -> list[dict]:
    """Three buttons for a non-ClickUp ask: Mark closed, Snooze 7d, Snooze until...

    The digest's sprint week (`week_iso`) is embedded ONLY in the button
    `value` (which the click handler parses), not in `action_id`. The
    action_id keys the surgical response-URL update to the clicked block
    and must stay stable/short; the week doesn't belong there.
    """
    return [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✅ Mark closed"},
            "style": "primary",
            "value": _pipe("close-ask", code, cp_hash, week_iso),
            "action_id": f"close-ask_{code}_{cp_hash}",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "💤 Snooze 7d"},
            "value": _pipe("snooze-ask-7d", code, cp_hash, week_iso),
            "action_id": f"snooze-ask-7d_{code}_{cp_hash}",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "📅 Snooze until…"},
            "value": _pipe("snooze-ask-pick", code, cp_hash, week_iso),
            "action_id": f"snooze-ask-pick_{code}_{cp_hash}",
        },
    ]


def _risk_action_buttons(*, code: str, cp_hash: str, week_iso: str | None = None) -> list[dict]:
    """See `_ask_action_buttons` — week_iso threaded into `value` only."""
    return [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✅ Resolve"},
            "style": "primary",
            "value": _pipe("resolve-risk", code, cp_hash, week_iso),
            "action_id": f"resolve-risk_{code}_{cp_hash}",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "💤 Snooze 7d"},
            "value": _pipe("snooze-risk-7d", code, cp_hash, week_iso),
            "action_id": f"snooze-risk-7d_{code}_{cp_hash}",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "📅 Snooze until…"},
            "value": _pipe("snooze-risk-pick", code, cp_hash, week_iso),
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
