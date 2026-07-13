"""Session capture (v0.4).

The Python half of `/cp-summarize`. Given a source repo + a prose summary,
writes the summary to the corresponding cp working dir (or the cp tenant's
exceptions/ dir for unlinked repos), updates the project's `cp.md` Last
session line, and commits + pushes.

Pulled out of the slash command markdown so the path-math, file-naming,
and cp.md edit logic is testable Python instead of fragile bash + Claude
prose. The slash command is a thin wrapper that drafts the summary, writes
it to a temp file, and shells out here.

The slash command is responsible for *prose*. This module is responsible
for *plumbing*.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cp_engine.config import enforce_engine_version_for_tenant
from cp_engine.link_local import find_cp_working_dir_for_remote


# ──────────────────────────────────────────────────────────────────────
#  Errors
# ──────────────────────────────────────────────────────────────────────


class CaptureSessionError(Exception):
    """Base class for capture-session errors."""


class SourceRepoNotAGitRepo(CaptureSessionError):
    """The supplied source-repo path isn't a git repository."""


class CpTenantInvalid(CaptureSessionError):
    """The supplied cp-tenant path isn't a cp tenant clone (no .cp-engine.toml)."""


class CpLinkUnresolvable(CaptureSessionError):
    """Source repo has no `.cp-link` and we couldn't resolve a cp working dir
    from its git remote either. The capture cannot proceed without knowing
    where to write."""


class WorkingDirNotInTenant(CaptureSessionError):
    """The supplied --working-dir path isn't inside any cp tenant clone (no
    `.cp-engine.toml` in any ancestor). content-only captures need to know
    which tenant repo to commit into."""


class PushFailed(CaptureSessionError):
    """`git push` failed for a reason auto-rebase couldn't recover from
    (network, auth, hook rejection, etc.). The session file and the
    [session] commit DID land locally; the user just needs to resolve
    whatever's blocking the push and rerun `git push` from the cp tenant."""

    def __init__(self, stderr: str) -> None:
        self.stderr = stderr
        super().__init__(
            "git push failed and auto-rebase didn't recover. The session "
            "file and commit are locally on the cp tenant; rerun "
            "`git push` from the cp tenant clone after resolving the "
            f"underlying issue:\n\n{stderr.strip()}"
        )


# ──────────────────────────────────────────────────────────────────────
#  Result type
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CaptureResult:
    """Outcome of a single capture-session invocation."""

    summary_path: Path  # absolute path of the written summary file
    cp_working_dir: Path | None  # None for exceptions captures
    is_exception: bool
    cp_md_updated: bool  # True if a cp.md Last session line was rewritten
    commit_sha: str | None  # None when --no-commit, or when nothing to commit
    pushed: bool
    push_rebased: bool = False  # True when first push was rejected and we rebased+retried
    extra_files_committed: tuple[Path, ...] = ()  # content-only mode: files beyond session+cp.md


# ──────────────────────────────────────────────────────────────────────
#  Public API
# ──────────────────────────────────────────────────────────────────────


