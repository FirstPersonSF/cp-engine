"""Warn-only lint: a hand-kept `Provenance:` stamp older than the file (#353 item 5).

WHY THIS EXISTS. Every tenant markdown file opens with an anchor block whose
`Provenance: Version <NN> | <YYYY-MM-DD>` line says which revision the reader
holds. For engine-written files sync refreshes it (`sync._refresh_provenance_
header`, dotted engine versions like `0.131.11`). For hand-kept documents —
`Version 01`, `Version 02` — nobody does: on 2026-09-15 a budget doc still
read `Version 01` through six revisions, and a reader quoting "v01" was
quoting a document that no longer existed.

WHAT IS FLAGGED. A file whose anchor stamp carries a plain-integer version
(hand-kept) and whose last SUBSTANTIVE commit is dated after the stamp:

- Machine commits are not revisions: `[auto-ingest]`, `[cp-sync]`,
  `[render]`, `[draft-summary]`, `[spine-promote]`.
- A commit is substantive only when the file's text changed once engine-
  managed regions (`<!-- cp-engine:start … end -->`), HTML comments, the
  `Provenance:` line itself and all whitespace are set aside — so a sync
  splice, a re-wrap or a stamp-only bump never counts.
- The commit that CREATED the file is not a revision either: a document
  written on the 19th and committed on the 21st is still the 19th's.
- An uncommitted substantive edit counts as today (the wrap-up runs
  `cxp render --check` before it commits — the moment to bump the stamp).

Exempt: what the word-count discipline exempts (`meetings/`, `inactive/`,
`meeting-history.md`), the generated `spine/` mirror, and archives (any
path part containing `archive`) — append-only ledgers that rotation stamps
`Version 01` once and appends to.

WARN ONLY. `cxp render` echoes the findings and the exit code never changes.
Reads git only; edits nothing. Silent outside git or on a shallow clone,
whose history cannot answer.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# Hand-kept document version: `Version 01`. A dotted version (`0.131.11`) is
# the engine's own stamp, refreshed by sync, and out of scope.
_HAND_STAMP_RE = re.compile(
    r"^Provenance:\s*Version\s+(?P<version>\d+)\s*\|\s*(?P<date>\d{4}-\d{2}-\d{2})\s*$"
)
_ANY_PROVENANCE_RE = re.compile(r"^Provenance:.*$", re.MULTILINE)
# How far into a file the anchor block may sit.
_ANCHOR_LINES = 15

_REGION_RE = re.compile(
    r"<!-- cp-engine:start (?P<name>[\w-]+) -->.*?<!-- cp-engine:end (?P=name) -->",
    re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_WS_RE = re.compile(r"\s+")

#: Commit-subject prefixes the engine writes. Their edits are not revisions.
MACHINE_PREFIXES: tuple[str, ...] = (
    "[auto-ingest]", "[cp-sync]", "[render]", "[draft-summary]", "[spine-promote]",
)

_EXEMPT_PARTS = frozenset({"spine"})

# How many post-stamp candidate commits per file are diffed before giving up.
_MAX_CANDIDATES = 10


def substantive_text(body: str) -> str:
    """The text a human revision changes: engine regions, comments, every
    `Provenance:` line and whitespace removed. Two versions of a file differ
    substantively iff this differs."""
    body = _REGION_RE.sub("", body or "")
    body = _COMMENT_RE.sub("", body)
    body = _ANY_PROVENANCE_RE.sub("", body)
    return _WS_RE.sub(" ", body).strip()


def hand_stamp(text: str) -> tuple[str, date] | None:
    """(version, date) of the anchor block's hand-kept stamp, or None."""
    for line in (text or "").splitlines()[:_ANCHOR_LINES]:
        m = _HAND_STAMP_RE.match(line.strip())
        if m:
            try:
                return m.group("version"), date.fromisoformat(m.group("date"))
            except ValueError:
                return None
    return None


def is_machine_commit(subject: str) -> bool:
    return subject.lstrip().startswith(MACHINE_PREFIXES)


@dataclass(frozen=True)
class StaleStamp:
    path: str  # tenant-relative
    version: str
    stamp: date
    revised: date
    commit: str | None  # short sha, None for an uncommitted edit
    subject: str | None
    revisions: int  # substantive revisions after the stamp (capped)
    capped: bool

    def describe(self) -> str:
        subject = self.subject or ""
        if len(subject) > 60:
            subject = subject[:57].rstrip() + "…"
        where = (f'{self.commit} "{subject}"' if self.commit
                 else "uncommitted edit")
        n = f"{self.revisions}{'+' if self.capped else ''}"
        noun = "revision" if self.revisions == 1 and not self.capped else "revisions"
        return (
            f"{self.path}: ⚠ Provenance says Version {self.version} | "
            f"{self.stamp.isoformat()}, but the file has {n} substantive {noun} "
            f"since — latest {self.revised.isoformat()} ({where}). Bump the "
            "version and date"
        )


