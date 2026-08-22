"""Local-link wiring for cp ↔ source repos (v0.4).

Two responsibilities:

1. **Discovery.** Given a cp tenant on disk, walk `<scope>/*/`
   looking for `_repo.md` files and extract the GitHub repo coordinate
   from each. Returns a map of `repo-name → cp-working-dir-path`. This
   is the source of truth for "which cp working dir corresponds to
   which source repo" — derived from the rendered filesystem, not from
   committed config (which lacks scope information) and not from MC-2
   (which would couple every link operation to the database).

2. **Linking.** For each entry in `[local-repos]`, write a `.cp-link`
   file inside the source repo containing the absolute path to its cp
   working dir, and append `.cp-link` to `.git/info/exclude` so the
   source repo doesn't accidentally commit the link. Idempotent —
   re-running with no changes is a no-op.

Errors fail loudly: a path in `[local-repos]` that isn't a git repo,
a git remote that doesn't match any cp working dir, etc.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cp_engine.config import TenantConfig

# Match the GitHub URL inside _repo.md (line: [org/repo](https://github.com/org/repo))
_REPO_URL_RE = re.compile(
    r"https://github\.com/(?P<org>[^/\s\)]+)/(?P<repo>[^/\s\)]+)"
)

# Match the source-repo's git remote (https or ssh). We only need the repo
# name segment; the org isn't checked because organizations get renamed.
_REMOTE_HTTPS_RE = re.compile(r"github\.com[:/](?P<org>[^/]+)/(?P<repo>[^/\s\.]+)")


# ──────────────────────────────────────────────────────────────────────
#  Errors
# ──────────────────────────────────────────────────────────────────────


class LinkLocalError(Exception):
    """Base class for link-local errors."""


class NotAGitRepo(LinkLocalError):
    """A path in [local-repos] isn't a git repository."""


class GitRemoteMismatch(LinkLocalError):
    """A configured local repo's git remote doesn't match its name."""


class NoMatchingCpWorkingDir(LinkLocalError):
    """No cp working dir found for a [local-repos] entry."""


# ──────────────────────────────────────────────────────────────────────
#  Discovery
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CpWorkingDir:
    """A cp working directory paired with the GitHub repo it represents."""

    repo_name: str  # bare repo name (e.g. "mc-2")
    github_org: str  # owner segment (e.g. "FirstPersonSF")
    path: Path  # absolute path to the working dir


def discover_cp_working_dirs(tenant_root: Path) -> tuple[CpWorkingDir, ...]:
    """Walk the cp tenant for working dirs that have a repo file.

    Two file shapes count as a link target:

    - **Singular `_repo.md`** — written by sync for repo-source projects
      (standalone repos like `firstpersonsf/cp-engine/_repo.md`). One
      file per working dir. This is the **canonical** working dir for
      that repo — it carries the project's `cp.md`, `sessions/`, etc.
    - **`_repo-<name>.md`** — written by sync for each linked repo on
      an engagement OR initiative (e.g. an engagement with two MC-2-
      linked repos has `_repo-ai-pipeline.md` + `_repo-events-calendar.md`
      in its dir; an initiative that references cp-engine has a
      `_repo-cp-engine.md` pointer). Treated as a **secondary
      reference** to a repo whose primary home may be a standalone
      `_repo.md` elsewhere in the tree.

    When the same `repo_name` appears as BOTH a singular `_repo.md`
    AND a `_repo-<name>.md` reference, the singular wins — that's
    the repo's working dir; the linked reference is decoration.
    When only the linked reference exists (no standalone working
    dir), it's surfaced as the working dir (initiative-only repos
    have no standalone home).

    Skips `inactive/` subdirs (and pre-v0.7.1 `archived/`) — those
    are not link targets. The first GitHub URL in each file is
    canonical.

    Returns a tuple with one entry per repo_name, sorted by
    repo_name for deterministic ordering.
    """
    # Two-pass: collect all candidates, then dedup by repo_name with
    # singular `_repo.md` winning over linked `_repo-<name>.md`. Tracking
    # the source-file kind locally (not on CpWorkingDir) keeps the public
    # type narrow — consumers only care about (repo_name, github_org, path).
    candidates: list[tuple[CpWorkingDir, bool]] = []  # (dir, is_singular)
    for repo_md in tenant_root.rglob("_repo*.md"):
        # `rglob("_repo*.md")` also matches names like `_repository.md`;
        # narrow to the two canonical shapes.
        if repo_md.name != "_repo.md" and not repo_md.name.startswith("_repo-"):
            continue
        if "inactive" in repo_md.parts or "archived" in repo_md.parts:
            continue
        text = repo_md.read_text(encoding="utf-8")
        match = _REPO_URL_RE.search(text)
        if not match:
            continue
        candidates.append((
            CpWorkingDir(
                repo_name=match["repo"],
                github_org=match["org"],
                path=repo_md.parent.resolve(),
            ),
            repo_md.name == "_repo.md",
        ))

    # Dedup: per repo_name, prefer the singular entry if one exists.
    # When two singulars or two linked-only entries exist (e.g. same
    # repo referenced from two initiative dirs — rare but possible),
    # pick the first one in sorted-path order so the result is
    # deterministic across runs.
    by_name: dict[str, CpWorkingDir] = {}
    name_has_singular: set[str] = set()
    # Iterate sorted-path order so ties break the same way every run.
    for cwd, is_singular in sorted(candidates, key=lambda c: str(c[0].path)):
        existing = by_name.get(cwd.repo_name)
        if existing is None:
            by_name[cwd.repo_name] = cwd
            if is_singular:
                name_has_singular.add(cwd.repo_name)
            continue
        # An entry for this name already exists. The new one wins only
        # if it's singular AND the existing one isn't.
        if is_singular and cwd.repo_name not in name_has_singular:
            by_name[cwd.repo_name] = cwd
            name_has_singular.add(cwd.repo_name)
    return tuple(sorted(by_name.values(), key=lambda d: d.repo_name))


