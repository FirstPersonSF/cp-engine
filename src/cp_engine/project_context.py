"""Project-context activity rollup (v0.5.2).

Powers `/cp-context`: from a cp working dir, report what's happened on
the linked source repo + in this project's `sessions/` directory over
a recent window. The slash command shells out to the `cp
project-context` CLI; this module is the testable plumbing.

Two streams of activity, merged on one timeline:
  - **commits** in the linked source repo's local clone (`git log`)
  - **sessions** captured by `/cp-summarize` into `<wd>/sessions/`

Both streams are local-filesystem reads; no network, no MC-2.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from cp_engine.config import load as load_config


# ──────────────────────────────────────────────────────────────────────
#  Errors
# ──────────────────────────────────────────────────────────────────────


class ProjectContextError(Exception):
    """Base class for project-context errors."""


class NotAWorkingDir(ProjectContextError):
    """The supplied path isn't a cp working dir (no `_repo.md` and no
    `cp.md`)."""


class NoLocalCloneAvailable(ProjectContextError):
    """The cp tenant's `[local-repos.<user>]` map has no entry for this
    repo on any user known to this machine, OR the entries point at
    paths that don't exist locally. Without a clone, there's no git
    history to read."""


# ──────────────────────────────────────────────────────────────────────
#  Result types
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommitEntry:
    sha: str  # short
    when: datetime
    author: str
    subject: str


@dataclass(frozen=True)
class SessionEntry:
    when: datetime
    user: str
    path: Path  # absolute path to the session file
    one_liner: str | None  # extracted from the file's `## Session:` body


@dataclass(frozen=True)
class ContextResult:
    """One project's recent activity timeline."""

    working_dir: Path
    repo_name: str
    github_url: str | None
    local_clone: Path | None  # None when no clone is reachable on this machine
    window_days: int
    commits: tuple[CommitEntry, ...]  # newest first
    sessions: tuple[SessionEntry, ...]  # newest first


# ──────────────────────────────────────────────────────────────────────
#  Public API
# ──────────────────────────────────────────────────────────────────────


def project_context(
    *,
    working_dir: Path,
    user: str | None = None,
    days: int = 7,
    now: datetime | None = None,
) -> ContextResult:
    """Walk the cp working dir's linked clone for recent commits and the
    working dir's sessions/ for recent captures. Merge on one timeline.

    Args:
        working_dir: a cp working dir (e.g. `cp/firstpersonsf/mc-2/`).
        user: which `[local-repos.<user>]` entry to use for the clone path.
            If None, picks the first user whose configured path exists on
            this machine. Raises `NoLocalCloneAvailable` if nothing matches.
        days: how far back to look. Default 7 days.
        now: clock for the cutoff. Defaults to datetime.now().
    """
    working_dir = working_dir.resolve()
    repo_md = working_dir / "_repo.md"
    cp_md = working_dir / "cp.md"
    if not repo_md.exists() and not cp_md.exists():
        raise NotAWorkingDir(
            f"{working_dir} is not a cp working dir (no _repo.md or cp.md)."
        )

    when = now or datetime.now()
    cutoff = when - timedelta(days=days)

    repo_name, github_url = _parse_repo_md(repo_md) if repo_md.exists() else (
        working_dir.name,
        None,
    )

    local_clone = _resolve_local_clone(working_dir, repo_name, user=user)

    commits = _gather_commits(local_clone, cutoff) if local_clone else ()
    sessions = _gather_sessions(working_dir / "sessions", cutoff)

    return ContextResult(
        working_dir=working_dir,
        repo_name=repo_name,
        github_url=github_url,
        local_clone=local_clone,
        window_days=days,
        commits=commits,
        sessions=sessions,
    )


# ──────────────────────────────────────────────────────────────────────
#  _repo.md parsing
# ──────────────────────────────────────────────────────────────────────


_REPO_URL_RE = re.compile(
    r"https://github\.com/(?P<org>[^/\s\)]+)/(?P<repo>[^/\s\)]+)"
)


def _parse_repo_md(repo_md: Path) -> tuple[str, str | None]:
    """Return (repo_name, github_url). Falls back to the parent dir's name
    if no GitHub URL is in the file."""
    text = repo_md.read_text(encoding="utf-8")
    match = _REPO_URL_RE.search(text)
    if not match:
        return repo_md.parent.name, None
    repo = match["repo"]
    org = match["org"]
    return repo, f"https://github.com/{org}/{repo}"


# ──────────────────────────────────────────────────────────────────────
#  Local clone resolution
# ──────────────────────────────────────────────────────────────────────