def capture_session(
    *,
    source_repo: Path,
    summary_text: str,
    user: str,
    cp_tenant: Path | None = None,
    when: datetime | None = None,
    commit: bool = True,
    push: bool = True,
) -> CaptureResult:
    """Capture a session summary back to the cp tree.

    Resolves the cp working dir as follows:
      1. Read `<source-repo>/.cp-link` if present. If the path it points at
         exists, use it.
      2. If `.cp-link` is stale (path missing), re-resolve by walking the
         cp tenant tree (rooted at the original `.cp-link` target's tenant
         root) for a `_repo.md` matching the source-repo's git remote.
         Update `.cp-link` in place when the new dir is found.
      3. If `.cp-link` is missing AND `cp_tenant` is provided, attempt to
         match the source repo's git remote against the cp tenant. If found,
         we have a working dir (linked path that just hadn't been
         materialized as `.cp-link`). If not found, fall back to
         `<cp-tenant>/exceptions/` (the unlinked-repo path).
      4. If `.cp-link` is missing AND `cp_tenant` is None, raise
         `CpLinkUnresolvable` — we don't know where to write.
    """
    source_repo = source_repo.resolve()
    if not (source_repo / ".git").exists():
        raise SourceRepoNotAGitRepo(f"{source_repo} is not a git repository.")

    if cp_tenant is not None:
        cp_tenant = cp_tenant.resolve()
        if not (cp_tenant / ".cp-engine.toml").exists():
            raise CpTenantInvalid(
                f"{cp_tenant} is not a cp tenant clone (no .cp-engine.toml)."
            )

    when = (when or datetime.now()).replace(microsecond=0)
    working_dir, is_exception, healed = _resolve_destination(source_repo, cp_tenant)

    # Enforce the cp tenant's engine pin BEFORE writing anything. A stale
    # cp-engine binary running against a newer-pinned tenant would silently
    # produce wrong-format output (e.g. cp.md updates that don't match the
    # current template shape, missing splice regions, etc.) — fail loud
    # instead. The error message tells the user how to upgrade.
    tenant_root = (
        cp_tenant
        if working_dir is None
        else _walk_to_cp_tenant_root(working_dir)
    )
    enforce_engine_version_for_tenant(tenant_root)

    if working_dir is None:
        # Exception path: write to <cp_tenant>/exceptions/.
        assert cp_tenant is not None  # _resolve_destination guarantees this
        summary_path = _write_exception(
            cp_tenant=cp_tenant,
            source_repo=source_repo,
            summary_text=summary_text,
            user=user,
            when=when,
        )
        commit_sha, pushed, push_rebased = _commit_and_push(
            cp_tenant,
            [summary_path],
            source_repo.name,
            when,
            user,
            commit,
            push,
        )
        return CaptureResult(
            summary_path=summary_path,
            cp_working_dir=None,
            is_exception=True,
            cp_md_updated=False,
            commit_sha=commit_sha,
            pushed=pushed,
            push_rebased=push_rebased,
        )

    # Linked path: write into <working_dir>/sessions/. Source-repo mode —
    # the working dir's other contents (if any) are derived/managed by
    # the engine, not hand-written, so the [session] commit stays narrow
    # (just the session file + cp.md).
    return _write_to_working_dir(
        working_dir=working_dir,
        summary_text=summary_text,
        user=user,
        when=when,
        commit=commit,
        push=push,
        is_exception=is_exception,
        include_working_dir_contents=False,
    )


def capture_session_in_working_dir(
    *,
    working_dir: Path,
    summary_text: str,
    user: str,
    when: datetime | None = None,
    commit: bool = True,
    push: bool = True,
) -> CaptureResult:
    """Capture a session summary directly into a known cp working dir.

    Use this for content-only projects where there is no separate source
    repo — the working dir IS the place all the work lives. The cp tenant
    repo is found by walking up from `working_dir` for `.cp-engine.toml`.

    Differences from `capture_session()`:
      - No `.cp-link` resolution, no git-remote matching, no exceptions
        fallback. The caller already knows where to write.
      - No source-repo concept at all. Callers don't need to fabricate
        a fake source repo to satisfy the API.
      - Tenant root is derived from `working_dir`'s ancestors, not from
        a separate `--cp-tenant` arg.
    """
    working_dir = working_dir.resolve()
    if not working_dir.is_dir():
        raise WorkingDirNotInTenant(
            f"{working_dir} is not a directory."
        )
    try:
        cp_tenant_root = _walk_to_cp_tenant_root(working_dir)
    except CaptureSessionError:
        raise WorkingDirNotInTenant(
            f"{working_dir} is not inside a cp tenant clone "
            f"(no .cp-engine.toml in any ancestor)."
        ) from None

    when = (when or datetime.now()).replace(microsecond=0)
    enforce_engine_version_for_tenant(cp_tenant_root)

    # Content-only mode — the working dir IS where the project's
    # hand-written content lives (synthesis docs, transcripts, notes).
    # Sweep up everything trackable inside it (filtered by .gitignore,
    # so binaries/media are excluded automatically).
    return _write_to_working_dir(
        working_dir=working_dir,
        summary_text=summary_text,
        user=user,
        when=when,
        commit=commit,
        push=push,
        is_exception=False,
        include_working_dir_contents=True,
    )