def _git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _show(root: Path, rev: str, rel: str) -> str | None:
    return _git(root, "show", f"{rev}:{rel}")


def _stamped_files(root: Path) -> dict[str, tuple[str, date]]:
    from cp_engine.tenant_walk import walk_tenant
    from cp_engine.word_count_lint import is_exempt

    out: dict[str, tuple[str, date]] = {}
    for path in walk_tenant(root, "*.md"):
        rel = path.relative_to(root)
        if is_exempt(rel) or _EXEMPT_PARTS.intersection(rel.parts):
            continue
        # Archives are append-only ledgers (rotation writes them with
        # `Version 01` and appends without a bump), like meeting-history.
        if any("archive" in part.lower() for part in rel.parts):
            continue
        try:
            with path.open(encoding="utf-8") as fh:
                head = "".join(next(fh, "") for _ in range(_ANCHOR_LINES))
        except (OSError, UnicodeDecodeError):
            continue
        found = hand_stamp(head)
        if found:
            out[rel.as_posix()] = found
    return out


def stale_stamps(root: Path, *, today: date | None = None) -> list[StaleStamp]:
    """Every hand-kept stamp in the tenant at `root` that predates the file's
    last substantive revision. Empty outside git or on a shallow clone."""
    root = Path(root)
    shallow = _git(root, "rev-parse", "--is-shallow-repository")
    if shallow is None or shallow.strip() == "true":
        return []
    stamped = _stamped_files(root)
    if not stamped:
        return []
    today = today or date.today()

    # One pass over the history since the oldest stamp: path -> commits,
    # newest first. `--relative` keeps paths tenant-relative from `root`.
    since = min(d for _, d in stamped.values())
    log = _git(root, "log", "--no-merges", "--relative", "--name-only",
               f"--since={since.isoformat()}T00:00:00",
               "--format=%x00%H%x09%h%x09%as%x09%s") or ""
    commits: dict[str, list[tuple[str, str, date, str]]] = {}
    for chunk in log.split("\x00")[1:]:
        lines = chunk.strip("\n").splitlines()
        if not lines:
            continue
        sha, short, day, subject = (lines[0].split("\t", 3) + ["", "", "", ""])[:4]
        try:
            when = date.fromisoformat(day)
        except ValueError:
            continue
        for rel in lines[1:]:
            rel = rel.strip()
            if rel in stamped:
                commits.setdefault(rel, []).append((sha, short, when, subject))

    # Tracked files with uncommitted edits, tenant-relative.
    changed = _git(root, "diff", "--name-only", "--relative", "HEAD") or ""
    dirty = {p.strip() for p in changed.splitlines() if p.strip() in stamped}

    findings: list[StaleStamp] = []
    for rel, (version, stamp) in sorted(stamped.items()):
        revisions: list[tuple[str | None, str | None, date]] = []
        if rel in dirty and stamp < today:
            head = _show(root, "HEAD", rel)
            try:
                now_text = (root / rel).read_text(encoding="utf-8")
            except OSError:
                now_text = None
            if head is not None and now_text is not None and \
                    substantive_text(head) != substantive_text(now_text):
                revisions.append((None, None, today))
        candidates = [c for c in commits.get(rel, ())
                      if c[2] > stamp and not is_machine_commit(c[3])]
        capped = len(candidates) > _MAX_CANDIDATES
        for sha, short, when, subject in candidates[:_MAX_CANDIDATES]:
            before = _show(root, f"{sha}^", rel)
            if before is None:
                continue  # the commit that created the file is not a revision
            after = _show(root, sha, rel)
            if after is None or substantive_text(before) == substantive_text(after):
                continue
            revisions.append((short, subject, when))
        if not revisions:
            continue
        short, subject, when = revisions[0]
        findings.append(StaleStamp(
            path=rel, version=version, stamp=stamp, revised=when,
            commit=short, subject=subject, revisions=len(revisions),
            capped=capped,
        ))
    return findings


def provenance_warnings(root: Path, *, today: date | None = None) -> list[str]:
    """Display lines for `cxp render`'s advisory pass."""
    return [f.describe() for f in stale_stamps(root, today=today)]


__all__ = [
    "MACHINE_PREFIXES", "StaleStamp", "hand_stamp", "is_machine_commit",
    "provenance_warnings", "stale_stamps", "substantive_text",
]
