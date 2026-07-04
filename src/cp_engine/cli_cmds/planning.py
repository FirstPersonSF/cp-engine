"""Planning verbs: prep-agenda, prep-planning, attention-digest.

Split from cli.py (arch-phase-4, #33). Shared helpers stay in
cp_engine.cli and are called via the module attribute so test
monkeypatches of `cp_engine.cli.<helper>` keep working.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

import cp_engine.cli as _cli
from cp_engine.config import ConfigError, load
from cp_engine.mc2_db import Tables


@click.command("prep-agenda")
@click.option(
    "--projects",
    "project_filter",
    default="",
    help="Comma-separated project codes to scope the agenda to. "
    "Empty (default) → full sprint planning across all active projects.",
)
@click.option(
    "--out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the agenda to this file path. Defaults to stdout.",
)
@click.option(
    "--summary",
    is_flag=True,
    help="Emit JSON metrics (workload by owner, coverage, urgency counts) "
    "instead of the rendered markdown. The /cp-prep plugin uses this.",
)
@click.option(
    "--no-sync",
    is_flag=True,
    help="Skip the automatic sync-if-stale check at the start. "
    "By default, prep-agenda auto-runs `cp sync` if master-cp's last sync "
    "is more than 10 minutes old, so the agenda never shows stale data.",
)
def prep_agenda_cmd(
    project_filter: str, out: Path | None, summary: bool, no_sync: bool
) -> None:
    """Render a sprint-planning agenda from current cp tenant state.

    Reads master-cp's project list, weekly-cp.md decisions, current-week
    sprint files, and per-project cp.md Quick Resume sections; cross-
    references everything into project-grouped blocks.

    Default: agenda for all active projects (full sprint planning).
    With `--projects <code>,<code>...`: scoped agenda for those projects only.

    Output is markdown. Default to stdout; pass `--out sprints/<W##>/_agenda.md`
    to overwrite the per-week agenda file. Pass `--summary` to emit JSON
    metrics instead (used by the /cp-prep plugin command).

    Deprecated as of v0.15.0 — superseded by ``cp prep-planning``, which
    produces a forward-looking, account-grouped sprint-planning doc with
    ClickUp-sourced milestones. ``cp prep-agenda`` will be removed in a
    future release.
    """
    import json
    from datetime import datetime, timedelta

    from cp_engine.agenda import build_agenda, build_agenda_summary, is_sync_stale
    from cp_engine.sync import _default_backend_factory, sync_tenant

    click.echo(
        "warning: 'cp prep-agenda' is deprecated and will be removed in a future "
        "release. Use 'cp prep-planning' instead.",
        err=True,
    )

    config = _cli._load_config_or_die()

    # v0.8.8.2: auto-sync if stale. Avoids the "agenda shows yesterday's
    # owner data" footgun. Opt-out via --no-sync for environments where
    # the network round-trip isn't acceptable (CI dry-runs, etc.).
    if not no_sync and is_sync_stale(config):
        click.echo("master-cp sync is stale; running cp sync first…", err=True)
        try:
            sync_tenant(config)
        except Exception as exc:
            click.echo(f"sync failed (continuing with stale data): {exc}", err=True)

    backend = _default_backend_factory(config.sync.backend)
    projects = backend.read_projects(config)

    # Pull last-week allocations the same way master-cp does, so the agenda's
    # per-project "last sprint hours" line matches what's in master-cp.md.
    today = datetime.now().date()
    last_monday = today - timedelta(days=today.weekday() + 7)
    try:
        allocations = backend.read_allocations(config, last_monday.isoformat())
    except Exception:
        allocations = None

    last_sprint_hours_by_project: dict[str, str] = {}
    if allocations and getattr(allocations, "by_project", None):
        for code, alloc in allocations.by_project.items():
            entries = [f"{e.person_name.split()[0]} {e.hours:g}h" for e in alloc.entries]
            if entries:
                last_sprint_hours_by_project[code] = ", ".join(entries)

    code_filter = tuple(c.strip() for c in project_filter.split(",") if c.strip()) or None

    if summary:
        s = build_agenda_summary(
            config,
            tuple(projects),
            today=today,
            project_filter=code_filter,
            last_sprint_hours_by_project=last_sprint_hours_by_project,
        )
        click.echo(json.dumps(s.to_dict(), indent=2))
        return

    agenda_md = build_agenda(
        config,
        tuple(projects),
        today=today,
        project_filter=code_filter,
        last_sprint_hours_by_project=last_sprint_hours_by_project,
    )

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(agenda_md)
        click.echo(f"wrote {out}")
    else:
        click.echo(agenda_md)


@click.command("prep-planning")
@click.option(
    "--projects",
    "project_filter",
    default="",
    help="Comma-separated project codes to scope the doc to. "
    "Empty (default) → all active projects across the tenant.",
)
@click.option(
    "--out",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write the planning doc to this file path. Defaults to stdout.",
)
@click.option(
    "--summary",
    is_flag=True,
    help="Emit a JSON summary (milestone counts, urgent counts, errors) "
    "instead of the rendered markdown. The /cp-prep-planning plugin uses this.",
)
@click.option(
    "--bundle",
    is_flag=True,
    help="Emit the structured per-project exec-summary + metrics bundle for "
    "in-session synthesis (the /cp-prep skill reads this) instead of the "
    "rendered doc.",
)
@click.option(
    "--legacy-render",
    is_flag=True,
    help="Render the DEPRECATED engine-formatted planning doc (the "
    "account-grouped inventory the --bundle synthesis flow replaced). "
    "Kept as an explicit escape hatch only; /cp-prep is the supported "
    "path.",
)
@click.option(
    "--sweep",
    is_flag=True,
    help="Attach a whole-project spine sweep synthesis to each active "
    "project's block (Project Spine). OPT-IN — makes one LLM call per "
    "project that has a backfilled spine/ dir, best-effort per project. "
    "Default (off) leaves the doc fast + free. Requires ANTHROPIC_API_KEY.",
)
@click.option(
    "--sweep-model",
    default="claude-opus-4-7",
    show_default=True,
    help="LLM model for the --sweep synthesis.",
)
def prep_planning_cmd(
    project_filter: str,
    out: Path | None,
    summary: bool,
    bundle: bool,
    legacy_render: bool,
    sweep: bool,
    sweep_model: str,
) -> None:
    """Emit sprint-planning raw material (--bundle) or metrics (--summary).

    The supported flows are ``--bundle`` (the structured exec-summary +
    metrics bundle that /cp-prep synthesizes into ``_planning.md``
    in-session) and ``--summary`` (JSON metrics). The old engine-rendered
    account-grouped doc is DEPRECATED — it's the ~426-line inventory the
    bundle flow replaced — and now requires an explicit
    ``--legacy-render``. A bare invocation exits non-zero with a pointer
    so habit can't silently regenerate the deprecated dump over
    ``_planning.md``.

    With ``--projects <code>,<code>``: scoped to those projects only.
    ``--out <path>`` persists the bundle or legacy doc; default stdout.
    """
    if not (summary or bundle or legacy_render):
        click.echo(
            "cp prep-planning no longer renders the planning doc directly.\n"
            "Supported flows:\n"
            "  cp prep-planning --bundle    # raw material for /cp-prep "
            "in-session synthesis\n"
            "  cp prep-planning --summary   # JSON metrics\n"
            "The deprecated engine-rendered inventory is available behind "
            "--legacy-render (works with --out).",
            err=True,
        )
        sys.exit(2)
    from datetime import datetime, timedelta

    from cp_engine.prep_planning import (
        render_planning_bundle_doc,
        render_planning_doc,
        render_planning_summary,
    )
    from cp_engine.sync import _default_backend_factory

    config = _cli._load_config_or_die()

    backend = _default_backend_factory(config.sync.backend)
    projects = backend.read_projects(config)

    # Pull last-week allocations the same way prep-agenda does so the tenant
    # strip stays consistent with the rest of cp's surfacing.
    today = datetime.now().date()
    last_monday = today - timedelta(days=today.weekday() + 7)
    tenant_hours: dict[str, int] = {}
    try:
        allocations = backend.read_allocations(config, last_monday.isoformat())
    except Exception:
        allocations = None
    if allocations is not None:
        rollup = getattr(allocations, "rollup", ()) or ()
        for entry in rollup:
            name = entry.person_name.split()[0] if entry.person_name else None
            if name:
                tenant_hours[name] = int(round(entry.total_hours))

    # PLANNING-week allocations (forward capacity, issue #16): what's
    # committed for the sprint being planned, not just last week's actuals.
    # Uses the same Mon/Tue-vs-Wed-Sun planning-week rule as week_iso.
    from cp_engine.sprints import _planning_monday

    planning_monday = _planning_monday(datetime.now())
    planned_allocations = None
    try:
        planned_allocations = backend.read_allocations(
            config, planning_monday.isoformat()
        )
    except Exception:
        planned_allocations = None

    # MC-2 Supabase client for clickup_list_id resolution. Silent degrade if
    # creds aren't set — per-project blocks will render "(ClickUp list not set)".
    from cp_engine.prep_planning import _make_supabase_client, _resolve_clickup_token
    supabase_client = _make_supabase_client(config)

    # ClickUp token: env first, then <mc-2 clone>/backend/.env (accepting both
    # CLICKUP_API_TOKEN and CLICKUP_API_KEY). Resolved here where config is
    # available — without it, a fresh shell resolves nothing and every
    # project's Forward Calendar renders empty.
    clickup_token = _resolve_clickup_token(config)

    code_filter = tuple(c.strip() for c in project_filter.split(",") if c.strip()) or None

    # Pre-fetch the cp_ask_hash → clickup_task_id map for bridging-period
    # dedupe (Open Commitments would otherwise render an ask once as a
    # ClickUp client-ask and once as a sprint-file fallback). Scan each
    # active project's current sprint file for hashes; degrade silently on
    # any failure since the dedupe is a polish, not load-bearing.
    clickup_task_ids: dict[str, str] = {}
    try:
        from cp_engine.prep_planning import (
            _parse_sprint_open_asks,
            filter_active,
        )
        from cp_engine.sprints import current_sprint_week_iso

        week_iso_for_dedupe = current_sprint_week_iso(
            datetime.combine(today, datetime.min.time())
        )
        wanted_codes = (
            {c.lower() for c in code_filter} if code_filter else None
        )
        all_hashes: list[str] = []
        for p in filter_active(projects):
            if wanted_codes is not None and p.code.lower() not in wanted_codes:
                continue
            sprint_path = (
                config.root / "sprints" / week_iso_for_dedupe / f"{p.code}.md"
            )
            for a in _parse_sprint_open_asks(sprint_path):
                if a["hash"]:
                    all_hashes.append(a["hash"])
        if all_hashes:
            clickup_task_ids = _fetch_clickup_task_ids_for_hashes(
                config, all_hashes
            )
    except Exception:  # noqa: BLE001 — dedupe must not break the doc
        clickup_task_ids = {}

    if summary:
        # Summary mode renders no per-project prose, so the sweep synthesis
        # has nowhere to land — skip it even if --sweep was passed (and don't
        # burn LLM calls). The markdown doc path below is the only consumer.
        out_str = render_planning_summary(
            config,
            tuple(projects),
            today=today,
            project_filter=code_filter,
            tenant_hours_last_week=tenant_hours,
            supabase_client=supabase_client,
            clickup_token=clickup_token,
            clickup_task_ids=clickup_task_ids,
            planned_allocations=planned_allocations,
        )
        click.echo(out_str)
        return

    # --bundle: emit the model-facing exec-summary + metrics dump. Placed
    # AFTER --summary so that path wins if both are passed (matching its
    # earlier position). Like --summary, the bundle ignores --sweep — it emits
    # raw material, not a swept synthesis. Respects --out / stdout exactly like
    # the default doc path below.
    if bundle:
        out_str = render_planning_bundle_doc(
            config,
            tuple(projects),
            today=today,
            project_filter=code_filter,
            tenant_hours_last_week=tenant_hours,
            supabase_client=supabase_client,
            clickup_token=clickup_token,
            clickup_task_ids=clickup_task_ids,
            planned_allocations=planned_allocations,
        )
        if out:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(out_str)
            click.echo(f"wrote {out}")
        else:
            click.echo(out_str)
        return

    # --sweep: build the real LLM wrapper and pass it to the renderer. Off by
    # default (sweep_llm=None → no spine load, no LLM call, fast path). The
    # per-project sweep is best-effort inside build_project_block, so a missing
    # ANTHROPIC_API_KEY logs a warning per project rather than aborting — note
    # it up front so the failure isn't a mystery.
    sweep_llm = None
    if sweep:
        import os

        if not os.environ.get("ANTHROPIC_API_KEY"):
            click.echo(
                "(note: --sweep needs ANTHROPIC_API_KEY; without it every "
                "project's sweep is skipped)",
                err=True,
            )
        from cp_engine.plan_from_transcript import _call_claude

        def sweep_llm(prompt: str) -> str:
            return _call_claude(prompt, model=sweep_model, api_key=None)

    doc = render_planning_doc(
        config,
        tuple(projects),
        today=today,
        project_filter=code_filter,
        tenant_hours_last_week=tenant_hours,
        supabase_client=supabase_client,
        clickup_token=clickup_token,
        clickup_task_ids=clickup_task_ids,
        sweep_llm=sweep_llm,
        planned_allocations=planned_allocations,
    )

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(doc)
        click.echo(f"wrote {out}")
    else:
        click.echo(doc)


@click.command("attention-digest")
@click.option(
    "--post-to-slack",
    is_flag=True,
    help="Post the digest as a Slack DM to configured recipients.",
)
@click.option(
    "--recipient",
    default="Drew",
    show_default=True,
    help="Recipient name used in the digest greeting.",
)
@click.option(
    "--today",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Override today's date (YYYY-MM-DD). Useful for testing.",
)
def attention_digest_cmd(post_to_slack: bool, recipient: str, today) -> None:
    """Print today's attention digest (past-due asks, escalated risks).

    Scans the current ISO-week sprint dir for past-due open asks and
    recently-escalated risks, then renders a Slack-flavored markdown
    digest. Default prints to stdout; `--post-to-slack` DMs the digest
    to each Slack user ID listed in `[attention_digest].recipients`.
    """
    from datetime import date as _date

    from cp_engine.attention_digest import (
        _post_digest_to_recipients,
        compose_digest,
    )
    from cp_engine.attention_digest import (
        attention_digest as run_digest,
    )

    try:
        config = load(Path.cwd())
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    today_date = today.date() if today else _date.today()
    digest = run_digest(config=config, today=today_date)
    markdown = compose_digest(digest, recipient_name=recipient, today=today_date)

    if post_to_slack:
        from cp_engine.slack import SlackError
        # Look up ClickUp task IDs for past-due asks so the Block Kit
        # renderer can emit "Open in ClickUp" link buttons instead of
        # Resolve/Snooze for asks already pushed via v0.12's pipeline.
        # Degrades to {} on any failure — digest send must not fail.
        clickup_task_ids: dict[str, str] = {}
        if digest.get("past_due"):
            hashes = [a.hash for a in digest["past_due"]]
            clickup_task_ids = _fetch_clickup_task_ids_for_hashes(config, hashes)
        try:
            timestamps = _post_digest_to_recipients(
                config=config,
                digest=digest,
                recipient_name=recipient,
                clickup_task_ids=clickup_task_ids,
            )
        except SlackError as exc:
            click.echo(f"Slack post failed: {exc}", err=True)
            sys.exit(1)
        click.echo(
            f"Posted digest to {len(timestamps)} recipient(s).", err=False
        )
        return

    click.echo(markdown.rstrip("\n"))


def _fetch_clickup_task_ids_for_hashes(config, hashes: list[str]) -> dict[str, str]:
    """Map each cp_ask_hash to its clickup_task_id (if any).

    Returns {} on any failure — Supabase missing, query error, network. The
    digest must still send when this helper degrades, because the
    Slack-button UX is a nice-to-have on top of the existing markdown digest.
    """
    import logging

    log = logging.getLogger(__name__)

    if not hashes:
        return {}
    try:
        from cp_engine import mc2_db

        client = mc2_db.get_client(required=False)
        if client is None:
            log.info("clickup task-id lookup skipped: SUPABASE env not set")
            return {}
        resp = (
            client.table(Tables.CLICKUP_TASK_PROPOSALS)
            .select("cp_ask_hash, clickup_task_id")
            .in_("cp_ask_hash", hashes)
            .not_.is_("clickup_task_id", "null")
            .execute()
        )
        return {
            row["cp_ask_hash"]: row["clickup_task_id"]
            for row in (resp.data or [])
            if row.get("clickup_task_id")
        }
    except Exception as exc:  # noqa: BLE001 — digest send must not fail
        log.warning("clickup task-id lookup failed (degrading to no links): %s", exc)
        return {}