def _write_to_working_dir(
    *,
    working_dir: Path,
    summary_text: str,
    user: str,
    when: datetime,
    commit: bool,
    push: bool,
    is_exception: bool,
    include_working_dir_contents: bool,
) -> CaptureResult:
    """Shared tail used by both `capture_session()` (after .cp-link
    resolution) and `capture_session_in_working_dir()` (skipping it).
    Writes the session file, updates cp.md, commits + pushes the
    tenant root.

    `include_working_dir_contents` controls commit scope:
      - False (source-repo mode): only the session file + cp.md.
      - True (content-only mode): everything tracked-or-trackable inside
        `working_dir/`. Binaries are auto-excluded by `.gitignore`.
    """
    summary_path = _write_session_file(
        working_dir=working_dir,
        summary_text=summary_text,
        user=user,
        when=when,
    )
    # Derived, not authored (cp-engine #81): the just-written session file
    # is on disk, so the derivation includes it — but if a NEWER capture
    # from a teammate already sits in sessions/, theirs correctly wins.
    cp_md_updated = refresh_last_session_line(working_dir)

    cp_tenant_root = _walk_to_cp_tenant_root(working_dir)
    # Stage just the files this capture wrote: the new session file, and
    # (if content-only mode) any other text content the user added during
    # the session. Pre-existing uncommitted state OUTSIDE the working dir
    # is left alone — a `[session]` commit must contain only one project's
    # changes.
    paths_to_commit: list[Path] = [summary_path]
    if cp_md_updated:
        paths_to_commit.append(working_dir / "cp.md")

    extras: list[Path] = []
    if include_working_dir_contents:
        all_extras = _trackable_paths_under(working_dir, cp_tenant_root)
        # De-dupe vs the session file + cp.md already in the list.
        already = {p.resolve() for p in paths_to_commit}
        for p in all_extras:
            if p.resolve() not in already:
                paths_to_commit.append(p)
                extras.append(p)

    commit_sha, pushed, push_rebased = _commit_and_push(
        cp_tenant_root,
        paths_to_commit,
        f"{working_dir.name}",
        when,
        user,
        commit,
        push,
    )
    return CaptureResult(
        summary_path=summary_path,
        cp_working_dir=working_dir,
        is_exception=is_exception,
        cp_md_updated=cp_md_updated,
        commit_sha=commit_sha,
        pushed=pushed,
        push_rebased=push_rebased,
        extra_files_committed=tuple(extras),
    )


