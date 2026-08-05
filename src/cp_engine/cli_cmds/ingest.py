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


