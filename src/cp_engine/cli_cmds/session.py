"""Session/repo verbs: capture-session, project-context, link-local.

Split from cli.py (arch-phase-4, #33). Shared helpers stay in
cp_engine.cli and are called via the module attribute so test
monkeypatches of `cp_engine.cli.<helper>` keep working.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import click

from cp_engine.config import ConfigError, load


@click.command(name="capture-session")
@click.argument("code", required=False)
@click.option(
    "--source-repo",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Source repo path (a code repo with a .cp-link). Mutually exclusive with --working-dir.",
)
@click.option(
    "--working-dir",
    "working_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help=(
        "cp working-dir path (for content-only projects with no separate "
        "source repo). Mutually exclusive with --source-repo. The cp tenant "
        "root is found by walking up for .cp-engine.toml, so --cp-tenant "
        "isn't needed."
    ),
)
@click.option(
    "--summary-file",
    "summary_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to a file containing the session summary (Claude writes this).",
)
@click.option(
    "--user",
    default=None,
    help="Display name for the session author (e.g. \"Drew\"). "
    "Defaults to the tenant roster's match for this machine or git "
    "identity, else `git config user.name`'s first name (#160, #325).",
)
@click.option(
    "--cp-tenant",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Path to the cp tenant clone. Required for unlinked source repos.",
)
@click.option("--no-commit", is_flag=True, help="Don't run git add/commit/push.")
@click.option("--no-push", is_flag=True, help="Commit but don't push.")
def capture_session_cmd(
    code: str | None,
    source_repo: Path | None,
    working_dir: Path | None,
    summary_file: Path,
    user: str | None,
    cp_tenant: Path | None,
    no_commit: bool,
    no_push: bool,
) -> None:
    """Write a session summary back to the cp tree.

    Three ways to say where:

    \b
    <code>  (positional, like exec-lint / spine-lint)
        Any workstream code (`ggl-5168`, `1pi-9005-mission-control`).
        Resolved to its working dir in the tenant found from cwd (or
        --cp-tenant), then captured as --working-dir would.

    \b
    --source-repo <path>  (default: cwd, when no <code> is given)
        For source-code repos with a .cp-link. Resolves destination via
        the link (self-healing if stale), or falls back to
        <cp-tenant>/exceptions/ for untracked repos.

    --working-dir <path>
        For content-only projects (1P engagements without a code repo).
        Skips source-repo resolution; writes directly to <path>/sessions/.
        The cp tenant root is found by walking up for .cp-engine.toml.

    Both modes write `<wd>/sessions/<YYYY-MM-DD>-<HHMM>-<user>.md`,
    update the project's `cp.md` Last session line, and commit + push.
    """
    from cp_engine.capture_session import (
        CaptureSessionError,
        capture_session,
        capture_session_in_working_dir,
    )

    from cp_engine.capture_session import default_session_user, find_tenant_root

    if sum(x is not None for x in (code, source_repo, working_dir)) > 1:
        click.echo(
            "Error: pass one of <code>, --source-repo or --working-dir.",
            err=True,
        )
        sys.exit(2)

    if code is not None:
        # #325: every other per-project verb takes the code positionally;
        # this one made you guess twice. The code names a working dir, so it
        # is --working-dir with the lookup done for you.
        from cp_engine.spine import SpineDirNotFound, find_spine_dir

        tenant = find_tenant_root((cp_tenant or Path.cwd()).resolve())
        if tenant is None:
            click.echo(
                f"Error: no cp tenant found from {cp_tenant or Path.cwd()} "
                "to resolve the code in — cd into the tenant or pass --cp-tenant.",
                err=True,
            )
            sys.exit(2)
        try:
            working_dir = find_spine_dir(tenant, code)
        except SpineDirNotFound as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(2)

    if user is None:
        # #160 gave --user a default; #325 makes it the tenant's name for
        # this person rather than whatever git's user.name happens to hold.
        user = default_session_user(_tenant_for_user(
            working_dir=working_dir, source_repo=source_repo, cp_tenant=cp_tenant,
        ))
        if not user:
            click.echo(
                "Error: --user not given, and neither the tenant roster nor "
                "`git config user.name` names you.",
                err=True,
            )
            sys.exit(2)

    summary_text = summary_file.read_text(encoding="utf-8")

    try:
        if working_dir is not None:
            result = capture_session_in_working_dir(
                working_dir=working_dir,
                summary_text=summary_text,
                user=user,
                commit=not no_commit,
                push=not (no_commit or no_push),
            )
        else:
            repo = (source_repo or Path.cwd()).resolve()
            result = capture_session(
                source_repo=repo,
                summary_text=summary_text,
                user=user,
                cp_tenant=cp_tenant,
                commit=not no_commit,
                push=not (no_commit or no_push),
            )
    except CaptureSessionError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if result.is_exception:
        click.echo(
            f"Logged exception (repo not tracked in this cp tenant): "
            f"{result.summary_path}"
        )
    else:
        click.echo(f"Wrote session summary: {result.summary_path}")
        if result.cp_md_updated:
            click.echo("Updated cp.md Last session line.")
        if result.extra_files_committed:
            click.echo(
                f"Also committed {len(result.extra_files_committed)} other file(s) "
                f"in the working dir (binaries excluded by .gitignore):"
            )
            wd = result.cp_working_dir
            for p in result.extra_files_committed:
                rel = p.relative_to(wd) if wd else p
                click.echo(f"  {rel}")
    if result.commit_sha:
        # Distinguish actually-skipped (--no-push or --no-commit) from
        # actually-pushed from rebased-then-pushed. Push *failures* now
        # raise PushFailed and are surfaced via the CaptureSessionError
        # branch above, so reaching here with pushed=False means the
        # caller asked us not to push.
        if result.push_rebased:
            click.echo(
                f"Committed {result.commit_sha}, rebased on top of upstream, "
                "and pushed."
            )
        elif result.pushed:
            click.echo(f"Committed {result.commit_sha} and pushed.")
        else:
            click.echo(f"Committed {result.commit_sha} (push skipped).")


def _tenant_for_user(
    *, working_dir: Path | None, source_repo: Path | None, cp_tenant: Path | None
) -> Path | None:
    """The tenant whose roster names the session author: the one the capture
    will write into, found the cheapest way each mode allows."""
    from cp_engine.capture_session import find_tenant_root

    if working_dir is not None:
        return find_tenant_root(working_dir.resolve())
    if cp_tenant is not None:
        return find_tenant_root(cp_tenant.resolve())
    repo = (source_repo or Path.cwd()).resolve()
    link = repo / ".cp-link"
    if link.is_file():
        target = link.read_text(encoding="utf-8").strip()
        if target:
            hit = find_tenant_root(Path(target).expanduser())
            if hit is not None:
                return hit
    return find_tenant_root(repo)


@click.command(name="project-context")
@click.option(
    "--working-dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="cp working dir. Defaults to cwd.",
)
@click.option("--user", help="Which `[local-repos.<user>]` entry to use.")
@click.option("--days", type=int, default=7, help="Lookback window. Default 7.")
def project_context_cmd(
    working_dir: Path | None,
    user: str | None,
    days: int,
) -> None:
    """Print recent activity for a project: git log + session captures.

    Run from inside a cp working dir. Reads the linked source repo's
    local clone path from `<tenant>/.cp-engine.toml [local-repos.<user>]`,
    runs `git log --since=<days> ago` there, lists session files in the
    working dir's `sessions/` directory from the same window, and prints
    one chronological timeline.
    """
    from cp_engine.project_context import ProjectContextError, project_context

    wd = (working_dir or Path.cwd()).resolve()
    try:
        result = project_context(working_dir=wd, user=user, days=days)
    except ProjectContextError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"# {result.repo_name} — last {result.window_days} days")
    if result.github_url:
        click.echo(f"GitHub: {result.github_url}")
    click.echo(f"Local clone: {result.local_clone or '(not on this machine)'}")
    click.echo()

    if not result.commits and not result.sessions:
        click.echo("(no activity in window)")
        return

    # Merge commits + sessions on one timeline. Each entry has a `.when`
    # attribute; sort newest first.
    timeline: list[tuple[datetime, str]] = []
    for c in result.commits:
        line = (
            f"  {c.when.strftime('%Y-%m-%d %H:%M')}  commit {c.sha}  "
            f"({c.author}) {c.subject}"
        )
        timeline.append((c.when, line))
    for s in result.sessions:
        one = s.one_liner or "(no summary)"
        line = (
            f"  {s.when.strftime('%Y-%m-%d %H:%M')}  session         "
            f"({s.user}) {one}"
        )
        timeline.append((s.when, line))
    timeline.sort(key=lambda t: t[0], reverse=True)
    for _when, line in timeline:
        click.echo(line)
    click.echo()
    click.echo(f"({len(result.commits)} commits, {len(result.sessions)} sessions)")


@click.command(name="link-local")
def link_local_cmd() -> None:
    """Wire `.cp-link` files into each source repo named in `[local-repos]`.

    Reads `.cp-engine.local.toml`'s `[local-repos]` table, validates each
    path is a git repo whose remote matches the entry, finds the
    corresponding cp working dir by walking the cp tenant tree, and writes
    `<source-repo>/.cp-link` containing the absolute path of that working
    dir. Also adds `.cp-link` to `.git/info/exclude` so the source repo
    won't accidentally commit it.

    Idempotent — re-running with no changes is a no-op.
    """
    try:
        config = load(Path.cwd())
    except ConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    from cp_engine.link_local import LinkLocalError
    from cp_engine.link_local import link_local as _run

    if not config.local_repos:
        click.echo(
            "No [local-repos] entries in .cp-engine.local.toml. Add one per "
            "repo you want linked, e.g.:\n\n"
            "  [local-repos]\n"
            '  "mc-2"      = "/Users/you/Documents/Python/mc-2"\n'
            '  "cp-engine" = "/Users/you/Documents/Python/context-protocol"\n'
        )
        return

    try:
        results = _run(config)
    except LinkLocalError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    wrote = sum(1 for r in results if r.wrote_link)
    excluded = sum(1 for r in results if r.excluded)
    for r in results:
        action = "wrote" if r.wrote_link else "ok   "
        click.echo(f"  {action} {r.source_repo_path}/.cp-link → {r.cp_working_dir}")
    click.echo(
        f"\n{len(results)} repo(s) linked. "
        f"{wrote} new/updated link file(s); "
        f"{excluded} .git/info/exclude update(s)."
    )