def _trackable_paths_under(working_dir: Path, tenant_root: Path) -> list[Path]:
    """Enumerate every file under `working_dir` that git would track if
    it were `git add`-ed (respecting `.gitignore`).

    Uses `git ls-files --others --exclude-standard --modified --cached`
    scoped to the working dir. Returns absolute paths.

    Skipping rationale per category:
      - cached: already tracked, not changed → already in the index, no-op
        but harmless to re-add.
      - modified: tracked + changed → must be staged.
      - others (excluded-standard): untracked + not gitignored → new
        text content the user added during the session.
      - ignored (NOT --ignored): binaries, .DS_Store, .cp-engine.local.toml
        → never staged.
    """
    rel_wd = str(working_dir.relative_to(tenant_root))
    result = subprocess.run(
        [
            "git",
            "-C",
            str(tenant_root),
            "ls-files",
            "--others",
            "--exclude-standard",
            "--modified",
            "--",
            rel_wd,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [tenant_root / line for line in result.stdout.splitlines() if line]


# ──────────────────────────────────────────────────────────────────────
#  Resolution
# ──────────────────────────────────────────────────────────────────────


def _resolve_destination(
    source_repo: Path, cp_tenant: Path | None
) -> tuple[Path | None, bool, bool]:
    """Returns (working_dir, is_exception, healed_cp_link).

    working_dir is None when the destination is the exceptions path. In that
    case `cp_tenant` must be set.
    """
    link_file = source_repo / ".cp-link"

    # Guard: treat an empty/whitespace-only `.cp-link` as missing. Without
    # this, `Path("").exists()` is True (it resolves to `.`) and
    # `.resolve()` returns the caller's cwd — we'd silently capture into
    # a random repo and `git add` against it. Fall through to the
    # unlinked branch instead.
    if link_file.exists() and link_file.read_text(encoding="utf-8").strip():
        target = Path(link_file.read_text(encoding="utf-8").strip())
        if target.exists():
            return target.resolve(), False, False
        # Self-heal: target is stale. Look for the cp tenant by walking up
        # the *recorded* (now-missing) path's ancestors. The closest existing
        # ancestor that contains `.cp-engine.toml` is the tenant root.
        tenant_root = find_tenant_root(target)
        if tenant_root is None and cp_tenant is not None:
            tenant_root = cp_tenant
        if tenant_root is None:
            raise CpLinkUnresolvable(
                f".cp-link in {source_repo} points at {target}, which doesn't "
                "exist, and we couldn't locate the cp tenant root from there. "
                "Pass --cp-tenant explicitly."
            )
        remote_url = _git_remote_origin_url(source_repo)
        if remote_url is None:
            raise CpLinkUnresolvable(
                f"Stale .cp-link in {source_repo}, and no `origin` remote to "
                "self-heal from."
            )
        new_wd = find_cp_working_dir_for_remote(tenant_root, remote_url)
        if new_wd is None:
            raise CpLinkUnresolvable(
                f"Stale .cp-link in {source_repo}; couldn't find a cp working "
                f"dir matching git remote {remote_url}. The project may have "
                "been archived or removed."
            )
        # Update .cp-link in place — user never sees the stale state.
        link_file.write_text(str(new_wd.path) + "\n", encoding="utf-8")
        return new_wd.path, False, True

    # No .cp-link → unlinked. Need an explicit cp_tenant to know where the
    # exceptions/ dir even is.
    if cp_tenant is None:
        raise CpLinkUnresolvable(
            f"{source_repo} has no .cp-link and no --cp-tenant was provided. "
            "Either run `cp link-local` from your cp tenant clone or pass "
            "--cp-tenant pointing at it."
        )

    # The repo might still match a tracked project that just hasn't been
    # `cp link-local`-ed yet on this machine. Try to find its working dir
    # before falling back to exceptions.
    remote_url = _git_remote_origin_url(source_repo)
    if remote_url is not None:
        wd = find_cp_working_dir_for_remote(cp_tenant, remote_url)
        if wd is not None:
            return wd.path, False, False

    # Unlinked + no matching project → exceptions path.
    return None, True, False


def find_tenant_root(start: Path) -> Path | None:
    """Walk up from `start` (which may not exist) looking for an ancestor
    that exists and contains `.cp-engine.toml`. Returns None if nothing
    found before the filesystem root.

    Public because `cp_engine.mcp_server` also relies on this walk-up to
    resolve the tenant root from whatever subdir the MCP server was launched
    in — keep it stable.
    """
    current: Path | None = start
    while current is not None and str(current) not in ("/", current.anchor):
        if current.exists() and (current / ".cp-engine.toml").exists():
            return current.resolve()
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return None


def _walk_to_cp_tenant_root(working_dir: Path) -> Path:
    """Walk up from a cp working dir to its tenant root."""
    root = find_tenant_root(working_dir)
    if root is None:
        raise CpTenantInvalid(
            f"Couldn't locate cp tenant root from working dir {working_dir}."
        )
    return root


def _git_remote_origin_url(repo: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    url = out.stdout.strip()
    return url or None


# ──────────────────────────────────────────────────────────────────────
#  File writers
# ──────────────────────────────────────────────────────────────────────


def _write_session_file(
    *, working_dir: Path, summary_text: str, user: str, when: datetime
) -> Path:
    """Write the summary to <working_dir>/sessions/<filename>.md.

    Filename: `<YYYY-MM-DD>-<HHMM>-<user>.md`. On collision (same minute
    + user), append `-<n>` before the extension until unique.
    """
    sessions_dir = working_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    base = f"{when.strftime('%Y-%m-%d')}-{when.strftime('%H%M')}-{_slug_user(user)}"
    candidate = sessions_dir / f"{base}.md"
    counter = 2
    while candidate.exists():
        candidate = sessions_dir / f"{base}-{counter}.md"
        counter += 1

    candidate.write_text(summary_text, encoding="utf-8")
    return candidate


def _write_exception(
    *,
    cp_tenant: Path,
    source_repo: Path,
    summary_text: str,
    user: str,
    when: datetime,
) -> Path:
    """Write the summary to <cp_tenant>/exceptions/<filename>.md.

    Filename: `<YYYY-MM-DD>-<repo-name>-<HHMM>-<user>.md`.
    """
    exceptions_dir = cp_tenant / "exceptions"
    exceptions_dir.mkdir(parents=True, exist_ok=True)

    repo_name = source_repo.name
    base = (
        f"{when.strftime('%Y-%m-%d')}-{repo_name}-"
        f"{when.strftime('%H%M')}-{_slug_user(user)}"
    )
    candidate = exceptions_dir / f"{base}.md"
    counter = 2
    while candidate.exists():
        candidate = exceptions_dir / f"{base}-{counter}.md"
        counter += 1

    candidate.write_text(summary_text, encoding="utf-8")
    return candidate


_USER_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug_user(user: str) -> str:
    """Lowercase, alphanumeric-only, hyphen-separated. 'Drew Fiero' → 'drew-fiero'."""
    return _USER_SLUG_RE.sub("-", user.lower()).strip("-") or "user"


# ──────────────────────────────────────────────────────────────────────
#  cp.md Last session line
# ──────────────────────────────────────────────────────────────────────


# Match `**Last session:**` and everything after it on the same line.
# The whole line gets replaced — original placeholder, prior auto-edit,
# or hand-written content all get overwritten.
_LAST_SESSION_RE = re.compile(r"^\*\*Last session:\*\*.*$", re.MULTILINE)

# Session-file header: `## Session: <YYYY-MM-DD HH:MM>, <user>` (the
# /cp-summarize template). Carries the user with proper casing, which the
# lowercase filename slug loses.
_SESSION_HEADER_RE = re.compile(
    r"^##\s+Session:\s*(?P<stamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:?\d{2})\s*,\s*(?P<user>.+?)\s*$",
    re.MULTILINE,
)

# Session filename: `YYYY-MM-DD-HHMM-<user-slug>[-n].md` (see
# `_write_session_file`). The stamp prefix makes lexicographic order
# chronological order — the whole derivation leans on that.
_SESSION_FILENAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})-(?P<time>\d{4})-(?P<user>.+?)(?:-\d+)?$"
)


