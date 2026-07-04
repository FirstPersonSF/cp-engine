"""Fathom meeting verbs: fathom-list, fathom-fetch, fathom-auto-poll, meetings-backfill.

Split from cli.py (arch-phase-4, #33). Shared helpers stay in
cp_engine.cli and are called via the module attribute so test
monkeypatches of `cp_engine.cli.<helper>` keep working.
"""

from __future__ import annotations

import sys

import click

import cp_engine.cli as _cli


@click.command("fathom-list")
@click.option(
    "--since",
    "since_iso",
    default=None,
    help="ISO timestamp; only return meetings newer than this. Default: most recent.",
)
@click.option("--limit", type=int, default=50, help="Max rows to return. Default 50.")
@click.option(
    "--type",
    "meeting_type",
    type=click.Choice([
        "project-status", "account-status", "sprint-planning",
        "work-session", "1-1", "untagged",
    ]),
    default=None,
    help="Filter by meeting_type (Phase B). Used by /cp-ingest --account to "
    "pull just account-status meetings. Default: no filter.",
)
def fathom_list_cmd(since_iso: str | None, limit: int, meeting_type: str | None) -> None:
    """List Fathom meetings from Supabase, newest first.

    Reads from `fathom_meetings` (the same Supabase project MC-2 uses;
    populated by the fathom-meeting-sync webhook). Lightweight rows —
    use `cp fathom-fetch <id>` to pull a full transcript.
    """
    import json

    from cp_engine.fathom import list_meetings

    config = _cli._load_config_or_die()
    meetings = list_meetings(
        config, since_iso=since_iso, limit=limit, meeting_type=meeting_type
    )
    click.echo(json.dumps([m.to_dict() for m in meetings], indent=2))


@click.command("fathom-fetch")
@click.argument("meeting_id")
@click.option(
    "--needs-review",
    is_flag=True,
    help="Stage to transcripts/needs-review/ instead of transcripts/incoming/. "
    "Used by auto-poll when the confidence gate trips.",
)
def fathom_fetch_cmd(meeting_id: str, needs_review: bool) -> None:
    """Fetch a Fathom meeting from Supabase and stage its transcript to disk.

    Default location: `transcripts/incoming/<meeting-id>.txt` at the
    tenant root. Pass `--needs-review` to stage to needs-review/ instead.

    Output is JSON: `{path, meeting_id, title, project_tags, ...}`.
    Hand the path to /cp-ingest to deepen.
    """
    import json

    from cp_engine.fathom import fetch_meeting, stage_transcript

    config = _cli._load_config_or_die()
    meeting = fetch_meeting(config, meeting_id)
    path = stage_transcript(meeting, tenant_root=config.root, needs_review=needs_review)
    click.echo(
        json.dumps(
            {
                "path": str(path),
                "meeting_id": meeting.id,
                "title": meeting.title,
                "meeting_date": meeting.meeting_date,
                "project_tags": meeting.project_tags,
                "meeting_type": meeting.meeting_type,
                "duration_minutes": meeting.duration_minutes,
                "needs_review": needs_review,
            },
            indent=2,
        )
    )


