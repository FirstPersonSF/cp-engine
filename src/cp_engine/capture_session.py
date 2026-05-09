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
        commit_sha, pushed = _commit_and_push(
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
        )

    # Linked path: write into <working_dir>/sessions/.
    summary_path = _write_session_file(
        working_dir=working_dir,
        summary_text=summary_text,
        user=user,
        when=when,
    )
    cp_md_updated = _update_last_session_line(working_dir, when, user, summary_text)

    cp_tenant_root = _walk_to_cp_tenant_root(working_dir)
    if healed:
        # We rewrote .cp-link — the change lives in the source repo's tree
        # (.git/info/exclude already has it). It's not part of the cp commit.
        pass
    # Stage only the files this capture wrote: the new session file, and
    # the cp.md if its Last session line was rewritten. Pre-existing
    # uncommitted state in the tenant (e.g. a half-finished hand-edit on
    # another project's cp.md) stays out of the [session] commit.
    paths_to_commit: list[Path] = [summary_path]
    if cp_md_updated:
        paths_to_commit.append(working_dir / "cp.md")

    commit_sha, pushed = _commit_and_push(
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
    )


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

    if link_file.exists():
        target = Path(link_file.read_text(encoding="utf-8").strip())
        if target.exists():
            return target.resolve(), False, False
        # Self-heal: target is stale. Look for the cp tenant by walking up
        # the *recorded* (now-missing) path's ancestors. The closest existing
        # ancestor that contains `.cp-engine.toml` is the tenant root.
        tenant_root = _find_existing_tenant_root(target)
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


def _find_existing_tenant_root(start: Path) -> Path | None:
    """Walk up from `start` (which may not exist) looking for an ancestor
    that exists and contains `.cp-engine.toml`. Returns None if nothing
    found before the filesystem root.
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
    root = _find_existing_tenant_root(working_dir)
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
# or hand-written content all get overwritten. By design (last write wins).
_LAST_SESSION_RE = re.compile(r"^\*\*Last session:\*\*.*$", re.MULTILINE)


def _update_last_session_line(
    working_dir: Path, when: datetime, user: str, summary_text: str
) -> bool:
    """Rewrite the `**Last session:**` line in <working_dir>/cp.md.

    If cp.md doesn't exist or has no `**Last session:**` line, this is a
    no-op (returns False). The deepening pass and the slash command both
    write here, so we do NOT touch other Quick Resume lines.

    Returns True if cp.md was modified.
    """
    cp_md = working_dir / "cp.md"
    if not cp_md.exists():
        return False

    existing = cp_md.read_text(encoding="utf-8")
    if not _LAST_SESSION_RE.search(existing):
        return False

    one_line = _extract_one_liner(summary_text) or "session captured"
    timestamp = when.strftime("%Y-%m-%d %H:%M")
    replacement = f"**Last session:** _{timestamp} ({user}) — {one_line}_"

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
) -> tuple[str | None, bool]:
    """Stage the given paths in the cp tenant, commit, optionally push.

    `paths` must be a list of files this capture wrote or modified — and
    only those. Pre-existing uncommitted state in the tenant is left
    alone; a `[session]` commit must contain only session-driven changes.

    Returns (commit_sha, pushed). commit_sha is None when commit=False or
    when there was nothing to commit (idempotent re-run).
    """
    if not commit:
        return None, False

    if not paths:
        return None, False

    # Stage just our paths. Filter to ones that exist on disk (an updated
    # cp.md may not have been written if the regex didn't match).
    rel_paths = [str(p.relative_to(cp_tenant)) for p in paths if p.exists()]
    if not rel_paths:
        return None, False
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
        return None, False

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

    pushed = False
    if push:
        result = subprocess.run(
            ["git", "-C", str(cp_tenant), "push"],
            capture_output=True,
            text=True,
        )
        pushed = result.returncode == 0
    return sha, pushed