def derive_last_session_line(working_dir: Path) -> str | None:
    """Derive the `**Last session:**` line from the NEWEST file under
    <working_dir>/sessions/ (cp-engine #81).

    The session files are additive and never conflict; this line is a
    projection of them. Deriving (instead of letting each capture write
    its own prose) makes the line convergent: whatever a merge does to it,
    the next capture or `cp sync` recomputes the same value on every
    machine — two teammates capturing between pulls can no longer race.

    Newest = last in lexicographic filename order (the `YYYY-MM-DD-HHMM-`
    prefix sorts chronologically; collision suffixes `-2`, `-3` sort after
    their base). Timestamp + proper-cased user come from the file's
    `## Session:` header, falling back to the filename slug. Returns None
    when there are no session files.
    """
    sessions_dir = working_dir / "sessions"
    if not sessions_dir.is_dir():
        return None
    files = sorted(p for p in sessions_dir.glob("*.md") if p.is_file())
    if not files:
        return None
    newest = files[-1]
    try:
        text = newest.read_text(encoding="utf-8")
    except OSError:
        return None

    timestamp: str | None = None
    user: str | None = None
    header = _SESSION_HEADER_RE.search(text)
    if header:
        timestamp = header.group("stamp").replace("T", " ")
        user = header.group("user")
    else:
        m = _SESSION_FILENAME_RE.match(newest.stem)
        if m:
            hhmm = m.group("time")
            timestamp = f"{m.group('date')} {hhmm[:2]}:{hhmm[2:]}"
            user = m.group("user").replace("-", " ").title()
    if not timestamp or not user:
        return None

    one_line = _extract_one_liner(text) or "session captured"
    return f"**Last session:** _{timestamp} ({user}) — {one_line}_"


