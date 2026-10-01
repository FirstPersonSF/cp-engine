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
from cp_engine.clock import tenant_now, tenant_today
from cp_engine.config import ConfigError, load


def _surface_logged_warnings(fn):
    """Print every cp_engine WARNING+ logged while the command ran (step 3).

    The planning/digest helpers degrade per project to `logger.warning`, and
    outside `cxp sync` no handler is installed — so a prep doc missing three
    projects' milestones printed exactly like a complete one. This collects
    those records for the command's duration and renders them to stderr when
    it finishes (including on SystemExit). Output and exit code are otherwise
    unchanged. Applied directly above `def`, under the click decorators.
    """
    import functools

    from cp_engine.loud import captured_warnings, print_warnings

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with captured_warnings() as collected:
            try:
                return fn(*args, **kwargs)
            finally:
                print_warnings(
                    collected.messages, count=collected.count,
                    label="warning(s) during this command — output may be incomplete",
                )

    return wrapper


def _alloc_read_failed(which: str, exc: Exception) -> None:
    click.echo(
        f"(WARNING: {which} allocations read failed — hours shown as absent, "
        f"not zero: {type(exc).__name__}: {exc})",
        err=True,
    )


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
    "By default, prep-agenda auto-runs `cxp sync` if master-cp's last sync "
    "is more than 10 minutes old, so the agenda never shows stale data.",
)
@_surface_logged_warnings
def prep_agenda_cmd(
    project_filter: str, out: Path | None, summary: bool, no_sync: bool
) -> None:
    """Render a sprint-planning agenda from current cp tenant state.

    Reads master-cp's project list, the cross-cutting decisions on account
    / program cp.md files and master-cp.md, current-week sprint files, and
    per-project cp.md Exec Summaries; cross-references everything into
    project-grouped blocks.

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
    from datetime import timedelta

    from cp_engine.agenda import build_agenda, build_agenda_summary, is_sync_stale
    from cp_engine.sync import _default_backend_factory, sync_tenant

    click.echo(
        "warning: 'cxp prep-agenda' is deprecated and will be removed in a future "
        "release. Use 'cxp prep-planning' instead.",
        err=True,
    )

    config = _cli._load_config_or_die()

    # v0.8.8.2: auto-sync if stale. Avoids the "agenda shows yesterday's
    # owner data" footgun. Opt-out via --no-sync for environments where
    # the network round-trip isn't acceptable (CI dry-runs, etc.).
    if not no_sync and is_sync_stale(config):
        click.echo("master-cp sync is stale; running cxp sync first…", err=True)
        try:
            sync_tenant(config)
        except Exception as exc:
            click.echo(f"sync failed (continuing with stale data): {exc}", err=True)

    backend = _default_backend_factory(config.sync.backend)
    projects = backend.read_projects(config)

    # Pull last-week allocations the same way master-cp does, so the agenda's
    # per-project "last sprint hours" line matches what's in master-cp.md.
    today = tenant_now().date()
    last_monday = today - timedelta(days=today.weekday() + 7)
    try:
        allocations = backend.read_allocations(config, last_monday.isoformat())
    except Exception as exc:  # noqa: BLE001 — degrade, but say so
        _alloc_read_failed("last-week", exc)
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
@_surface_logged_warnings
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
            "cxp prep-planning no longer renders the planning doc directly.\n"
            "Supported flows:\n"
            "  cxp prep-planning --bundle    # raw material for /cp-prep "
            "in-session synthesis\n"
            "  cxp prep-planning --summary   # JSON metrics\n"
            "The deprecated engine-rendered inventory is available behind "
            "--legacy-render (works with --out).",
            err=True,
        )
        sys.exit(2)
    from datetime import timedelta

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
    today = tenant_now().date()
    last_monday = today - timedelta(days=today.weekday() + 7)
    tenant_hours: dict[str, int] = {}
    try:
        allocations = backend.read_allocations(config, last_monday.isoformat())
    except Exception as exc:  # noqa: BLE001 — degrade, but say so
        _alloc_read_failed("last-week", exc)
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

    planning_monday = _planning_monday(tenant_now())
    planned_allocations = None
    try:
        planned_allocations = backend.read_allocations(
            config, planning_monday.isoformat()
        )
    except Exception as exc:  # noqa: BLE001 — degrade, but say so
        _alloc_read_failed("planning-week", exc)
        planned_allocations = None

    # MC-2 Supabase client (schedule milestones + commitments). Silent
    # degrade if creds aren't set — per-project blocks render empty-state
    # notes. ClickUp is out of the prep path entirely (commitments
    # consolidation, cp-engine #38): milestones come from MC-2 schedules,
    # asks from the commitments table, and sprint-ask dedupe happens inside
    # build_project_block via the shared cp_hash recipe.
    from cp_engine.prep_planning import _make_supabase_client
    supabase_client = _make_supabase_client(config)

    code_filter = tuple(c.strip() for c in project_filter.split(",") if c.strip()) or None

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
@_surface_logged_warnings
def attention_digest_cmd(post_to_slack: bool, recipient: str, today) -> None:
    """Print today's attention digest (past-due asks, escalated risks).

    Scans the current ISO-week sprint dir for past-due open asks and
    recently-escalated risks, then renders a Slack-flavored markdown
    digest. Default prints to stdout; `--post-to-slack` DMs the digest
    to each Slack user ID listed in `[attention_digest].recipients`.
    """

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

    today_date = today.date() if today else tenant_today()
    # Asks live in MC-2 (step 4a): hand the digest a client so it reads the
    # commitments table; it falls back to the sprint files without one.
    from cp_engine import mc2_db

    try:
        digest_client = mc2_db.get_client(config, required=False)
    except Exception:  # noqa: BLE001 — the digest still runs from the files
        digest_client = None
    digest = run_digest(config=config, today=today_date, client=digest_client)
    # Cross-project routing proposals (#88): pending rows from MC-2's
    # review gate ride the same digest. Best-effort — the digest must
    # still send when MC-2 is unreachable.
    digest["cross_project"] = _fetch_pending_cross_project()
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
        return mc2_db.fetch_clickup_task_id_map(client, hashes)
    except Exception as exc:  # noqa: BLE001 — digest send must not fail
        log.warning("clickup task-id lookup failed (degrading to no links): %s", exc)
        return {}


def _fetch_pending_cross_project() -> list[dict]:
    """Pending cross-project proposals from MC-2 (#88).

    Returns [] on any failure (Supabase missing, query error) — the
    digest must still send without the proposals section.
    """
    import logging

    log = logging.getLogger(__name__)
    try:
        from cp_engine import mc2_db
        from cp_engine.cross_project import list_pending

        client = mc2_db.get_client(required=False)
        if client is None:
            log.info("cross-project lookup skipped: SUPABASE env not set")
            return []
        return list_pending(client)
    except Exception as exc:  # noqa: BLE001 — digest send must not fail
        log.warning("cross-project lookup failed (degrading to none): %s", exc)
        return []




@click.command("commitments-sweep")
@click.argument("code", required=False)
@click.option("--all", "sweep_all", is_flag=True, help="Tenant-wide, grouped by project.")
@click.option("--undated", is_flag=True, help="Only rows with no due date.")
@click.option("--older-than", type=int, default=None, metavar="DAYS", help="Only rows at least DAYS old.")
@click.option("--stale", is_flag=True, help="Undated AND ≥14d old — the \"is this still real?\" bucket.")
@click.option(
    "--today",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Override today's date (YYYY-MM-DD). Useful for testing.",
)
def commitments_sweep_cmd(
    code: str | None, sweep_all: bool, undated: bool,
    older_than: int | None, stale: bool, today,
) -> None:
    """Surface stale/undated open commitments for review (read-only).

    One project (CODE) or tenant-wide (--all). Rows group by project and
    sort oldest-first with age, date status, source, and owner — the
    fields that drive a keep/close decision. Rows the dates-loop TTL
    (#136) is about to expire are marked. Closing stays a deliberate act
    via resolve_commitment; this just makes the decision cheap.
    """

    from cp_engine import mc2_db
    from cp_engine.commitments_sweep import render_sweep, sweep

    if bool(code) == sweep_all:
        click.echo("Error: pass a project CODE or --all (exactly one).", err=True)
        raise SystemExit(2)
    try:
        config = load(Path.cwd())
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    today_d = today.date() if today else tenant_today()
    client = mc2_db.get_client(config)
    try:
        groups = sweep(
            client,
            code=code,
            today=today_d,
            undated_only=undated,
            older_than=older_than,
            stale_only=stale,
        )
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    click.echo(render_sweep(groups, today=today_d))


@click.command("dates-loop")
@click.option(
    "--post",
    is_flag=True,
    help="Actually post to Slack and apply ratification write-backs. "
    "Default is a dry run: render everything, send nothing, change nothing.",
)
@click.option(
    "--window-days",
    type=int,
    default=None,
    help="Forward window in days (default: [dates_loop].window_days, 14).",
)
@click.option(
    "--today",
    type=click.DateTime(formats=["%Y-%m-%d"]),
    default=None,
    help="Override today's date (YYYY-MM-DD). Useful for testing.",
)
def dates_loop_cmd(post: bool, window_days: int | None, today) -> None:
    """Weekly Slack dates loop — what's coming due, per project channel.

    Renders one post per active project/initiative with open commitments
    or schedule milestones inside the window (due this week / next N days
    / needs a date / slipped), plus a tenant-wide partners rollup. With
    --post, sends each to its mapped Slack channel(s) and applies the
    ratification write-backs (posted_count bumps, proposed->agreed after
    two unchanged posts, slipped stamps). Dry run by default.
    """

    from cp_engine.dates_loop import run_dates_loop

    try:
        config = load(Path.cwd())
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    today_d = today.date() if today else tenant_today()
    result = run_dates_loop(
        config, today=today_d, post=post, window_days=window_days
    )

    for cpost in result.posts:
        sent = " [POSTED]" if cpost.posted else ""
        click.echo(f"--- {cpost.code} -> {', '.join(cpost.channel_ids)}{sent}")
        click.echo(cpost.text)
        click.echo("")
    if result.partners_text:
        dest = result.partners_channel or (
            "(no partners channel — set app_config key "
            "'dates_loop_partners_channel' in MC-2)"
        )
        sent = " [POSTED]" if result.partners_posted else ""
        click.echo(f"--- partners rollup -> {dest}{sent}")
        click.echo(result.partners_text)
        click.echo("")
    if result.skipped_no_channel:
        click.echo(
            "skipped (content but no Slack channel / slack disabled): "
            + ", ".join(result.skipped_no_channel)
        )
    if post:
        click.echo(
            f"ratification: {result.posted_count_bumped} bumped · "
            f"{result.agreed_promoted} promoted to agreed · "
            f"{result.slipped_stamped} stamped slipped"
        )
        if result.expired_stamped or result.expire_warned:
            click.echo(
                f"ttl (#136): {result.expired_stamped} expired · "
                f"{result.expire_warned} in the warn window"
            )
    if result.errors:
        for err in result.errors:
            click.echo(f"error: {err}", err=True)
        # Partial success is SUCCESS with loud errors (cp-engine #85): a
        # failed partners rollup after delivered per-project posts must not
        # fail the run — a rerun would double-post the delivered messages
        # and double-bump their ratification counters. Exit 1 only when
        # post mode delivered NOTHING.
        any_delivered = (
            any(p.posted for p in result.posts) or result.partners_posted
        )
        if post and not any_delivered:
            raise SystemExit(1)
        delivered = sum(1 for p in result.posts if p.posted) + (
            1 if result.partners_posted else 0
        )
        click.echo(
            f"partial success: {delivered} post(s) delivered, "
            f"{len(result.errors)} failed — see errors above.",
            err=True,
        )
    if not result.posts and not result.partners_text:
        click.echo("Nothing due in the window — no posts to send.")


@click.command("draft-summaries")
@click.argument("codes", nargs=-1)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Draft and check, print the drafts, write nothing.",
)
@click.option(
    "--model",
    default=None,
    help="LLM model (default: $CP_DRAFT_MODEL, else the engine's current "
    "Claude default).",
)
@click.option(
    "--report",
    "report_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Also write the per-workstream results as JSON to this path.",
)
@click.option(
    "--planning-morning-only",
    is_flag=True,
    help="Do nothing unless it is Monday before 10:00 tenant time (the "
    "scheduled run's window).",
)
def draft_summaries_cmd(
    codes: tuple[str, ...],
    dry_run: bool,
    model: str | None,
    report_path: Path | None,
    planning_morning_only: bool,
) -> None:
    """Machine-draft stale Exec Summaries from each workstream's own recent
    material (#251).

    In scope: every active workstream whose summary is 14+ days old or a
    partial refresh (or, with CODES, only those — still scope-checked).
    Writes Status, Where it stands, Next up and Blockers — never Objective —
    and marks the stamp `· drafted by cp`. A workstream any of whose fields
    fails the fidelity check gets NOTHING written, and is reported.
    """
    import json

    from cp_engine.exec_summary_draft import (
        all_attempts_errored,
        is_planning_morning,
        run_drafts,
    )

    now = tenant_now()
    if planning_morning_only and not is_planning_morning(now):
        click.echo(f"Not Monday morning tenant time ({now:%a %H:%M}) — nothing to do.")
        return

    config = _cli._load_config_or_die()
    results = run_drafts(config, now=now, codes=codes, model=model, apply=not dry_run)

    for r in results:
        click.echo(f"--- {r.code} [{r.reason}] {r.outcome}"
                   + (f" — {r.detail}" if r.detail else ""))
        if r.source_note:
            click.echo(f"    from {r.source_note}")
        if r.fields and (dry_run or r.outcome == "rejected"):
            for label, value in r.fields.items():
                if isinstance(value, str):
                    click.echo(f"    **{label}:** {value}")
                else:
                    click.echo(f"    **{label}:**")
                    for v in value:
                        click.echo(f"      - {v}")
        for c in r.checks:
            score = "—" if c.score is None else f"{c.score:.2f}"
            click.echo(f"    check {c.field}: {score} {'ok' if c.ok else 'FAIL'} ({c.reason})")

    tally: dict[str, int] = {}
    for r in results:
        tally[r.outcome] = tally.get(r.outcome, 0) + 1
    click.echo("summary: " + (", ".join(f"{v} {k}" for k, v in sorted(tally.items())) or "nothing in scope"))

    if report_path is not None:
        report_path.write_text(
            json.dumps({"results": [r.as_dict() for r in results], "tally": tally},
                       indent=2, default=str),
            encoding="utf-8",
        )
    # Exit non-zero only when drafting was attempted and every attempt
    # errored (a transport or credential failure) — a rejection is the
    # check doing its job, not a failure of the run.
    if all_attempts_errored(results):
        sys.exit(1)
