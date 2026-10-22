"""Does an Exec Summary's `· updated` stamp speak for the whole summary? (#251)

WHY THIS EXISTS. Every freshness check on an Exec Summary reads ONE date: the
`## Exec Summary · updated <date>` heading stamp. The stamp advances on any
write to the region, including a one-field write. So a Status-only refresh
makes the whole summary read as current while `Where it stands`, `Next up`
and `Blockers` stay exactly as they were.

Measured on the live tenant 2026-09-30: six active engagements carried a
2026-09-15 stamp (the day 13 summaries got a Status-only refresh via
`capture_project_state`) over body fields last changed on 2026-07-14 or
2026-08-10, 36-63 days before their own stamp. ggl-5188's `Next up` still
read "Drew: widget updates per Tony's spec by Fri 7/17". Sprint planning
reads `Next up`, and the planning bundle called that summary 15 days old.

HOW. The file's own git history is the only record of when each line last
changed, so this reads `git blame` over the region and dates each field by
its newest line. It needs real history:

- A SHALLOW clone (the tenant's daily sync Action checks out with
  `fetch-depth: 1`; the hosted MCP tree is `--depth 1`) blames every line
  to the boundary commit, which would make every field read as brand new.
  So a shallow repo returns None up front. (Blame's own `boundary` flag is
  no help here: in a full clone it marks the ROOT commit, whose dates are
  real.)
- Outside a git repo, or for an untracked file, this returns None.

None means "cannot tell". Callers fall back to the stamp, which is what
they did before this module existed. It surfaces in the planning bundle,
`cxp exec-lint`, `cxp spine-lint`, and master-cp.md (`⚠️ _partial refresh_`
in the project's row, via `sync._derive_partial_refresh_days`). For
master-cp.md, "cannot tell" renders the row exactly as it rendered before
the marker existed, so a shallow sync never adds a false marker. It does
DROP a true one, though: the index only stays steady if every sync that
writes it has full history, which is why the tenant's sync workflow checks
out with `fetch-depth: 0` (#251).

THIS READS ONLY. It authors nothing and edits nothing. The engine owns
read/render and the model owns the prose
(`docs/plans/2026-06-30-exec-summary.md`), so the finding is a warning for
a person, never a rewrite.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from cp_engine.exec_summary_lint import field_label
from cp_engine.render import EXEC_SUMMARY_END, EXEC_SUMMARY_START

logger = logging.getLogger(__name__)

# The fields that carry the project's current state. `Objective` is left
# out on purpose because it is durable and SHOULD outlive many refreshes.
# `Status` is left out because it is the field a partial refresh writes;
# including it would hide the very case this detects. `Updates` and
# `Last session` are logs, not state.
BODY_FIELDS: tuple[str, ...] = ("Where it stands", "Next up", "Blockers")

# How far the newest body-field change may trail the stamp before the stamp
# is said to overstate the summary. Two weeks is one sprint plus slack: a
# wrap-up that legitimately left `Blockers: None` alone for a week is not a
# finding, and a UTC/local date split between the stamp and the commit
# (the 09-15 refresh committed at 06:33 UTC, i.e. 09-14 Pacific) cannot
# trip it.
PARTIAL_REFRESH_LAG_DAYS = 14

_STAMP_RE = re.compile(
    r"^##\s+Exec Summary\s*·\s*updated\s+(?P<date>\d{4}-\d{2}-\d{2})",
    re.MULTILINE,
)
# Scaffold placeholder (`_<...>_`). A scaffold line is not authoring, and
# an all-scaffold field is #190's PARTIAL finding, not this one.
_PLACEHOLDER_RE = re.compile(r"_<[^>]+>_")
_HEADER_RE = re.compile(r"^[0-9a-f]{40} \d+ \d+")


@dataclass(frozen=True)
class PartialRefresh:
    """A summary whose stamp is newer than its state fields."""

    stamp: date
    body_changed: date
    lag_days: int
    fields: tuple[str, ...]

    def describe(self) -> str:
        """One display line. It names the dates so a reader can check them."""
        names = ", ".join(self.fields)
        return (
            f"{names} last changed {self.body_changed.isoformat()}, "
            f"{self.lag_days}d before the {self.stamp.isoformat()} stamp"
        )


def _run_git(args: list[str], cwd: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("git %s failed in %s: %s", args[0], cwd, exc)
        return None
    if proc.returncode != 0:
        logger.debug("git %s exited %s in %s: %s",
                     args[0], proc.returncode, cwd, proc.stderr.strip())
        return None
    return proc.stdout


def _author_date(epoch: int, tz: str) -> date:
    """The commit's calendar date in its author's own timezone. The stamp is
    written in the author's local date, so compare like with like."""
    sign = -1 if tz.startswith("-") else 1
    digits = tz.lstrip("+-")
    try:
        offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4]))
    except ValueError:
        offset = timedelta(0)
    return datetime.fromtimestamp(epoch, timezone(sign * offset)).date()


