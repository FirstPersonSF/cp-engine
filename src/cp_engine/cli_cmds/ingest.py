"""Ingest-plan verbs: ingest, ingest-from-transcript.

Split from cli.py (arch-phase-4, #33). Shared helpers stay in
cp_engine.cli and are called via the module attribute so test
monkeypatches of `cp_engine.cli.<helper>` keep working.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import click

import cp_engine.cli as _cli


@click.command("ingest")
@click.option(
    "--plan",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="YAML plan file to execute.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Validate the plan and print what would happen, but don't write files.",
)
@click.option(
    "--week",
    "week_iso",
    default=None,
    metavar="YYYY-W##",
    help=(
        "Target sprint week override (e.g. 2026-W32). Default: derived "
        "from the plan's entry dates (the meeting date), falling back to "
        "today's calendar week when the plan carries no dates."
    ),
)
def ingest_cmd(plan: Path, dry_run: bool, week_iso: str | None) -> None:
    """Execute an ingest plan against the cp tenant.

    The plan is a YAML file produced by /cp-ingest (or hand-authored).
    Schema: {transcript: ..., projects: {<code>: {<verb>: [items]}}, themes: [...]}

    The target sprint week derives from the plan's entry dates (the
    meeting date) — never from the planning-week roll-forward, which
    routed early-week meetings ingested Wed–Sun into next week's dir
    (#156). Use --week to override explicitly.

    On success: writes bullets to the right sprint-file subsections; the
    plan is saved (separately, by the plugin) to sprints/<W##>/_ingest-log/
    for audit. Idempotent — re-running the same plan is a no-op via
    content-hash deduplication.
    """
    import json
    import re as _re

    import yaml

    from cp_engine.ingest import (
        IngestPlanError,
        _calendar_week_iso,
        execute_plan,
        plan_week_iso,
    )

    if week_iso is not None and not _re.fullmatch(r"\d{4}-W\d{2}", week_iso):
        click.echo(
            f"--week must look like 2026-W32 (got {week_iso!r})", err=True
        )
        sys.exit(1)

    plan_data = yaml.safe_load(plan.read_text(encoding="utf-8"))
    config = _cli._load_config_or_die()
    today = datetime.now().date()
    resolved_week = week_iso or plan_week_iso(plan_data) or _calendar_week_iso(today)

    if dry_run:
        # Just validate the plan; report what would happen.
        from cp_engine.ingest import _normalize_verb, _validate_plan
        try:
            _validate_plan(plan_data)
        except IngestPlanError as exc:
            click.echo(f"Plan validation failed: {exc}", err=True)
            sys.exit(1)
        projects = plan_data.get("projects") or {}
        themes = plan_data.get("themes") or []
        account_decisions = plan_data.get("account_decisions") or []
        summary = {
            "valid": True,
            "target_week": resolved_week,
            "projects_touched": list(projects.keys()),
            "verb_counts": {
                code: {_normalize_verb(v): len(items) for v, items in entries.items()}
                for code, entries in projects.items()
            },
            "themes_count": len(themes),
            "account_decisions_count": len(account_decisions),
        }
        click.echo(json.dumps(summary, indent=2))
        return

    try:
        result = execute_plan(
            plan_data,
            tenant_root=config.root,
            today=today,
            week_iso=resolved_week,
        )
    except IngestPlanError as exc:
        click.echo(f"Plan validation failed: {exc}", err=True)
        sys.exit(1)

    click.echo(json.dumps(result.to_dict(), indent=2))
    if result.errors:
        sys.exit(2)


@click.command("ingest-from-transcript")
@click.option(
    "--project",
    "project_code",
    required=True,
    help="Canonical project code (e.g. ggl-5168). One project per call.",
)
@click.option(
    "--transcript",
    "transcript_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the transcript file.",
)
@click.option(
    "--apply",
    is_flag=True,
    help="Execute the generated plan against the tenant. Default is to print YAML only.",
)
@click.option(
    "--model",
    default="claude-opus-4-7",
    show_default=True,
    help="Anthropic model to use for plan generation.",
)
@click.option(
    "--show-prompt",
    is_flag=True,
    help="Print the assembled prompt and exit without calling the API. For prompt-iteration debugging.",
)
def ingest_from_transcript_cmd(
    project_code: str,
    transcript_path: Path,
    apply: bool,
    model: str,
    show_prompt: bool,
) -> None:
    """Generate a cp ingest plan from a transcript via Claude (Phase C.1).

    By default prints the generated YAML to stdout for human review.
    With --apply, validates and executes the plan immediately.
    Requires ANTHROPIC_API_KEY in environment.
    """
    import json

    import yaml as _yaml

    from cp_engine.ingest import IngestPlanError, execute_plan
    from cp_engine.plan_from_transcript import (
        PlanGenerationError,
        _build_prompt,
        _load_project_context,
        _read_transcript,
        generate_plan,
    )

    config = _cli._load_config_or_die()

    if show_prompt:
        prompt = _build_prompt(
            transcript=_read_transcript(transcript_path),
            project_context=_load_project_context(config, project_code),
            project_code=project_code,
            transcript_path=transcript_path,
            team=config.team,
        )
        click.echo(prompt)
        return

    try:
        result = generate_plan(
            config=config,
            project_code=project_code,
            transcript_path=transcript_path,
            model=model,
        )
    except PlanGenerationError as exc:
        click.echo(f"Plan generation failed: {exc}", err=True)
        sys.exit(1)

    yaml_output = _yaml.safe_dump(result.plan, sort_keys=False, allow_unicode=True)

    if not apply:
        click.echo(yaml_output)
        return

    try:
        exec_result = execute_plan(
            result.plan, tenant_root=config.root, today=datetime.now().date()
        )
    except IngestPlanError as exc:
        click.echo(f"Plan execution failed: {exc}", err=True)
        sys.exit(1)

    click.echo(json.dumps(exec_result.to_dict(), indent=2))
    if exec_result.errors:
        sys.exit(2)




# ──────────────────────────────────────────────────────────────────────
#  rerun-failed-ingests (#194 recovery)
# ──────────────────────────────────────────────────────────────────────


@click.command("rerun-failed-ingests")
@click.option(
    "--since",
    default=None,
    help=(
        "Only replay runs whose MEETING date is on/after this YYYY-MM-DD. "
        "Defaults to 14 days ago — older bullets have generally been dealt "
        "with by hand and replaying them is noise, not recovery."
    ),
)
@click.option(
    "--exclude",
    multiple=True,
    help=(
        "Project code to skip (repeatable). Use for projects already "
        "hand-recovered: a replay re-asks the model, so it produces "
        "differently-worded near-duplicates that content-hash dedupe "
        "CANNOT catch."
    ),
)
@click.option(
    "--skip-meeting",
    multiple=True,
    help=(
        "Meeting id to skip (repeatable). The runs table cannot know a run "
        "was already recovered by hand or by an earlier partial pass, and a "
        "replay re-asks the model — so replaying one twice yields "
        "differently-worded duplicates, not a no-op."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be replayed and exit. No API calls, no writes.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Replay at most N runs (oldest meeting first). For a cautious first pass.",
)
def rerun_failed_ingests_cmd(
    since: str | None,
    exclude: tuple[str, ...],
    skip_meeting: tuple[str, ...],
    dry_run: bool,
    limit: int | None,
) -> None:
    """Replay auto-ingest runs whose plans were computed but never written.

    Recovers the #194 class: `execute_plan` built its sprint-file target
    from the plan's project key verbatim, so a plan keyed by SHORT CODE
    ("slt-5196") targeted a file that does not exist (the real one is
    "slt-5196-brand-campaign-26.md"). The plan was dropped AFTER the model
    computed it, and the commit path hardcoded status="success", so the
    runs table reads clean. These are the rows with status='success' and a
    non-empty `errors` array.

    `plan_summary` records only COUNTS, not content, so recovery means
    regenerating each plan from its transcript — one Anthropic call per
    run. Re-running is otherwise safe: `execute_plan`'s content-hash
    dedupe makes an unchanged bullet a no-op.

    ALWAYS run --dry-run first. Without it this writes to the tenant.
    """
    import json as _json
    from collections import Counter
    from datetime import date, timedelta
    from datetime import date as _date_cls

    from cp_engine.ingest import execute_plan
    from cp_engine.mc2_db import Tables, get_client
    from cp_engine.plan_from_transcript import PlanGenerationError, generate_plan

    config = _cli._load_config_or_die()
    cutoff = since or (date.today() - timedelta(days=14)).isoformat()
    excluded = {e.strip() for e in exclude if e.strip()}
    skipped_meetings = {m.strip() for m in skip_meeting if m.strip()}

    client = get_client(config=config)
    rows = (
        client.table(Tables.AUTO_INGEST_RUNS)
        .select("id,created_at,status,project_codes,meeting_id,plan_summary,errors")
        .order("created_at")
        .execute()
        .data
    ) or []
    # The #194 signature: recorded success, but carried errors.
    stranded = [r for r in rows if r.get("status") == "success" and r.get("errors")]
    if not stranded:
        click.echo("No stranded runs found — nothing to replay.")
        return

    meeting_ids = [r["meeting_id"] for r in stranded if r.get("meeting_id")]
    meetings = (
        client.table(Tables.FATHOM_MEETINGS)
        .select("id,title,meeting_date,transcript,action_items")
        .in_("id", meeting_ids)
        .execute()
        .data
    ) or []
    by_id = {m["id"]: m for m in meetings}

    # Filter on the MEETING date, not the run date: the run may be recent
    # while the content it carries is old, and it is the content's age that
    # decides whether replaying it is recovery or noise.
    candidates = []
    for r in stranded:
        meeting = by_id.get(r.get("meeting_id") or "")
        if not meeting:
            continue
        if (r.get("meeting_id") or "") in skipped_meetings:
            continue
        m_date = (meeting.get("meeting_date") or "")[:10]
        if m_date < cutoff:
            continue
        codes = [c for c in (r.get("project_codes") or []) if c not in excluded]
        if not codes:
            continue
        candidates.append((r, meeting, codes, m_date))

    candidates.sort(key=lambda t: t[3])
    if limit:
        candidates = candidates[:limit]

    def _bullets(run: dict, codes: list[str]) -> int:
        total = 0
        for code, block in (run.get("plan_summary") or {}).items():
            if code in codes and isinstance(block, dict):
                total += sum(v for v in block.values() if isinstance(v, int))
        return total

    per_project: Counter = Counter()
    for run, _m, codes, _d in candidates:
        for code in codes:
            per_project[code] += _bullets(run, [code])
    planned_bullets = sum(per_project.values())

    click.echo(f"Stranded runs in table:        {len(stranded)}")
    click.echo(f"Meeting date on/after {cutoff}: {len(candidates)} run(s)")
    if excluded:
        click.echo(f"Excluded projects:             {', '.join(sorted(excluded))}")
    if skipped_meetings:
        click.echo(f"Skipped meetings:              {len(skipped_meetings)}")
    click.echo(f"Bullets recorded in those plans: ~{planned_bullets}")
    click.echo("")
    for code, n in per_project.most_common():
        click.echo(f"  {n:5d}  {code}")
    click.echo("")
    for run, meeting, codes, m_date in candidates:
        title = str(meeting.get("title") or "")[:44]
        click.echo(
            f"  {m_date}  {','.join(codes):22s} {title:46s} "
            f"~{_bullets(run, codes)} bullets"
        )

    if dry_run:
        click.echo("")
        click.echo(
            "DRY RUN — nothing generated, nothing written. "
            "Counts come from each run's recorded plan_summary; a replay "
            "re-asks the model, so actual bullets will differ somewhat."
        )
        return

    click.echo("")
    click.echo(
        f"Replaying {len(candidates)} run(s) — one Anthropic call each. "
        "Writes to the tenant."
    )
    written, failed, dupes = 0, 0, 0
    for run, meeting, codes, m_date in candidates:
        transcript = _normalize_meeting_transcript(meeting.get("transcript"))
        if not transcript.strip():
            click.echo(f"  ! {m_date} {codes}: no transcript — skipped", err=True)
            failed += 1
            continue
        import tempfile

        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(transcript)
            tpath = Path(fh.name)
        try:
            for code in codes:
                try:
                    gen = generate_plan(
                        config=config,
                        project_code=code,
                        transcript_path=tpath,
                        action_items=meeting.get("action_items"),
                        # Anchor the model's dates to the MEETING, not the
                        # wall clock — this is the whole point of a replay.
                        today=m_date,
                    )
                except PlanGenerationError as exc:
                    click.echo(f"  ! {m_date} {code}: plan generation failed: {exc}", err=True)
                    failed += 1
                    continue
                # `today` is the MEETING date, not the wall clock. Undated
                # bullets fall back to it (_resolve_today_iso), so replaying
                # weeks later would otherwise stamp two-week-old asks,
                # decisions and risks with today's date — making recovered
                # content look brand new and corrupting every age-based
                # surface downstream (past-due asks, escalation windows,
                # carry-forward). The meeting date is when this content
                # actually happened.
                y, mth, dy = (int(x) for x in m_date.split("-")[:3])
                result = execute_plan(
                    gen.plan,
                    tenant_root=config.root,
                    today=_date_cls(y, mth, dy),
                    supabase=client,
                    meeting_id=run.get("meeting_id"),
                    week_iso=_week_iso_for(m_date),
                )
                dupes += result.skipped_duplicate
                if result.files_written:
                    written += len(result.files_written)
                    click.echo(
                        f"  ✓ {m_date} {code}: {len(result.files_written)} file(s), "
                        f"{result.skipped_duplicate} dup(s) skipped"
                    )
                for err in result.errors:
                    click.echo(f"  ! {m_date} {code}: {err}", err=True)
        finally:
            tpath.unlink(missing_ok=True)

    click.echo("")
    click.echo(f"Done. {written} file-write(s), {dupes} duplicate(s) skipped, {failed} failure(s).")
    click.echo("Review with `git diff` before committing.")


def _week_iso_for(meeting_date: str) -> str:
    """ISO sprint week for a meeting date — the week that OWNS the content.

    Deliberately explicit rather than letting `execute_plan` derive it:
    a replay runs weeks after the meeting, and the plan's entry dates are
    what should decide the target week, not today.
    """
    from datetime import date as _date

    y, m, d = (int(x) for x in meeting_date.split("-")[:3])
    iso = _date(y, m, d).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _normalize_meeting_transcript(field) -> str:
    """Fathom transcripts are a JSONB list of segments; flatten to text.

    Mirrors `webhook/pipeline.py::_normalize_transcript`. Kept local so the
    CLI does not import from the webhook package, which is not installed
    alongside the engine.
    """
    if field is None:
        return ""
    if isinstance(field, str):
        return field
    if isinstance(field, list):
        parts = []
        for seg in field:
            if isinstance(seg, str):
                parts.append(seg)
            elif isinstance(seg, dict):
                speaker = seg.get("speaker") or seg.get("name") or ""
                text = seg.get("text") or seg.get("transcript") or ""
                parts.append(f"{speaker}: {text}" if speaker else str(text))
        return "\n".join(p for p in parts if p.strip())
    return str(field)
