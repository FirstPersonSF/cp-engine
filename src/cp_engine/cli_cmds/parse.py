"""Parsing/inspection verbs: parse-sprint, parse-transcript, list-active-projects.

Split from cli.py (arch-phase-4, #33). Shared helpers stay in
cp_engine.cli and are called via the module attribute so test
monkeypatches of `cp_engine.cli.<helper>` keep working.
"""

from __future__ import annotations

from pathlib import Path

import click

import cp_engine.cli as _cli


@click.command("parse-sprint")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--json", "as_json", is_flag=True, help="Emit JSON to stdout.")
def parse_sprint_cmd(path: Path, as_json: bool) -> None:
    """Parse a sprint file and emit its structured representation.

    Without `--json`, prints a one-line summary. With `--json`, emits the
    full `SprintFile` as JSON — useful for piping into `jq` or another
    consumer that wants to introspect a sprint's parsed state.
    """
    from cp_engine.sprints import parse_sprint_file, sprint_file_to_dict

    sf = parse_sprint_file(path)
    if as_json:
        import json

        click.echo(json.dumps(sprint_file_to_dict(sf), default=str, indent=2))
    else:
        click.echo(
            f"{sf.project_code} · {sf.week_iso} · "
            f"{len(sf.client_open_asks)} asks · {len(sf.risks)} risks"
        )


@click.command("parse-transcript")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--gap-threshold",
    type=int,
    default=2,
    help="Flag audio gaps >= this many minutes. Default 2.",
)
@click.option(
    "--codes",
    default="",
    help="Comma-separated project codes to scan for. Empty → no code scan.",
)
def parse_transcript_cmd(path: Path, gap_threshold: int, codes: str) -> None:
    """Audit a Fathom-style transcript. Emits JSON to stdout.

    Output schema:
      {speakers, duration_minutes, gaps[], mentioned_codes[], action_items[]}

    Used by /cp-ingest plugin to surface a confirmation prompt before
    writing anything. Catches transcripts with audio gaps, missing speakers,
    or no mentioned project codes — the W19 retro flagged these failure
    modes after several got into committed artifacts.
    """
    import json

    from cp_engine.ingest import parse_transcript

    code_list = tuple(c.strip() for c in codes.split(",") if c.strip())
    audit = parse_transcript(
        path, gap_threshold_minutes=gap_threshold, project_codes=code_list
    )
    click.echo(json.dumps(audit.to_dict(), indent=2))


@click.command("list-active-projects")
@click.option(
    "--scope",
    type=click.Choice(["1p", "fpsf", "canonic", "all"]),
    default="all",
    help="Filter by scope. Default 'all'.",
)
@click.option(
    "--company",
    default=None,
    help="Filter by company code (case-insensitive, e.g. 'GGL' or 'IBX'). "
    "Used by /cp-ingest --account <company> to find the projects a "
    "multi-project account meeting touches. Default: all companies.",
)
def list_active_projects_cmd(scope: str, company: str | None) -> None:
    """List active projects from MC-2 as JSON for transcript classification.

    Output: list of {code, name, company_code, company_name, owner, scope}.
    Used by /cp-ingest plugin to give Claude the candidate-projects list
    when classifying which projects a transcript touches.
    """
    import json

    from cp_engine.state import scope_for
    from cp_engine.status import is_active_status
    from cp_engine.sync import _default_backend_factory

    config = _cli._load_config_or_die()
    backend = _default_backend_factory(config.sync.backend)
    projects = backend.read_projects(config)

    company_lc = company.lower() if company else None

    out = []
    for p in projects:
        if p.source == "engagement":
            if not is_active_status(p.status) or p.is_internal:
                continue
        else:
            if p.status != "Active":
                continue
        proj_scope = scope_for(p.company_kind)
        if scope != "all" and proj_scope != scope:
            continue
        # Phase B.2: --company filter for /cp-ingest --account flow.
        if company_lc and (p.company_code or "").lower() != company_lc:
            continue
        out.append(
            {
                "code": p.code,
                "name": p.name,
                "company_code": p.company_code,
                "company_name": p.company_name,
                "owner": p.owner,
                "scope": proj_scope,
                "source": p.source,
            }
        )
    out.sort(key=lambda r: (r["scope"], r["code"]))
    click.echo(json.dumps(out, indent=2))