def blame_field_dates(cp_md_path: Path) -> dict[str, date] | None:
    """Newest change date of each Exec Summary field, from `git blame`.

    Returns {field label: date} for every field that has at least one
    authored, dateable line. Returns None when the history cannot answer:
    no file, no git, a shallow clone, or an untracked file. Uncommitted edits
    are dated today, which is when they are being made (a wrap-up runs the
    lint before it commits).
    """
    if not cp_md_path.is_file():
        return None
    cwd = cp_md_path.parent
    shallow = _run_git(["rev-parse", "--is-shallow-repository"], cwd)
    if shallow is None or shallow.strip() == "true":
        return None
    out = _run_git(
        ["blame", "-w", "--line-porcelain", "--", cp_md_path.name], cwd)
    if out is None:
        return None

    dates: dict[str, date] = {}
    in_region = False
    field: str | None = None
    epoch: int | None = None
    tz = "+0000"
    for raw in out.splitlines():
        if _HEADER_RE.match(raw):
            epoch, tz = None, "+0000"
            continue
        if raw.startswith("author-time "):
            try:
                epoch = int(raw.split(" ", 1)[1])
            except ValueError:
                epoch = None
            continue
        if raw.startswith("author-tz "):
            tz = raw.split(" ", 1)[1].strip()
            continue
        if not raw.startswith("\t"):
            continue

        line = raw[1:]
        if EXEC_SUMMARY_START in line:
            in_region = True
            continue
        if EXEC_SUMMARY_END in line:
            break
        if not in_region:
            continue
        labelled = field_label(line.strip())
        if labelled is not None:
            field = labelled[0]
        elif line.startswith("## "):
            field = None  # the stamp heading belongs to no field
            continue
        if field is None or not line.strip() or _PLACEHOLDER_RE.search(line):
            continue
        if labelled is not None and not labelled[1].strip():
            continue  # a bare `**Next up:**` label is not content
        if epoch is None:
            continue
        when = _author_date(epoch, tz)
        if field not in dates or when > dates[field]:
            dates[field] = when
    return dates


def detect_partial_refresh(
    stamp: date | None,
    field_dates: dict[str, date] | None,
) -> PartialRefresh | None:
    """A PartialRefresh when every authored state field trails the stamp.

    Pure. None when either input is unknown, when no state field has a
    dateable line (nothing to compare), or when the newest state-field change
    is within PARTIAL_REFRESH_LAG_DAYS of the stamp.
    """
    if stamp is None or not field_dates:
        return None
    present = {f: field_dates[f] for f in BODY_FIELDS if f in field_dates}
    if not present:
        return None
    newest = max(present.values())
    lag = (stamp - newest).days
    if lag < PARTIAL_REFRESH_LAG_DAYS:
        return None
    return PartialRefresh(
        stamp=stamp,
        body_changed=newest,
        lag_days=lag,
        fields=tuple(f for f in BODY_FIELDS if f in present),
    )


def partial_refresh(cp_md_path: Path) -> PartialRefresh | None:
    """Read the stamp and blame the region for one project cp.md."""
    try:
        text = cp_md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    m = _STAMP_RE.search(text)
    if m is None:
        return None
    # A machine draft (#251, `exec_summary_draft`) writes all four state
    # fields together and stamps `· drafted by cp`. It is current by
    # construction, so it is never a partial refresh — even when one drafted
    # field came out identical to the old text and so kept its old blame
    # date. The first human write clears the marker (`exec_summary_merge`
    # rewrites the whole stamp line), and detection resumes from there.
    from cp_engine.exec_summary_draft import is_drafted

    if is_drafted(text):
        return None
    try:
        stamp = date.fromisoformat(m.group("date"))
    except ValueError:
        return None
    return detect_partial_refresh(stamp, blame_field_dates(cp_md_path))


