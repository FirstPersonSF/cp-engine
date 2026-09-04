"""Slack verbs: slack-channels, slack-fetch, slack-digest.

Split from cli.py (arch-phase-4, #33). Shared helpers stay in
cp_engine.cli and are called via the module attribute so test
monkeypatches of `cp_engine.cli.<helper>` keep working.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import click

import cp_engine.cli as _cli


@click.command("slack-channels")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format. Default 'table'.",
)
@click.option(
    "--active-only",
    is_flag=True,
    help="Filter to active rows (engagements: Deal|Open; initiatives: Active).",
)
@click.option(
    "--check",
    is_flag=True,
    help="Exit non-zero if any active row has enable_slack=true with no "
    "channel bound. For CI and the digest workflow — that state is "
    "unsatisfiable and silently skips the project every week.",
)
def slack_channels_cmd(output_format: str, active_only: bool, check: bool) -> None:
    """List active engagement projects + initiatives + their Slack channels.

    Debug command for the weekly Slack digest pipeline. Each row is one
    engagement (`projects` row, non-internal) or internal initiative
    (`initiatives` row) with: canonical code, channel ids, enable_slack
    flag, and status.

    Use this to spot rows that need a channel_id backfill in MC-2 before
    turning on the cron.

    `--check` turns that from something a human has to notice into
    something CI enforces. `enable_slack=true` with no bound channel is
    an unsatisfiable promise: the digest skips the project as
    `no_channels` every single week and says so only inside a merged
    count. Six initiatives and one engagement sat in that state for
    months (cp-engine #227) because nothing ever failed.
    """
    import json

    from cp_engine.slack import list_channel_map

    config = _cli._load_config_or_die()
    rows = list_channel_map(config)

    # Active = engagement (Deal|Open) OR initiative (Active).
    if active_only:
        rows = [r for r in rows if r.status in ("Deal", "Open", "Active")]

    if output_format == "json":
        click.echo(
            json.dumps(
                [
                    {
                        "code": r.code,
                        "name": r.name,
                        "company_code": r.company_code,
                        "status": r.status,
                        "enable_slack": r.enable_slack,
                        "channel_ids": list(r.channel_ids),
                        "primary_channel_id": r.primary_channel_id,
                        "primary_channel_name": r.primary_channel_name,
                    }
                    for r in rows
                ],
                indent=2,
            )
        )
        if check:
            _exit_if_unsatisfiable(rows)
        return

    # Table output.
    if not rows:
        click.echo("(no projects)")
        return

    code_w = max(len(r.code) for r in rows)
    name_w = min(40, max(len(r.name) for r in rows))
    headers = (
        f"{'CODE':<{code_w}}  {'STATUS':<7}  {'SLACK':<5}  "
        f"{'#CH':<3}  {'CHANNELS':<40}  NAME"
    )
    click.echo(headers)
    click.echo("-" * len(headers))
    n_mapped = 0
    n_unmapped = 0
    for r in rows:
        flag = "ON" if r.enable_slack else "OFF"
        n_ch = len(r.channel_ids)
        chans = ", ".join(r.channel_ids) if r.channel_ids else "(none)"
        chans_clip = chans if len(chans) <= 40 else chans[:39] + "…"
        name_clip = r.name if len(r.name) <= name_w else r.name[: name_w - 1] + "…"
        click.echo(
            f"{r.code:<{code_w}}  {r.status:<7}  {flag:<5}  "
            f"{n_ch:<3}  {chans_clip:<40}  {name_clip}"
        )
        if r.channel_ids:
            n_mapped += 1
        elif r.enable_slack:
            n_unmapped += 1
    click.echo()
    click.echo(f"{n_mapped} mapped, {n_unmapped} enable_slack=true with no channels")
    if check:
        _exit_if_unsatisfiable(rows)


def _exit_if_unsatisfiable(rows) -> None:
    """Exit 1 if any row promises a Slack read it cannot perform.

    `enable_slack=true` with zero bound channels is not a warning state,
    it is a broken one: the digest can never satisfy it, so the project
    is skipped every week and the skip is indistinguishable from a quiet
    channel in the commit body. Failing here is the cheap half of
    cp-engine #227 — the run rows record the state after the fact, this
    refuses to let it persist unnoticed.
    """
    bad = [r for r in rows if r.enable_slack and not r.channel_ids]
    if not bad:
        return
    click.echo(err=True)
    click.echo(
        f"ERROR: {len(bad)} row(s) have enable_slack=true with no channel bound.",
        err=True,
    )
    click.echo(
        "Each is skipped by the weekly digest as `no_channels`, every week.",
        err=True,
    )
    for r in sorted(bad, key=lambda x: x.code):
        click.echo(f"  - {r.code} ({r.status})", err=True)
    click.echo(err=True)
    click.echo(
        "Fix by binding a channel in project_integrations, or by setting "
        "enable_slack=false if the project has no Slack channel.",
        err=True,
    )
    sys.exit(1)


def _parse_iso_week(week: str) -> tuple[datetime, datetime]:
    """Parse `YYYY-W##` → (monday_00:00_UTC, next_monday_00:00_UTC)."""
    import re as _re
    from datetime import timedelta

    m = _re.fullmatch(r"(\d{4})-W(\d{1,2})", week)
    if not m:
        raise click.BadParameter(
            f"week must be 'YYYY-W##' (e.g. '2026-W19'), got {week!r}"
        )
    year = int(m.group(1))
    wk = int(m.group(2))
    # ISO week date: Monday is weekday 1.
    monday = datetime.fromisocalendar(year, wk, 1).replace(tzinfo=timezone.utc)
    return monday, monday + timedelta(days=7)


@click.command("slack-fetch")
@click.option(
    "--code",
    "project_code",
    required=True,
    help="Canonical project code (e.g. ggl-5168). One project per call.",
)
@click.option(
    "--week",
    required=True,
    help="ISO week, e.g. '2026-W19'. Pulls Monday 00:00 UTC through "
    "next Monday 00:00 UTC.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format. 'text' is human-readable; 'json' is for piping.",
)
def slack_fetch_cmd(project_code: str, week: str, output_format: str) -> None:
    """Pull one week of Slack messages for a single project.

    Resolves the project's `slack_channel_ids` from MC-2, then calls
    Slack's `conversations.history` for each channel and the ISO week.
    Filters bots, joins, reactions-only events; resolves user mentions.

    For multi-channel projects, prints one section per channel
    (separated by `# Channel: <id> (<name>)` headers).
    """
    import json

    from cp_engine.slack import (
        OUTCOME_EMPTY,
        OUTCOME_EXEC_ERROR,
        OUTCOME_NO_CHANNELS,
        OUTCOME_OK,
        OUTCOME_PLAN_ERROR,
        ChannelMapRow,
        DigestRunRow,
        SlackError,
        fetch_channels_with_outcomes,
        list_channel_map,
        load_slack_token,
        record_digest_runs,
    )

    config = _cli._load_config_or_die()
    monday, next_monday = _parse_iso_week(week)

    rows = list_channel_map(config)
    by_code: dict[str, ChannelMapRow] = {r.code: r for r in rows}
    row = by_code.get(project_code)
    if row is None:
        click.echo(f"Project {project_code!r} not found in MC-2.", err=True)
        sys.exit(2)
    if not row.channel_ids:
        click.echo(
            f"Project {project_code} has no Slack channels in MC-2 "
            "(use `cp slack-channels` to see the full map).",
            err=True,
        )
        sys.exit(2)

    try:
        token = load_slack_token(config)
        fetched = fetch_channels(
            token, row.channel_ids, monday, week_end=next_monday
        )
    except SlackError as exc:
        click.echo(f"Slack fetch failed: {exc}", err=True)
        sys.exit(1)

    if output_format == "json":
        click.echo(
            json.dumps(
                [
                    {
                        "channel_id": fc.channel_id,
                        "channel_name": fc.channel_name,
                        "messages": [
                            {"ts": m.ts, "iso": m.iso, "user_name": m.user_name, "text": m.text}
                            for m in fc.messages
                        ],
                    }
                    for fc in fetched
                ],
                indent=2,
            )
        )
        return

    total = sum(len(fc.messages) for fc in fetched)
    click.echo(
        f"# {project_code} · {week} · {len(fetched)} channel(s) · {total} message(s)\n"
    )
    for fc in fetched:
        name = f"#{fc.channel_name}" if fc.channel_name else "(unknown name)"
        click.echo(f"## Channel: {fc.channel_id} {name} · {len(fc.messages)} messages")
        for m in fc.messages:
            click.echo(f"[{m.iso} · {m.user_name}] {m.text}")
        click.echo()


@click.command("slack-digest")
@click.option(
    "--code",
    "project_code",
    default=None,
    help="Single project code (e.g. ggl-5168). Omit to iterate ALL active "
    "projects with `enable_slack=true` and a slack_channel_id.",
)
@click.option(
    "--week",
    required=True,
    help="ISO week, e.g. '2026-W19'. Pulls Monday 00:00 UTC through "
    "next Monday 00:00 UTC.",
)
@click.option(
    "--apply",
    is_flag=True,
    help="Execute the generated plan(s) against the tenant. Default is "
    "to print YAML to stdout only.",
)
@click.option(
    "--model",
    default="claude-opus-4-7",
    show_default=True,
    help="Anthropic model to use for plan generation.",
)
@click.option(
    "--skip-quiet/--include-quiet",
    default=True,
    help="When iterating multiple projects, skip channels with zero messages. "
    "Default: skip (quiet weeks don't get a 'no activity' bullet).",
)
def slack_digest_cmd(
    project_code: str | None,
    week: str,
    apply: bool,
    model: str,
    skip_quiet: bool,
) -> None:
    """Generate Slack-digest plan(s) for one week. Single project or all.

    Single-project mode (--code <code>): pulls the week's messages,
    generates one YAML plan, optionally applies it.

    Multi-project mode (no --code): iterates every active project with
    enable_slack=true and a slack_channel_id. Skips channels with zero
    messages by default. Produces one combined commit if --apply is set
    (P.4 phase — for now, applies each plan independently and prints a
    summary).
    """
    import json

    import yaml as _yaml

    from cp_engine.ingest import IngestPlanError, execute_plan
    from cp_engine.plan_from_slack import (
        SlackPlanError,
        generate_slack_plan,
    )
    from cp_engine.slack import (
        ChannelMapRow,
        SlackError,
        fetch_channels,
        list_channel_map,
        load_slack_token,
    )

    config = _cli._load_config_or_die()
    monday, next_monday = _parse_iso_week(week)

    rows = list_channel_map(config)
    by_code: dict[str, ChannelMapRow] = {r.code: r for r in rows}

    if project_code:
        if project_code not in by_code:
            click.echo(f"Project {project_code!r} not found in MC-2.", err=True)
            sys.exit(2)
        targets = [by_code[project_code]]
    else:
        # Active = engagement (Deal|Open) OR initiative (Active),
        # plus enable_slack=true and ≥1 channel.
        targets = [
            r for r in rows
            if r.status in ("Deal", "Open", "Active")
            and r.enable_slack
            and r.channel_ids
        ]
        if not targets:
            click.echo("No active projects with Slack channels set.", err=True)
            sys.exit(1)

    try:
        token = load_slack_token(config)
    except SlackError as exc:
        click.echo(f"Slack auth failed: {exc}", err=True)
        sys.exit(1)

    summary = {
        "week": week,
        "projects": [],
    }
    # cp-engine #227: one row per (project, channel) with the real outcome,
    # so "quiet week" stops being indistinguishable from "bot can't see it".
    run_rows: list[DigestRunRow] = []

    for row in targets:
        if not row.channel_ids:
            click.echo(
                f"# skip {row.code}: no Slack channels", err=True
            )
            summary["projects"].append({"code": row.code, "skipped": "no_channels"})
            run_rows.append(
                DigestRunRow(
                    week=week,
                    project_code=row.code,
                    outcome=OUTCOME_NO_CHANNELS,
                    error_detail="enable_slack=true but no channel bound in project_integrations",
                )
            )
            continue

        try:
            outcomes = fetch_channels_with_outcomes(
                token, row.channel_ids, monday, week_end=next_monday
            )
        except SlackError as exc:
            # Only credential failures reach here now — per-channel errors
            # come back as outcomes. An auth failure is fatal for every
            # remaining project, so stop rather than log it N times.
            click.echo(f"# {row.code}: fetch failed: {exc}", err=True)
            summary["projects"].append({"code": row.code, "skipped": f"fetch_error: {exc}"})
            record_digest_runs(config, run_rows)
            raise

        for oc in outcomes:
            run_rows.append(
                DigestRunRow(
                    week=week,
                    project_code=row.code,
                    channel_id=oc.channel_id,
                    channel_name=oc.channel_name,
                    outcome=oc.outcome,
                    message_count=len(oc.messages),
                    error_detail=oc.error_detail,
                )
            )
            if not oc.readable:
                click.echo(
                    f"# {row.code}: channel {oc.channel_id} unreadable "
                    f"({oc.outcome}) — {oc.error_detail}",
                    err=True,
                )

        # Only readable channels feed the plan; an unreadable one must not
        # be silently folded in as if it were quiet.
        fetched = [oc for oc in outcomes if oc.readable]
        total_msgs = sum(len(fc.messages) for fc in fetched)
        if total_msgs == 0 and skip_quiet:
            click.echo(
                f"# skip {row.code}: 0 messages across {len(fetched)} channel(s) (quiet week)",
                err=True,
            )
            summary["projects"].append({"code": row.code, "skipped": "quiet_week"})
            continue

        click.echo(
            f"# {row.code} · {total_msgs} messages across {len(fetched)} channel(s) — generating plan…",
            err=True,
        )
        try:
            result = generate_slack_plan(
                config=config,
                project_code=row.code,
                week=week,
                channels=fetched,
                model=model,
            )
        except SlackPlanError as exc:
            click.echo(f"# {row.code}: plan generation failed: {exc}", err=True)
            summary["projects"].append({"code": row.code, "skipped": f"plan_error: {exc}"})
            for rr in run_rows:
                if rr.project_code == row.code and rr.outcome == OUTCOME_OK:
                    rr.outcome = OUTCOME_PLAN_ERROR
                    rr.error_detail = str(exc)
            continue

        if not apply:
            yaml_output = _yaml.safe_dump(
                result.plan, sort_keys=False, allow_unicode=True
            )
            click.echo(f"# --- {row.code} ---")
            click.echo(yaml_output)
            summary["projects"].append(
                {
                    "code": row.code,
                    "messages": total_msgs,
                    "channels": len(fetched),
                }
            )
            continue

        try:
            exec_result = execute_plan(
                result.plan,
                tenant_root=config.root,
                today=datetime.now().date(),
                week_iso=week,
            )
        except IngestPlanError as exc:
            click.echo(f"# {row.code}: plan execution failed: {exc}", err=True)
            summary["projects"].append({"code": row.code, "skipped": f"exec_error: {exc}"})
            for rr in run_rows:
                if rr.project_code == row.code and rr.outcome == OUTCOME_OK:
                    rr.outcome = OUTCOME_EXEC_ERROR
                    rr.error_detail = str(exc)
            continue

        summary["projects"].append(
            {
                "code": row.code,
                "messages": total_msgs,
                "channels": len(fetched),
                "files_written": [str(p) for p in exec_result.files_written],
                "skipped_duplicate": exec_result.skipped_duplicate,
                "errors": exec_result.errors,
            }
        )

    # Bookkeeping last, and only when we actually ran the pipeline: a
    # dry run reports what WOULD happen and should not claim a run.
    if apply:
        written = record_digest_runs(config, run_rows)
        unreadable = [r for r in run_rows if r.outcome not in (OUTCOME_OK, OUTCOME_EMPTY)]
        summary["run_rows_written"] = written
        summary["unreadable_channels"] = [
            {
                "code": r.project_code,
                "channel_id": r.channel_id,
                "outcome": r.outcome,
                "detail": r.error_detail,
            }
            for r in unreadable
        ]
        if unreadable:
            click.echo(
                f"# {len(unreadable)} channel(s) could NOT be read this week "
                f"— see slack_digest_runs for detail",
                err=True,
            )
        click.echo(json.dumps(summary, indent=2))