# First YYYY-MM-DD anywhere after the label — matches both capture-written
# lines (`_2026-07-13 10:15 (Drew) — …_`) and wrap-up-authored prose
# (`2026-07-11 (evening) — …`). ISO dates compare lexicographically.
_LINE_DATE_RE = re.compile(r"\*\*Last session:\*\*.*?(\d{4}-\d{2}-\d{2})")


def refresh_last_session_line(working_dir: Path) -> bool:
    """Recompute the `**Last session:**` line in <working_dir>/cp.md from
    the sessions/ directory and rewrite it in place — but only FORWARD.

    The line has two legitimate writers: session captures (which leave a
    file under sessions/) and wrap-ups (which author the line directly,
    with NO session file). "Newest session file" is therefore a floor,
    not the truth: a wrap-up-authored line dated on or after the newest
    capture must be preserved. The guard: replace only when the derived
    capture is STRICTLY newer (by date) than the date already on the
    line, or when the line carries no parseable date at all (scaffold
    placeholder, freeform prose — the convergent choice is the capture).

    No-op (False) when cp.md is missing, has no `**Last session:**` line,
    or the working dir has no session files. Only the FIRST matching line
    is touched — on project/initiative cp.mds that's the one inside the
    exec-summary region, on repo cp.mds the Quick Resume one.

    Called from capture (right after the session file is written, so the
    just-captured session is part of the derivation) and from `cp sync`
    (the convergence point: a mis-merged line self-heals on the next sync
    on any machine).
    """
    cp_md = working_dir / "cp.md"
    if not cp_md.exists():
        return False
    replacement = derive_last_session_line(working_dir)
    if replacement is None:
        return False

    existing = cp_md.read_text(encoding="utf-8")
    current = _LAST_SESSION_RE.search(existing)
    if not current:
        return False

    existing_date = _LINE_DATE_RE.search(current.group(0))
    derived_date = _LINE_DATE_RE.search(replacement)
    if existing_date and derived_date:
        # Same-day counts as current: a wrap-up and a capture on one day
        # are both truthful — don't churn between them.
        if existing_date.group(1) >= derived_date.group(1):
            return False

    updated = _LAST_SESSION_RE.sub(replacement, existing, count=1)
    if updated == existing:
        return False
    cp_md.write_text(updated, encoding="utf-8")
    return True


_HEADER_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def _extract_one_liner(summary_text: str) -> str | None:
    """Pull the first line of body text under '## What we did' (or the first
    section after the Session header). Truncate to 120 chars.

    The slash command's template puts the chunk we want under '## What we
    did'. If that header is missing, fall back to the first non-empty
    non-header line.
    """
    lines = summary_text.splitlines()

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

    # Fallback: first non-empty non-header line
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("---"):
            continue
        return _truncate(stripped)
    return None


def _truncate(s: str, limit: int = 120) -> str:
    s = s.strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


# ──────────────────────────────────────────────────────────────────────
#  Git commit + push
# ──────────────────────────────────────────────────────────────────────