def find_cp_working_dir_for_remote(
    tenant_root: Path, remote_url: str
) -> CpWorkingDir | None:
    """Self-healing helper: given a source-repo's git remote URL, find the
    matching cp working dir. Returns None if no match.
    """
    match = _REMOTE_HTTPS_RE.search(remote_url)
    if not match:
        return None
    repo_name = match["repo"]
    for wd in discover_cp_working_dirs(tenant_root):
        if wd.repo_name == repo_name:
            return wd
    return None


# ──────────────────────────────────────────────────────────────────────
#  Linking
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LinkResult:
    """Outcome of a single link-local operation on one source repo."""

    repo_name: str
    source_repo_path: Path
    cp_working_dir: Path
    link_file: Path
    wrote_link: bool  # True if .cp-link was created or updated
    excluded: bool  # True if .git/info/exclude was modified


def link_local(config: TenantConfig) -> tuple[LinkResult, ...]:
    """Write `.cp-link` files into each source repo named in [local-repos].

    Skips entries whose repo name has no matching cp working dir, raising
    `NoMatchingCpWorkingDir` listing all unmatched names — fail loud rather
    than write partial state.
    """
    if not config.local_repos:
        return ()

    working_dirs = {wd.repo_name: wd for wd in discover_cp_working_dirs(config.root)}

    unmatched = sorted(set(config.local_repos) - set(working_dirs))
    if unmatched:
        raise NoMatchingCpWorkingDir(
            f"No cp working dir found for: {', '.join(unmatched)}. "
            "Run `cxp sync` first so each project has a `_repo.md`, or remove "
            "the unknown entries from [local-repos]."
        )

    results: list[LinkResult] = []
    for repo_name, source_repo_path in config.local_repos.items():
        results.append(_link_one(repo_name, source_repo_path, working_dirs[repo_name]))
    return tuple(results)


def _link_one(
    repo_name: str, source_repo_path: Path, working_dir: CpWorkingDir
) -> LinkResult:
    git_dir = source_repo_path / ".git"
    if not git_dir.exists():
        raise NotAGitRepo(
            f"{source_repo_path} (configured as [local-repos].{repo_name!r}) "
            "is not a git repository (no .git directory)."
        )

    actual = _git_remote_repo_name(source_repo_path)
    if actual is not None and actual != repo_name:
        raise GitRemoteMismatch(
            f"[local-repos].{repo_name!r} points at {source_repo_path}, but "
            f"that repo's git remote is named {actual!r}. Check the path."
        )

    link_file = source_repo_path / ".cp-link"
    target = str(working_dir.path) + "\n"
    wrote_link = False
    if not link_file.exists() or link_file.read_text(encoding="utf-8") != target:
        link_file.write_text(target, encoding="utf-8")
        wrote_link = True

    excluded = _ensure_excluded(git_dir, ".cp-link")

    return LinkResult(
        repo_name=repo_name,
        source_repo_path=source_repo_path,
        cp_working_dir=working_dir.path,
        link_file=link_file,
        wrote_link=wrote_link,
        excluded=excluded,
    )


def _git_remote_repo_name(repo_path: Path) -> str | None:
    """Return the repo-name segment of `origin`'s URL, or None if unset."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_path), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    url = out.stdout.strip()
    if not url:
        return None
    match = _REMOTE_HTTPS_RE.search(url)
    if not match:
        return None
    return match["repo"]


def _ensure_excluded(git_dir: Path, entry: str) -> bool:
    """Append `entry` to `.git/info/exclude` if not already present.

    Returns True if the file was modified. Creates `info/` if needed.
    """
    info_dir = git_dir / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    exclude_file = info_dir / "exclude"
    existing = exclude_file.read_text(encoding="utf-8") if exclude_file.exists() else ""
    if any(line.strip() == entry for line in existing.splitlines()):
        return False
    if existing and not existing.endswith("\n"):
        existing += "\n"
    exclude_file.write_text(existing + entry + "\n", encoding="utf-8")
    return True
