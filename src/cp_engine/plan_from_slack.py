"""Generate a `cp ingest` plan from one week of Slack messages.

Mirrors `plan_from_transcript.py` but tuned for async chat. Two
differences in the prompt:

1. **Always emit a digest** — even if no structured items are confident
   enough to extract. The digest is the contract: each project gets one
   bullet per week summarizing the channel's chatter.

2. **Be more conservative about verbs.** Async chat is full of "maybe"s
   and reactions. A decision needs an explicit commitment; an ask needs
   an open loop; inbound needs substance, not reactions. Skip
   stakeholders entirely — Slack rarely introduces genuinely new
   external people and the false-positive rate is too high.

Multi-channel projects: each project can have N channels (e.g. a main
channel and a `_team` internal one). The digest paragraph is structured
with one labeled sub-paragraph per channel, separated by blank lines,
so Drew/Tony can see at a glance where each thread happened. Single-
channel projects render as a single paragraph with no channel label.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import yaml

from cp_engine.config import TenantConfig
from cp_engine.ingest import IngestPlanError, _validate_plan
from cp_engine.plan_from_transcript import (
    _load_project_context,
    _call_claude,
    _extract_yaml,
)
from cp_engine.slack import FetchedChannel

# Slack weeks tend to be much smaller than meeting transcripts (most
# active project channels are <50 messages/week). 30k chars per channel
# is generous; the total budget across channels is the same — we don't
# want a noisy team channel to blow out the prompt size.
_MAX_TOTAL_CHARS = 30_000


class SlackPlanError(Exception):
    """Raised when Claude fails to return a valid Slack-digest plan."""


@dataclass
class GeneratedSlackPlan:
    plan: dict
    raw_response: str
    project_code: str
    week: str
    channel_ids: tuple[str, ...]
    model: str


def generate_slack_plan(
    *,
    config: TenantConfig,
    project_code: str,
    week: str,
    channels: list[FetchedChannel] | tuple[FetchedChannel, ...],
    model: str = "claude-opus-4-7",
    api_key: str | None = None,
) -> GeneratedSlackPlan:
    """Read messages + project context, ask Claude for a digest plan, validate it.

    `channels` is a list of FetchedChannel — one per Slack channel
    associated with the project. For single-channel projects, pass a
    one-element list. Channels with zero messages are still passed
    through; the prompt names them and tells Claude to mention them as
    quiet weeks rather than omit them.
    """
    channels = list(channels)
    project_ctx = _load_project_context(config, project_code)
    formatted = _format_multi_channel(channels)
    prompt = _build_slack_prompt(
        channels_text=formatted,
        project_context=project_ctx,
        project_code=project_code,
        week=week,
        channels=channels,
        team=config.team,
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
    # Claude sometimes drops the field on otherwise-valid responses.
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
    # project IF there was any activity at all. Genuinely quiet weeks
    # (zero messages across all channels) are allowed to skip the digest;
    # the caller has already decided to invoke this code anyway, so a
    # missing digest in that case isn't a hard failure.
    digest_items = proj_block.get("slack_digest") or proj_block.get("record-slack-digest")
    total_msgs = sum(len(c.messages) for c in channels)
    if not digest_items and total_msgs > 0:
        raise SlackPlanError(
            "plan is missing the required `slack_digest` entry for "
            f"{project_code} (had {total_msgs} messages across "
            f"{len(channels)} channel(s) — should have produced a digest)"
        )

    return GeneratedSlackPlan(
        plan=plan,
        raw_response=response_text,
        project_code=project_code,
        week=week,
        channel_ids=tuple(c.channel_id for c in channels),
        model=model,
    )


def _format_multi_channel(channels: list[FetchedChannel]) -> str:
    """Render N channels worth of messages with per-channel headers.

    For each channel: `## Channel: #<name>` (or the raw ID if name is
    empty) followed by a numbered chronological list. A total-char
    budget is enforced across all channels combined to keep the prompt
    reasonable even when one channel is much chattier than the rest.
    """
    if not channels:
        return "(no channels)"

    blocks: list[str] = []
    total = 0
    for c in channels:
        label = f"#{c.channel_name}" if c.channel_name else c.channel_id
        header = f"## Channel: {label} ({c.channel_id}) · {len(c.messages)} messages"
        if not c.messages:
            block = f"{header}\n(no messages this week)"
        else:
            lines = [header]
            for i, m in enumerate(c.messages, start=1):
                line = f"{i}. [{m.iso} · {m.user_name}] {m.text}"
                if total + len(line) > _MAX_TOTAL_CHARS:
                    lines.append(
                        f"... ({len(c.messages) - i + 1} more messages truncated) ..."
                    )
                    break
                lines.append(line)
                total += len(line) + 1
            block = "\n".join(lines)
        blocks.append(block)
        total += len(header)
    return "\n\n".join(blocks)


def _build_slack_prompt(
    *,
    channels_text: str,
    project_context: str,
    project_code: str,
    week: str,
    channels: list[FetchedChannel],
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
    channel_summary = "\n".join(
        f"- {('#' + c.channel_name) if c.channel_name else c.channel_id} "
        f"({c.channel_id}) — {len(c.messages)} messages"
        for c in channels
    )
    digest_shape = _digest_shape_instructions(channels)
    synthetic_path = f"slack://{'+'.join(c.channel_id for c in channels)}/{week}"
    return _PROMPT_TEMPLATE.format(
        today=today,
        project_code=project_code,
        week=week,
        channel_summary=channel_summary,
        synthetic_path=synthetic_path,
        project_context=project_context,
        channels_text=channels_text,
        team_block=team_block,
        digest_shape=digest_shape,
    )


def _digest_shape_instructions(channels: list[FetchedChannel]) -> str:
    """Tell Claude how to structure the digest's `text` field.

    Single-channel: one paragraph, no labels.
    Multi-channel: one labeled paragraph per channel, separated by
    blank lines, in the same order as the channel list.
    """
    if len(channels) <= 1:
        return (
            "Single channel — produce ONE paragraph (60–120 words) "
            "describing the week's chatter. No channel label is needed."
        )
    labels = [
        f"**#{c.channel_name}**:" if c.channel_name else f"**{c.channel_id}**:"
        for c in channels
    ]
    label_list = "\n".join(f"   - `{lab} <paragraph>`" for lab in labels)
    return (
        f"This project has {len(channels)} channels. The `text` field must "
        f"contain ONE paragraph PER channel, in this order:\n"
        f"{label_list}\n"
        "Format: each channel's section starts with its bold-label "
        "(e.g. `**#ibx_5167_ddi_platform_video_team**:`), followed by a "
        "40–90 word paragraph. Separate channels with a blank line. "
        "If a channel was quiet, write its label followed by `(quiet "
        "this week)` or a one-sentence note — do not omit the channel."
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

# Slack channels for this project
{channel_summary}

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
    slack_digest:                # ALWAYS emit exactly one entry.
      - text: "..."              # See "Digest shape" below for structure.
        week: "{week}"
    inbound:                     # OPTIONAL — only if HIGH confidence
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

# Digest shape

{digest_shape}

# Rules

1. **The `slack_digest` entry is REQUIRED.** Exactly one YAML entry,
   even for multi-channel projects (the `text` field carries the
   per-channel paragraphs internally).
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
   automated extraction.
4. **Don't duplicate what's already known.** If the project context
   already records a decision or ask, do NOT re-emit it as a verb.
   The digest can still mention it for color.
5. **Internal team members never become stakeholders.**
6. **Date fields:** use the message's ISO date (left of the `T`).
7. **No empty lists.** If a verb has no entries, omit it.

# Output format

Respond with ONLY the YAML plan inside a single ```yaml fenced code
block. No preamble, no explanation, no postscript.

# Slack messages (chronological, per channel)

{channels_text}
"""
