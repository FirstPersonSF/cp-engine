"""What changed since the planning doc was synthesized (#353 item 3).

WHY THIS EXISTS. `/cp-prep` synthesizes `sprints/<W##>/_planning.md` from a
bundle read at one moment. On 2026-09-01 three meetings changed six projects
the day after synthesis, and nothing said the doc was behind: it read as
current until someone happened to notice. `cxp prep-planning
--since-synthesis` answers "what moved after this doc was written?" so the
doc can be patched (or the meeting told) instead of trusted.

WHAT COUNTS AS A CHANGE, per workstream, after the synthesis time:

- its SPRINT FILE (`sprints/<W##>/<code>.md`, any week) — when the file's
  hand-written text changed (engine regions, comments, the Provenance line
  and whitespace set aside, `provenance_lint.substantive_text`), so a
  `[cp-sync]` re-render of derived strips is not news;
- its EXEC SUMMARY — the `exec-summary` region of its `cp.md` differs
  between the synthesis revision and HEAD (a sync touching the rest of
  cp.md is not news);
- its COMMITMENTS — MC-2 rows created or updated after the synthesis time
  (one query, explicit columns; skipped when MC-2 is not reachable, and
  said so).

The synthesis time is the last commit of `_planning.md`, or the file's mtime
when it has uncommitted edits or was never committed. Committed changes
only on the git side: an edit still in someone's working tree is not yet in
anyone else's planning either.

READ ONLY. Git reads and one MC-2 select; nothing is written.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cp_engine.provenance_lint import substantive_text

log = logging.getLogger(__name__)

PLANNING_NAME = "_planning.md"
_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")


@dataclass
class WorkstreamChange:
    code: str
    sprint_files: list[str] = field(default_factory=list)
    exec_summary: bool = False
    commitments: int = 0
    subjects: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "sprint_files": list(self.sprint_files),
            "exec_summary": self.exec_summary,
            "commitments": self.commitments,
            "commits": list(self.subjects),
        }


@dataclass
class SinceSynthesis:
    planning_path: str
    synthesized_at: datetime
    basis: str  # "commit <sha>" | "mtime"
    changes: list[WorkstreamChange]
    commitments_note: str | None = None

    def to_dict(self) -> dict:
        return {
            "planning_doc": self.planning_path,
            "synthesized_at": self.synthesized_at.isoformat(),
            "basis": self.basis,
            "changed": [c.to_dict() for c in self.changes],
            "commitments_note": self.commitments_note,
        }

    def render_markdown(self) -> str:
        when = self.synthesized_at.strftime("%Y-%m-%d %H:%M %z")
        out = [
            "## Changed since synthesis",
            "",
            f"Planning doc: `{self.planning_path}` — synthesized {when} "
            f"({self.basis}).",
            "",
        ]
        if not self.changes:
            out.append("_No workstream changed after it._")
        else:
            n = len(self.changes)
            out.append(f"{n} workstream{'s' if n != 1 else ''} changed after it:")
            out.append("")
            for c in self.changes:
                what: list[str] = []
                if c.sprint_files:
                    weeks = ", ".join(Path(p).parent.name for p in c.sprint_files)
                    what.append(f"sprint file ({weeks})")
                if c.exec_summary:
                    what.append("Exec Summary")
                if c.commitments:
                    what.append(f"{c.commitments} commitment"
                                f"{'s' if c.commitments != 1 else ''}")
                line = f"- **{c.code}** — {', '.join(what)}"
                if c.subjects:
                    shown = "; ".join(f"`{s}`" for s in c.subjects[:3])
                    more = len(c.subjects) - 3
                    line += f" · {shown}" + (f" (+{more} more)" if more > 0 else "")
                out.append(line)
        if self.commitments_note:
            out.extend(["", f"_({self.commitments_note})_"])
        return "\n".join(out) + "\n"


# ──────────────────────────────────────────────────────────────────────
#  git
# ──────────────────────────────────────────────────────────────────────


def _git(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(["git", "-C", str(root), *args],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def find_planning_doc(root: Path, week_iso: str) -> Path | None:
    """The planning week's `_planning.md`, else the newest one at or before
    that week (Wed–Sun the planning week has rolled forward, and the doc
    being used is still last Monday's)."""
    exact = root / "sprints" / week_iso / PLANNING_NAME
    if exact.is_file():
        return exact
    sprints = root / "sprints"
    if not sprints.is_dir():
        return None
    weeks = sorted(
        (d.name for d in sprints.iterdir()
         if d.is_dir() and _WEEK_RE.match(d.name) and d.name <= week_iso
         and (d / PLANNING_NAME).is_file()),
        reverse=True,
    )
    return sprints / weeks[0] / PLANNING_NAME if weeks else None


def synthesis_time(root: Path, planning: Path) -> tuple[datetime, str, str | None]:
    """(when, basis label, commit sha or None) for the planning doc."""
    rel = planning.relative_to(root).as_posix()
    dirty = _git(root, "status", "--porcelain", "--", rel)
    out = _git(root, "log", "-1", "--format=%H%x09%cI", "--", rel)
    if out and out.strip() and not (dirty or "").strip():
        sha, _, iso = out.strip().partition("\t")
        return datetime.fromisoformat(iso), f"commit {sha[:8]}", sha
    mtime = datetime.fromtimestamp(planning.stat().st_mtime, tz=UTC).astimezone()
    return mtime, "mtime — uncommitted", None


def _code_key(code: str | int | None) -> str:
    """Match key for a workstream across spellings: its MC-2 job number
    (unique tenant-wide — `commitments.resolve_commitment_owner` keys on it
    alone), else the lowercased code."""
    from cp_engine.codes import code_number

    if isinstance(code, int):
        return str(code)
    n = code_number(code)
    return str(n) if n is not None else (code or "").lower()


_ANY_REGION_RE = re.compile(
    r"<!-- cp-engine:start (?P<name>[\w-]+) -->.*?<!-- cp-engine:end (?P=name) -->",
    re.DOTALL,
)


def _sprint_text(text: str | None) -> str:
    """What people and ingest wrote in a sprint file: every engine region
    (carry-forward too — it re-renders last week's items), the scaffold and
    whitespace set aside. A freshly scaffolded week reads as empty."""
    from cp_engine.exec_summary_draft import hand_written_sprint_text

    if not text:
        return ""
    return substantive_text(hand_written_sprint_text(_ANY_REGION_RE.sub("", text)))


def _region(text: str | None) -> str | None:
    from cp_engine.render import slice_exec_summary_region

    return slice_exec_summary_region(text) if text else None


def _git_changes(
    root: Path, since: datetime, base: str | None,
) -> dict[str, WorkstreamChange]:
    """Committed changes after `since`, keyed by workstream code (as the
    path spells it)."""
    log_out = _git(
        root, "log", "--no-merges", "--relative", "--name-only",
        f"--since={since.isoformat()}", "--format=%x00%H%x09%cI%x09%s",
    ) or ""
    touched: dict[str, list[str]] = {}  # rel path -> subjects (newest first)
    for chunk in log_out.split("\x00")[1:]:
        lines = chunk.strip("\n").splitlines()
        if not lines:
            continue
        sha, iso, subject = (lines[0].split("\t", 2) + ["", "", ""])[:3]
        if base and sha == base:
            continue
        try:
            if datetime.fromisoformat(iso) <= since:
                continue  # `--since` is inclusive to the second
        except ValueError:
            pass
        for rel in lines[1:]:
            rel = rel.strip()
            if rel:
                touched.setdefault(rel, []).append(subject)

    # The tree as the doc saw it: the synthesis commit, else the last commit
    # at or before the synthesis time.
    if base is None:
        base = (_git(root, "rev-list", "-1", f"--before={since.isoformat()}",
                     "HEAD") or "").strip() or None

    def at(rev: str | None, rel: str) -> str | None:
        return _git(root, "show", f"{rev}:{rel}") if rev else None

    from cp_engine.codes import code_number

    changes: dict[str, WorkstreamChange] = {}
    for rel, subjects in sorted(touched.items()):
        parts = Path(rel).parts
        code: str | None = None
        kind: str | None = None
        if (len(parts) == 3 and parts[0] == "sprints" and _WEEK_RE.match(parts[1])
                and parts[2].endswith(".md") and code_number(parts[2][:-3]) is not None):
            code, kind = parts[2][:-3], "sprint"
        elif len(parts) >= 2 and parts[-1] == "cp.md":
            code, kind = parts[-2], "cp"
        if code is None:
            continue
        now = at("HEAD", rel)
        before = at(base, rel)
        if kind == "sprint":
            moved = _sprint_text(before) != _sprint_text(now)
        else:
            moved = _region(before) != _region(now) and _region(now) is not None
        if not moved:
            continue
        ch = changes.setdefault(code, WorkstreamChange(code=code))
        if kind == "sprint":
            ch.sprint_files.append(rel)
        else:
            ch.exec_summary = True
        for s in subjects:
            if s not in ch.subjects and not s.startswith("[cp-sync]"):
                ch.subjects.append(s)
    return changes


def _commitment_counts(client, since: datetime) -> dict[str, int]:
    """MC-2 commitments created or updated after `since`, by job-number key.
    Raises on a failed read — the caller reports it."""
    from cp_engine.mc2_db import Tables

    iso = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = (
        client.table(Tables.COMMITMENTS)
        .select("project_id, created_at, updated_at")
        .or_(f"created_at.gt.{iso},updated_at.gt.{iso}")
        .execute().data
    ) or []
    by_project: dict[str, int] = {}
    for r in rows:
        pid = r.get("project_id")
        if pid:
            by_project[pid] = by_project.get(pid, 0) + 1
    if not by_project:
        return {}
    projects = (
        client.table(Tables.PROJECTS)
        .select("id, number")
        .in_("id", list(by_project))
        .execute().data
    ) or []
    out: dict[str, int] = {}
    for p in projects:
        if p.get("number") is not None:
            key = _code_key(int(p["number"]))
            out[key] = out.get(key, 0) + by_project.get(p["id"], 0)
    return out


def changes_since_synthesis(
    root: Path,
    week_iso: str,
    *,
    supabase_client=None,
    projects: Iterable = (),
    codes: Iterable[str] | None = None,
) -> SinceSynthesis | None:
    """The report, or None when there is no planning doc to compare against.

    `projects` (the roster) names a workstream whose only change is in MC-2;
    `codes` (any spelling) scopes the report, like `--projects`."""
    root = Path(root)
    planning = find_planning_doc(root, week_iso)
    if planning is None:
        return None
    since, basis, sha = synthesis_time(root, planning)
    changes = _git_changes(root, since, sha)

    note: str | None = None
    if supabase_client is None:
        note = "commitments not checked — MC-2 not reachable"
    else:
        try:
            counts = _commitment_counts(supabase_client, since)
        except Exception as exc:  # noqa: BLE001 — reported, not swallowed
            note = f"commitments read failed ({type(exc).__name__}: {exc})"
            counts = {}
        by_key = {_code_key(p.code): p.code for p in projects}
        by_key.update({_code_key(c): c for c in changes})
        for key, n in counts.items():
            code = by_key.get(key, f"job {key}")
            changes.setdefault(code, WorkstreamChange(code=code)).commitments += n

    if codes:
        wanted = {_code_key(c) for c in codes}
        changes = {c: v for c, v in changes.items() if _code_key(c) in wanted}

    return SinceSynthesis(
        planning_path=planning.relative_to(root).as_posix(),
        synthesized_at=since,
        basis=basis,
        changes=[changes[c] for c in sorted(changes)],
        commitments_note=note,
    )


__all__ = [
    "SinceSynthesis", "WorkstreamChange", "changes_since_synthesis",
    "find_planning_doc", "synthesis_time",
]
