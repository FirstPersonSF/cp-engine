"""Generate a `cp ingest` plan from one week of Slack messages.

Mirrors `plan_from_transcript.py` but tuned for async chat. The two
differences in the prompt:

1. **Always emit a digest** — even if no structured items are confident
   enough to extract. The digest is the contract: each project gets one
   bullet per week summarizing the channel's chatter.

2. **Be more conservative about verbs.** Async chat is full of "maybe"s
   and reactions. A decision needs an explicit commitment; an ask needs
   an open loop; inbound needs substance, not reactions. Skip
   stakeholders entirely — Slack rarely introduces genuinely new
   external people and the false-positive rate is too high.

Schema produced is identical to `plan_from_transcript` except it always
includes a `slack_digest` entry per project (the digest bullet) and
substitutes `transcript.source` = "slack" with a synthetic `path`
identifying the channel + week.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from cp_engine.config import TenantConfig
from cp_engine.ingest import IngestPlanError, _validate_plan
from cp_engine.plan_from_transcript import (
    _load_project_context,
    _call_claude,
    _extract_yaml,
)
from cp_engine.slack import SlackMessage

# Slack weeks tend to be much smaller than meeting transcripts (most
# active project channels are <50 messages/week). 30k chars is generous.
_MAX_MESSAGES_CHARS = 30_000


class SlackPlanError(Exception):
    """Raised when Claude fails to return a valid Slack-digest plan."""


@dataclass
class GeneratedSlackPlan:
    plan: dict
    raw_response: str
    project_code: str
    week: str
    channel_id: str
    model: str


def generate_slack_plan(
    *,
    config: TenantConfig,
    project_code: str,
    week: str,
    channel_id: str,
    messages: list[SlackMessage],
    model: str = "claude-opus-4-7",
    api_key: str | None = None,
) -> GeneratedSlackPlan:
    """Read messages + project context, ask Claude for a digest plan, validate it."""
    project_ctx = _load_project_context(config, project_code)
    formatted = _format_messages(messages)
    prompt = _build_slack_prompt(
        messages_text=formatted,
        project_context=project_ctx,
        project_code=project_code,
        week=week,
        channel_id=channel_id,
        team=config.team,
        message_count=len(messages),
    )

    response_text = _call_claude(prompt, model=model, api_key=api_key)
    yaml_text = _extract_yaml(response_text)

    try:
        plan = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise SlackPlanError(
            f"Claude returned non-YAML output: {exc}\n--- response ---\n{response_text[:500]}"
        ) from exc

    if not isinstance(plan, dict):
        raise SlackPlanError(
            f"Claude returned a non-mapping plan: {type(plan).__name__}"
        )

    # Inject `week` into every slack_digest item before validation. The
    # prompt asks Claude to include it, but observed behavior is flaky —
    # Claude sometimes drops the field on otherwise-valid responses. Since
    # the caller always knows the target week, we can fill it server-side
    # rather than depend on prompt adherence.
    projects = plan.get("projects") or {}
    proj_block = projects.get(project_code) or {}
    for verb_key in ("slack_digest", "slack-digest", "record-slack-digest"):
        for item in proj_block.get(verb_key) or []:
            if isinstance(item, dict) and not item.get("week"):
                item["week"] = week

    try:
        _validate_plan(plan)
    except IngestPlanError as exc:
        raise SlackPlanError(f"plan failed validation: {exc}") from exc

    # Post-validation: ensure the digest bullet got emitted for the target
    # project. The prompt requires it, but be paranoid: if a quiet week
    # ended up with no bullet at all, the caller should know.
    digest_items = proj_block.get("slack_digest") or proj_block.get("record-slack-digest")
    if not digest_items and messages:
        raise SlackPlanError(
            "plan is missing the required `slack_digest` entry for "
            f"{project_code} (had {len(messages)} messages — should have produced a digest)"
        )

    return GeneratedSlackPlan(
        plan=plan,
        raw_response=response_text,
        project_code=project_code,
        week=week,
        channel_id=channel_id,
        model=model,
    )


def _format_messages(messages: list[SlackMessage]) -> str:
    """Render messages as a numbered list for the prompt.

    Includes ISO timestamp + author + text. Truncates if the total exceeds
    `_MAX_MESSAGES_CHARS` — quiet weeks won't hit this; busy ones may.
    """
    if not messages:
        return "(no messages this week)"
    lines: list[str] = []
    total = 0
    for i, m in enumerate(messages, start=1):
        line = f"{i}. [{m.iso} · {m.user_name}] {m.text}"
        if total + len(line) > _MAX_MESSAGES_CHARS:
            lines.append(f"... ({len(messages) - i + 1} more messages truncated) ...")
            break
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


def _build_slack_prompt(
    *,
    messages_text: str,
    project_context: str,
    project_code: str,
    week: str,
    channel_id: str,
    message_count: int,
    team: tuple[str, ...] = (),
) -> str:
    today = datetime.now().date().isoformat()
    if team:
        team_block = (
            "These names are INTERNAL TEAM MEMBERS, not project stakeholders:\n"
            + ", ".join(team)
        )
    else:
        team_block = "(No team roster declared in tenant config.)"
    synthetic_path = f"slack://{channel_id}/{week}"
    return _PROMPT_TEMPLATE.format(
        today=today,
        project_code=project_code,
        week=week,
        channel_id=channel_id,
        synthetic_path=synthetic_path,
        project_context=project_context,
        messages_text=messages_text,
        message_count=message_count,
        team_block=team_block,
    )


_PROMPT_TEMPLATE = """\
You are summarizing one ISO week of Slack chatter into a `cp ingest` plan.
Your output is a YAML plan that `cp ingest` will execute against the
project's sprint file.