def partial_refresh_warning(cp_md_path: Path) -> str | None:
    """The lint line for `cxp exec-lint` / `cxp spine-lint`, or None."""
    found = partial_refresh(cp_md_path)
    if found is None:
        return None
    return (
        f"⚠ exec-summary stamp overstates it: {found.describe()}. "
        "Refresh them against current reality, or confirm they still "
        "hold"
    )


# ──────────────────────────────────────────────────────────────────────
#  Unauthored, but fed (#353 item 2)
# ──────────────────────────────────────────────────────────────────────
#
# Meeting auto-ingest writes a workstream's sprint file and meeting ledger;
# it never touches the Exec Summary. On 2026-09-01 a full client briefing
# landed on sap-5198 while its cp.md was still the scaffold, and prep said
# "not yet authored" — which reads as "nothing has happened". The ingest
# commits are the record that something has: each lands as
# `[auto-ingest] <code>: meeting <id>`. Counting them turns the silence into
# a number a person can act on. Read-only, like the rest of this module.

# The `<co>-<number>` prefix. Ingest commits name a workstream by whichever
# spelling MC-2 carried (`slt-5196`, `ggl-5216-google`), so match both.
_SHORT_CODE_RE = re.compile(r"^([a-z0-9]+-\d+)(?:-|$)")
_INGEST_MEETING_RE = re.compile(r": meeting (?P<id>\S+)")


@dataclass(frozen=True)
class IngestedMeetings:
    """Meetings auto-ingest landed on one workstream, per the git log."""

    meetings: int
    first: date
    last: date

    def describe(self) -> str:
        noun = "meeting" if self.meetings == 1 else "meetings"
        return (f"{self.meetings} {noun} ingested since "
                f"{self.first.isoformat()} (latest {self.last.isoformat()})")


def _code_pattern(code: str) -> str:
    """ERE matching every spelling of `code` an ingest subject may carry."""
    m = _SHORT_CODE_RE.match(code or "")
    if m is None:
        return re.escape(code)
    short = m.group(1)
    return f"{short}(-[a-z0-9-]+)?"


def ingested_meetings(repo_dir: Path, code: str) -> IngestedMeetings | None:
    """Distinct meetings `[auto-ingest] <code>: meeting <id>` commits landed.

    None when there are none, or when git cannot answer (no repo, or a
    shallow clone, whose single commit would undercount to a misleading
    "1 meeting"). Account and sprint-planning summaries
    (`[auto-ingest] account:<code>:`) are not this workstream's own ingest
    and do not count.
    """
    shallow = _run_git(["rev-parse", "--is-shallow-repository"], repo_dir)
    if shallow is None or shallow.strip() == "true":
        return None
    out = _run_git(
        ["log", "-E", f"--grep=^\\[auto-ingest\\] {_code_pattern(code)}: meeting ",
         "--format=%cs%x09%s"],
        repo_dir,
    )
    if not out:
        return None
    seen: dict[str, date] = {}
    for line in out.splitlines():
        day, _, subject = line.partition("\t")
        m = _INGEST_MEETING_RE.search(subject)
        if m is None:
            continue
        try:
            when = date.fromisoformat(day)
        except ValueError:
            continue
        mid = m.group("id")
        seen[mid] = min(when, seen.get(mid, when))
    if not seen:
        return None
    dates = sorted(seen.values())
    return IngestedMeetings(meetings=len(seen), first=dates[0], last=dates[-1])


def unauthored_ingest(cp_md_path: Path, code: str | None = None) -> IngestedMeetings | None:
    """Ingest that reached a workstream whose Exec Summary is still scaffold.

    None when the summary is authored (any real field), when the cp.md has
    no exec-summary region, or when no ingest is on record. `code` defaults
    to the workstream directory's name.
    """
    from cp_engine.render import exec_summary_is_authored, slice_exec_summary_region

    try:
        text = cp_md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    region = slice_exec_summary_region(text)
    if region is None or exec_summary_is_authored(region):
        return None
    return ingested_meetings(cp_md_path.parent, code or cp_md_path.parent.name)


def unauthored_ingest_warning(cp_md_path: Path, code: str | None = None) -> str | None:
    """The lint line for `cxp exec-lint` / `cxp spine-lint`, or None."""
    found = unauthored_ingest(cp_md_path, code)
    if found is None:
        return None
    return (
        f"⚠ exec-summary never authored, but {found.describe()}. The state "
        "is in the sprint files; author it at wrap up (or let "
        "`cxp draft-summaries` draft it)"
    )
