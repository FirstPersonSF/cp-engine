"""`cp merge-check` — prove a merge didn't silently drop tracked content.

The tenant convention is to resolve generated files (sprint scaffolds,
`_sources.md`, spine mirrors) `--ours` on a conflict, because sync regenerates
them anyway. That is right *only when your side is genuinely newer*. When the
auto-ingest webhook has been writing to the remote while a session works
locally, `--ours` throws that content away — and a silent content drop looks
exactly like a clean resolution. Nothing errors; the merge commits; the bullets
are simply gone.

The cheap proof is a set-difference on `cp:hash` markers, the 8-hex content
hashes auto-ingest stamps on every bullet it writes. Enumerate the hashes
present on a reference commit's version of each file, then assert every one
still appears somewhere in the working tree. A hash that vanished is content
that vanished.

Real case (2026-08-19): merging 19 remote commits produced add/add conflicts
on 39 W35 scaffold files. Blanket `--ours` would have dropped seven
auto-ingest bullets — including an ESCALATED resourcing risk on storyos —
with no error and no visible sign.

Deliberately NOT limited to the files git reports as conflicted: a bad
`checkout --ours`, an over-eager `git restore`, or a hand-resolution that
truncated a section all lose content the same way, and all are caught by the
same check.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# The content-hash marker auto-ingest stamps on bullets it writes:
#   ... text <!-- cp:hash=8f3a1c2b -->
_HASH_RE = re.compile(r"cp:hash=(?P<hash>[0-9a-f]{8})")

# Files worth checking: markdown carrying ingest-written bullets. Binary and
# cache files never carry cp:hash markers, so scanning them is wasted work.
_CHECKED_SUFFIXES = (".md",)


@dataclass(frozen=True)
class LostContent:
    """One `cp:hash` present on the reference commit but missing from the tree.

    `path` is repo-relative. `snippet` is the bullet as the reference commit
    had it, trimmed for display — enough to recognize what would be lost and
    to recover it by hand if needed.
    """

    path: str
    hash: str
    snippet: str


def _git(args: list[str], cwd: Path) -> str:
    """Run a git command, returning stdout ('' on failure).

    Failures are swallowed deliberately: a file that doesn't exist on the
    reference commit (a genuinely new local file) is the normal case, not an
    error, and it has nothing to lose.
    """
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        return out.stdout if out.returncode == 0 else ""
    except OSError:
        return ""


def _hashes_with_context(body: str) -> dict[str, str]:
    """Map every cp:hash in `body` to the line carrying it."""
    found: dict[str, str] = {}
    for line in body.splitlines():
        for m in _HASH_RE.finditer(line):
            found.setdefault(m.group("hash"), line.strip())
    return found


def check_merge(
    repo_root: Path,
    ref: str = "ORIG_HEAD",
) -> tuple[list[LostContent], int]:
    """Compare `ref`'s cp:hash markers against the working tree.

    Returns (lost, files_checked). `ref` defaults to ORIG_HEAD, which git sets
    to the pre-merge HEAD — but the interesting comparison after resolving
    `--ours` is usually against the REMOTE side, so callers typically pass
    `origin/main` or `MERGE_HEAD`.

    A hash is considered present if it appears anywhere in the same file in
    the working tree; position and surrounding edits don't matter, only that
    the content survived. A file deleted locally but present on `ref` reports
    all of its hashes as lost, which is the correct reading — deleting a file
    full of ingest bullets is exactly the accident this catches.
    """
    listing = _git(["ls-tree", "-r", "--name-only", ref], repo_root)
    lost: list[LostContent] = []
    checked = 0

    for rel in listing.splitlines():
        rel = rel.strip()
        if not rel or not rel.endswith(_CHECKED_SUFFIXES):
            continue

        ref_body = _git(["show", f"{ref}:{rel}"], repo_root)
        if not ref_body:
            continue
        ref_hashes = _hashes_with_context(ref_body)
        if not ref_hashes:
            continue

        checked += 1
        local = repo_root / rel
        local_body = local.read_text(encoding="utf-8") if local.is_file() else ""
        local_hashes = set(_HASH_RE.findall(local_body))

        for h, snippet in ref_hashes.items():
            if h not in local_hashes:
                lost.append(
                    LostContent(
                        path=rel,
                        hash=h,
                        snippet=snippet[:160],
                    )
                )

    return lost, checked