# Today
{today}

# Target project
{project_code}

# ISO week being summarized
{week} (Monday 00:00 UTC through next Monday 00:00 UTC)

# Slack channel
{channel_id} ({message_count} top-level messages — bot/system messages
already filtered out, thread replies dropped, mentions resolved)

# Internal team
{team_block}

# What's already known about this project
{project_context}

# Schema you must produce

```yaml
transcript:
  source: slack
  path: {synthetic_path}

projects:
  {project_code}:
    slack_digest:                # ALWAYS emit exactly one entry, even
      - text: "..."              # if you have no structured verbs below.
        week: "{week}"           # This is the contract.
    inbound:                     # OPTIONAL — only if confident
      - text: "..."
        date: "YYYY-MM-DD"
        who: "<who said it>"
    asks:                        # OPTIONAL — open loops we're waiting on
      - text: "..."
        who: "<who we're asking>"
        by: "YYYY-MM-DD"
        date: "YYYY-MM-DD"
    decisions:                   # OPTIONAL — explicit commitments only
      - text: "..."
        date: "YYYY-MM-DD"
        cross_cutting: false
    risks:                       # OPTIONAL — flagged concerns
      - text: "..."
        severity: "watching"
        category: "schedule"
        date: "YYYY-MM-DD"
```

# Rules

1. **The `slack_digest` entry is REQUIRED.** Exactly one entry. The
   `text` is one paragraph (60–120 words) capturing the dominant
   threads, decisions made or pending, and unresolved questions from
   the week. Write it as a human standup-style summary, not a list of
   message-by-message recaps. If the week was genuinely quiet (no
   meaningful chatter despite messages existing), say so plainly.
2. **Verbs are bonus extraction.** Async chat is harder to classify
   than a meeting transcript. Only emit `inbound`/`asks`/`decisions`/
   `risks` when you have HIGH CONFIDENCE the message qualifies.
   - **Decisions** need explicit commitment ("I'll own Firebase
     setup", "deadline moved to 6/8"). "Maybe we should..." is NOT
     a decision.
   - **Asks** need an open loop ("@brandon any word from Rena?").
     Resolved questions are not asks.
   - **Inbound** is substantive information from the client side
     ("Maria said launch pushed to 6/8"). Drop reactions ("+1",
     "thanks", "👍").
   - **Risks** need an explicit concern, not vibes ("worried we
     won't make 6/1").
3. **Skip stakeholders entirely.** Slack rarely introduces genuinely
   new external people, and the false-positive rate is too high for
   automated extraction. The `record-stakeholder` verb is intentionally
   omitted from the schema above.
4. **Don't duplicate what's already known.** If the project_context
   already records a decision or ask, do NOT re-emit it as a verb.
   The digest can still mention it ("Tony confirmed his ownership of
   Firebase setup, decided last week").
5. **Internal team members never become stakeholders.** (Moot since
   we skip stakeholders entirely, but stays true in spirit.)
6. **Date fields:** use the message's ISO date (left of the `T`),
   not today. If a date isn't extractable, use today ({today}).
7. **No empty lists.** If a verb has no entries, omit it. (The
   `slack_digest` entry is the only one that's always present.)

# Output format

Respond with ONLY the YAML plan inside a single ```yaml fenced code
block. No preamble, no explanation, no postscript.

# Slack messages (chronological)

{messages_text}
"""