def _resolve_local_clone(
    working_dir: Path, repo_name: str, *, user: str | None
) -> Path | None:
    """Find the linked source repo's local clone path on this machine.

    Reads the cp tenant's `.cp-engine.toml` `[local-repos.<user>]` map.
    If `user` is given, uses that user's entry (errors if missing). If
    `user` is None, picks the first user whose configured path actually
    exists on this machine — that's the heuristic for "we're running on
    Drew's machine, find Drew's clone without him having to specify."

    Returns None if no clone is reachable; caller treats that as "git
    history isn't available, sessions-only timeline."
    """
    tenant = _walk_to_tenant_root(working_dir)
    if tenant is None:
        return None
    config = load_config(tenant)
    by_user = config.local_repos_by_user

    if user is not None:
        paths = by_user.get(user) or {}
        raw = paths.get(repo_name)
        if not raw:
            raise NoLocalCloneAvailable(
                f"No `[local-repos.{user}].{repo_name!r}` entry in {tenant}/.cp-engine.toml. "
                "Add the path or omit --user to pick whichever user's path "
                "exists on this machine."
            )
        path = Path(raw).expanduser()
        return path.resolve() if path.exists() else None

    # No --user: try every user's entry and pick the first that exists.
    for _user, paths in by_user.items():
        raw = paths.get(repo_name)
        if not raw:
            continue
        path = Path(raw).expanduser()
        if path.exists():
            return path.resolve()
    return None


def _walk_to_tenant_root(start: Path) -> Path | None:
    """Walk up from `start` to the nearest ancestor with `.cp-engine.toml`."""
    current = start.resolve()
    while True:
        if (current / ".cp-engine.toml").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


# ──────────────────────────────────────────────────────────────────────
#  Git log gathering
# ──────────────────────────────────────────────────────────────────────


def _gather_commits(
    local_clone: Path, cutoff: datetime
) -> tuple[CommitEntry, ...]:
    """Run `git log` in the local clone, parse output into CommitEntry."""
    # `--since` is ISO-friendly. Use a strict format string so parsing is
    # robust regardless of git's locale.
    fmt = "%h%x09%cI%x09%an%x09%s"  # tab-separated: short-sha, ISO date, author, subject
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(local_clone),
            "log",
            f"--since={cutoff.isoformat()}",
            f"--pretty=format:{fmt}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # Not a git repo, or some other read failure. Treat as empty
        # rather than blow up — sessions-only timeline is still useful.
        return ()
    commits: list[CommitEntry] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        sha, iso, author, subject = parts
        try:
            when = datetime.fromisoformat(iso)
            # Strip tzinfo for consistent comparison with naive cutoff.
            if when.tzinfo is not None:
                when = when.replace(tzinfo=None)
        except ValueError:
            continue
        commits.append(CommitEntry(sha=sha, when=when, author=author, subject=subject))
    return tuple(commits)


# ──────────────────────────────────────────────────────────────────────
#  Sessions gathering
# ──────────────────────────────────────────────────────────────────────


# Filename: <YYYY-MM-DD>-<HHMM>-<user>(-N)?.md
_SESSION_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})-(?P<hhmm>\d{4})-(?P<user>[^/]+?)(?:-\d+)?\.md$"
)


def _gather_sessions(
    sessions_dir: Path, cutoff: datetime
) -> tuple[SessionEntry, ...]:
    if not sessions_dir.exists():
        return ()
    out: list[SessionEntry] = []
    for path in sessions_dir.iterdir():
        if path.is_dir() or not path.name.endswith(".md"):
            continue
        match = _SESSION_FILENAME_RE.match(path.name)
        if not match:
            continue
        try:
            when = datetime.strptime(
                f"{match['date']} {match['hhmm']}", "%Y-%m-%d %H%M"
            )
        except ValueError:
            continue
        if when < cutoff:
            continue
        user_slug = match["user"]
        user_display = user_slug.replace("-", " ").title()
        one_liner = _extract_session_one_liner(path)
        out.append(
            SessionEntry(
                when=when,
                user=user_display,
                path=path,
                one_liner=one_liner,
            )
        )
    out.sort(key=lambda e: e.when, reverse=True)
    return tuple(out)


def _extract_session_one_liner(path: Path) -> str | None:
    """Pull the first line under `### What we did` (or first body line if
    missing). Truncate to 120 chars."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    in_what = False
    for line in lines:
        if re.match(r"^#{2,3}\s+What we did\b", line, re.IGNORECASE):
            in_what = True
            continue
        if in_what:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                break
            return _truncate(stripped)
    # Fallback: first non-empty non-header non-frontmatter line
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("---"):
            continue
        return _truncate(stripped)
    return None


def _truncate(s: str, limit: int = 120) -> str:
    s = s.strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"