def _commit_and_push(
    cp_tenant: Path,
    paths: list[Path],
    label: str,
    when: datetime,
    user: str,
    commit: bool,
    push: bool,
) -> tuple[str | None, bool, bool]:
    """Stage the given paths in the cp tenant, commit, optionally push.

    `paths` must be a list of files this capture wrote or modified — and
    only those. Pre-existing uncommitted state in the tenant is left
    alone; a `[session]` commit must contain only session-driven changes.

    On push rejection due to non-fast-forward (a `[cp-sync]` cron commit
    landed since we last pulled, which is the common case), this auto-
    rebases the local session commit onto the upstream tip and retries
    the push once. Other push failures (network, auth, hook rejection)
    raise `PushFailed`.

    Returns (commit_sha, pushed, push_rebased). commit_sha is None when
    commit=False or when there was nothing to commit (idempotent re-run).
    push_rebased is True when the first push was rejected and the retry
    after rebase succeeded.
    """
    if not commit:
        return None, False, False

    if not paths:
        return None, False, False

    # Stage just our paths. Filter to ones that exist on disk (an updated
    # cp.md may not have been written if the regex didn't match).
    rel_paths = [str(p.relative_to(cp_tenant)) for p in paths if p.exists()]
    if not rel_paths:
        return None, False, False
    subprocess.run(
        ["git", "-C", str(cp_tenant), "add", "--", *rel_paths], check=True
    )

    # Detect "nothing to commit" via diff --cached restricted to our
    # paths. We can't use plain `git status --porcelain` here because
    # pre-existing uncommitted state in the tenant would falsely signal
    # "yes, commit" — that's exactly the bug we're fixing.
    diff = subprocess.run(
        [
            "git",
            "-C",
            str(cp_tenant),
            "diff",
            "--cached",
            "--name-only",
            "--",
            *rel_paths,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    if not diff.stdout.strip():
        return None, False, False

    timestamp = when.strftime("%Y-%m-%d %H:%M")
    message = f"[session] {label}: {user} {timestamp}"
    subprocess.run(
        ["git", "-C", str(cp_tenant), "commit", "-m", message],
        check=True,
        capture_output=True,
    )

    sha_proc = subprocess.run(
        ["git", "-C", str(cp_tenant), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    sha = sha_proc.stdout.strip()

    if not push:
        return sha, False, False

    pushed, rebased = _push_with_retry(cp_tenant)
    return sha, pushed, rebased


# Patterns in `git push` stderr that indicate the push was rejected because
# the remote moved ahead of our local branch — recoverable via pull --rebase.
_NON_FAST_FORWARD_MARKERS = (
    "non-fast-forward",
    "(fetch first)",
    "Updates were rejected because the remote contains work",
    "Updates were rejected because the tip of your current branch is behind",
)


def _push_with_retry(cp_tenant: Path) -> tuple[bool, bool]:
    """Try `git push`. On non-fast-forward rejection, run `git pull
    --rebase` and try one more time. Returns (pushed, rebased).

    Raises `PushFailed` for any push failure not caught by the rebase
    recovery (network, auth, hook rejection, conflict during rebase).
    Doesn't raise for "no remote configured" — that's a legitimate
    --no-push-equivalent state for tests, and the caller already
    distinguishes pushed=False from pushed=True via the return value.
    """
    first = subprocess.run(
        ["git", "-C", str(cp_tenant), "push"],
        capture_output=True,
        text=True,
    )
    if first.returncode == 0:
        return True, False

    # No remote configured at all? Not a hard failure — the caller can
    # see pushed=False and decide what to surface. Older capture-session
    # tests rely on this (they don't wire up a remote).
    if "No configured push destination" in first.stderr or (
        "no upstream branch" in first.stderr
    ):
        return False, False

    if not any(marker in first.stderr for marker in _NON_FAST_FORWARD_MARKERS):
        # Different class of failure — auth, hook reject, network, etc.
        raise PushFailed(first.stderr)

    # Non-fast-forward: pull --rebase then push again.
    rebase = subprocess.run(
        ["git", "-C", str(cp_tenant), "pull", "--rebase"],
        capture_output=True,
        text=True,
    )
    if rebase.returncode != 0:
        # Rebase failed (likely a conflict on the same lines). Don't
        # leave the tenant in mid-rebase state — abort and surface the
        # original push failure.
        subprocess.run(
            ["git", "-C", str(cp_tenant), "rebase", "--abort"],
            capture_output=True,
            text=True,
        )
        raise PushFailed(
            f"Push rejected (non-fast-forward), and auto-rebase failed:\n"
            f"{rebase.stderr.strip()}"
        )

    second = subprocess.run(
        ["git", "-C", str(cp_tenant), "push"],
        capture_output=True,
        text=True,
    )
    if second.returncode != 0:
        raise PushFailed(second.stderr)
    return True, True