@click.command("fathom-auto-poll")
@click.option(
    "--limit",
    type=int,
    default=20,
    help="Max meetings to consider per poll. Default 20.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="List what would be staged + classified, but don't write files.",
)
def fathom_auto_poll_cmd(limit: int, dry_run: bool) -> None:
    """Poll Fathom Supabase for new meetings and stage them.

    Confidence gate: meetings with non-empty + non-`untagged` `project_tags`
    are staged to `transcripts/incoming/` for /cp-ingest. Meetings with no
    real tags go to `transcripts/needs-review/` for manual handling.

    State (last_polled_at + processed_ids) at `.cp-engine/state.json`
    (gitignored). Idempotent — already-processed ids are skipped.

    Output: JSON summary `{polled, staged_for_ingest[], staged_for_review[],
    skipped_already_processed[], errors[]}`.

    The `[fathom-ingest]` GitHub Actions workflow runs this on a cron and
    commits the staged files (not the deepening — /cp-ingest still requires
    /cp-ingest plugin orchestration which has Claude as the planner).
    """
    import json

    from cp_engine.fathom import (
        already_processed,
        fetch_meeting,
        has_good_tags,
        list_meetings,
        load_state,
        mark_processed,
        save_state,
        stage_transcript,
    )

    config = _cli._load_config_or_die()
    state = load_state(config.root)
    meetings = list_meetings(config, since_iso=state.last_polled_at, limit=limit)

    summary: dict = {
        "polled": len(meetings),
        "staged_for_ingest": [],
        "staged_for_review": [],
        "skipped_already_processed": [],
        "errors": [],
    }

    for m in meetings:
        if already_processed(state, m.id):
            summary["skipped_already_processed"].append(m.id)
            continue
        try:
            target = "ingest" if has_good_tags(m.project_tags) else "review"
            if dry_run:
                bucket = "staged_for_ingest" if target == "ingest" else "staged_for_review"
                summary[bucket].append({"id": m.id, "title": m.title, "tags": m.project_tags})
                continue
            full = fetch_meeting(config, m.id)
            path = stage_transcript(
                full,
                tenant_root=config.root,
                needs_review=(target == "review"),
            )
            bucket = "staged_for_ingest" if target == "ingest" else "staged_for_review"
            summary[bucket].append(
                {"id": m.id, "title": m.title, "path": str(path), "tags": m.project_tags}
            )
            state = mark_processed(state, m.id)
        except Exception as exc:
            summary["errors"].append({"id": m.id, "error": str(exc)})

    if not dry_run:
        save_state(state, config.root)

    click.echo(json.dumps(summary, indent=2))


# ──────────────────────────────────────────────────────────────────────
#  Slack weekly-digest commands
# ──────────────────────────────────────────────────────────────────────


@click.command(name="meetings-backfill")
@click.argument("code", required=False)
@click.option("--all", "all_", is_flag=True,
              help="Backfill every tagged meeting across all projects.")
def meetings_backfill_cmd(code: str | None, all_: bool) -> None:
    """Link + embed already-tagged Fathom meetings into the project/RAG world.

    `cp meetings-backfill ibx-5167` backfills one project's tagged meetings;
    `cp meetings-backfill --all` backfills every tagged meeting. Exactly one of
    CODE or --all is required. These meetings already exist as rows in MC-2 (no
    Fathom API call) — this runs `link_meeting` over them, embedding each
    meeting's SUMMARY. Exits non-zero if any row failed, so cron/CI notices.
    """
    from cp_engine import meetings as meetings_mod
    from cp_engine import sync_mc2

    if bool(code) == bool(all_):
        click.echo(
            "Error: pass exactly one of CODE or --all (got both or neither).",
            err=True,
        )
        sys.exit(2)

    config = _cli._load_config_or_die()
    # SUPABASE_* for the MC-2 client; OPENAI/VOYAGE for the embed pipeline
    # (without _load_ingest_creds the embed's OpenAI/Voyage client gets None —
    # the v0.40.2 fix).
    url, key = sync_mc2._load_supabase_creds(config)
    sync_mc2._load_ingest_creds(config)

    client = _cli.build_mc2_client()
    summary = meetings_mod.backfill_meetings(
        client, code=code or None, supabase_url=url, supabase_key=key
    )

    click.echo(
        f"meetings-backfill: total={summary['total']} "
        f"linked={summary['linked']} skipped={summary['skipped']} "
        f"failed={summary['failed']}"
    )
    unresolved = summary.get("unresolved") or []
    if unresolved:
        click.echo(f"  unresolved ({len(unresolved)}):", err=True)
        for item in unresolved:
            click.echo(f"    - {item}", err=True)
    failures = summary.get("failures") or []
    if failures:
        click.echo(f"  failures ({len(failures)}):", err=True)
        for rid, reason in failures:
            click.echo(f"    - {rid}: {reason}", err=True)
    if summary["failed"]:
        sys.exit(1)


