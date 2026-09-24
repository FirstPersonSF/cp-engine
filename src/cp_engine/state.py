"""Shared data shapes consumed by sync, render, and (eventually) the CLI.

Lives in its own module to break the sync↔render circular import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Literal, Mapping

# Which MC-2 company kind the workstream belongs to, from its company row.
# Renderers group by this to produce the master-CP sections.
CompanyKind = Literal["client", "self-fpsf", "self-canonic"]

# Maps company_kind to the v0.3 working-tree scope directory name. This is
# the contract between MC-2's data model and cp's filesystem layout.
_SCOPE_BY_KIND: dict[str, str] = {
    "client": "1p",
    "self-fpsf": "firstpersonsf",
    "self-canonic": "canonic",
}


def scope_for(company_kind: str) -> str:
    """Return the working-tree scope directory for a company kind.

    Raises ValueError on unknown kinds rather than silently misplacing a
    project — better to fail loud than scaffold under the wrong scope.
    """
    try:
        return _SCOPE_BY_KIND[company_kind]
    except KeyError:
        raise ValueError(
            f"Unknown company_kind {company_kind!r}; expected one of "
            f"{sorted(_SCOPE_BY_KIND)}"
        ) from None


def company_slug(company_name: str | None) -> str:
    """Kebab-case a company name for use as a working-dir name.

    "Infoblox" -> "infoblox". "Sentinel One" -> "sentinel-one".
    "AT&T, Inc." -> "at-t-inc". Empty / None / whitespace-only -> "unknown"
    (so the caller never builds a path with `//` or a missing segment).
    """
    if not company_name or not company_name.strip():
        return "unknown"
    slug = _SLUG_NON_ALPHANUM.sub("-", company_name.lower()).strip("-")
    return slug or "unknown"


# v0.7 working-tree layout: working dirs live under their scope
# (`<tenant>/<scope>/...`), with inactive dirs under a sibling
# `inactive/` bin next to the live dir. Pre-v0.7 layouts (which had an
# extra `projects/` segment) are migrated by `cp migrate-projects-flat`.
#
# v0.7.1 renamed `archived/` → `inactive/`: projects often flip back to
# active (engagements paused and resumed, internal flag toggled, etc.),
# so "inactive" captures the actual semantics better than "archived"
# (which suggests a one-way trip).
INACTIVE_DIR_NAME = "inactive"

# The tenant's top-level scope dirs — the roots every resolver walks.
SCOPE_DIRS: tuple[str, ...] = ("1p", "firstpersonsf", "canonic")

# The machine-readable path index sync writes on every real run (#302,
# plan D10). Committed (the generated .gitignore re-includes it) so the
# webhook's sparse clone and the hosted server's mirror read the same map
# the CLI does; every resolver reads it FIRST and walks the tree second.
PATHS_INDEX_REL = ".cp-engine/paths.json"
PATHS_INDEX_VERSION = 1


def scope_root(tenant_root: Path, scope: str) -> Path:
    """Return `<tenant_root>/<scope>` — the parent of a scope's working dirs."""
    return tenant_root / scope


# ──────────────────────────────────────────────────────────────────────
#  Path authority (#302) — the tree is recursive; nothing constructs a
#  path from a scope + a code any more.
# ──────────────────────────────────────────────────────────────────────

# A parent chain longer than this is a cycle in `parent_code` (MC-2 has no
# constraint against one); fail loud rather than recurse forever.
_MAX_TREE_DEPTH = 32


def path_for(project: "ProjectState", by_code: "Mapping[str, ProjectState]") -> str:
    """The working-dir path for `project`, RELATIVE to the tenant root.

    THE one authority for where a workstream lives (plan §3.3, D5, D10):

    - **account node** (`label == "account"`) → `<scope>/<company-slug>`.
      That is the EXISTING account dir, so the account CP already rendered
      there becomes the node's own `cp.md` and nothing moves.
    - **any node whose parent is in `by_code`** → `<path_for(parent)>/<code>`.
      Programs make this recursive: `1p/google/ggl-5xxx-go-safety/
      ggl-5136-go-safety-website/`.
    - **client job whose parent is unknown or held back** →
      `<scope>/<company-slug>/<code>` — today's layout, so a job whose
      account node is not in the roster does not move.
    - **self-company top-level** → `<scope>/<code>`.

    `by_code` is the roster the caller has (every ProjectState sync read, or
    an empty mapping when only one project is in hand — the fallbacks above
    then reproduce the pre-#302 layout). The code segment is `dir_slug(code)`.
    """
    return _path_for(project, by_code, depth=0)


def _path_for(project: "ProjectState", by_code: "Mapping[str, ProjectState]", *, depth: int) -> str:
    if depth > _MAX_TREE_DEPTH:
        raise ValueError(
            f"parent_code chain for {project.code!r} exceeds {_MAX_TREE_DEPTH} "
            "levels — a cycle in MC-2's parent_id"
        )
    scope = scope_for(project.company_kind)
    if project.label == "account":
        return f"{scope}/{company_slug(project.company_name)}"
    parent = by_code.get(project.parent_code) if project.parent_code else None
    if parent is not None and parent.code != project.code:
        return f"{_path_for(parent, by_code, depth=depth + 1)}/{dir_slug(project.code)}"
    if project.company_kind == "client":
        return f"{scope}/{company_slug(project.company_name)}/{dir_slug(project.code)}"
    return f"{scope}/{dir_slug(project.code)}"


def parent_path_for(project: "ProjectState", by_code: "Mapping[str, ProjectState]") -> str:
    """The directory that CONTAINS the project's working dir, tenant-relative.

    `1p/google` for a job under Google, `1p` for Google's account node,
    `1p/google/<program>` for a job under a program, `firstpersonsf` for an
    internal workstream. This is what the pre-#302 `account_scope_for`
    meant, generalised to the tree; templates render
    `{{ scope }}/{{ dir_slug }}/cp.md` from it plus `dir_name_for`.
    """
    return path_for(project, by_code).rsplit("/", 1)[0]


def dir_name_for(project: "ProjectState", by_code: "Mapping[str, ProjectState]") -> str:
    """The last segment of `path_for` — `dir_slug(code)` for every node
    except the account node, whose dir is the company slug."""
    return path_for(project, by_code).rsplit("/", 1)[-1]


def inactive_path_for(project: "ProjectState", by_code: "Mapping[str, ProjectState]") -> str:
    """Where the project parks when it drops out of sync's view:
    `<parent path>/inactive/<dir name>` — an account's inactive jobs stay
    at `1p/google/inactive/<code>`, as before #302."""
    parent = parent_path_for(project, by_code)
    return f"{parent}/{INACTIVE_DIR_NAME}/{dir_name_for(project, by_code)}"


@dataclass(frozen=True)
class PathEntry:
    """One row of `.cp-engine/paths.json`."""

    code: str
    path: str  # tenant-relative working-dir path
    parent: str | None
    has_agreement: bool
    label: str | None
    mc2_id: str | None
    company: str | None  # company code (GGL, 1PI, …)
    status: str


def paths_index_rows(
    projects: "Iterable[ProjectState]",
) -> dict[str, dict]:
    """The `workstreams` mapping of the index, from a roster."""
    roster = tuple(projects)
    by_code = {p.code: p for p in roster}
    out: dict[str, dict] = {}
    for p in roster:
        out[p.code] = {
            "path": path_for(p, by_code),
            "parent": p.parent_code,
            "has_agreement": bool(p.has_agreement),
            "label": p.label,
            "mc2_id": p.mc2_id,
            "company": p.company_code,
            "status": p.status,
        }
    return out


def _dump_paths_index(workstreams: dict[str, dict], generated_at: str) -> str:
    import json

    doc = {
        "version": PATHS_INDEX_VERSION,
        "generated_at": generated_at,
        "workstreams": workstreams,
    }
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_paths_index(
    tenant_root: Path,
    projects: "Iterable[ProjectState]",
    *,
    now: "datetime | None" = None,
) -> Path | None:
    """Write `.cp-engine/paths.json`; return its path when the bytes changed.

    Byte-stable across runs when nothing changed: the `generated_at` stamp
    only advances when the `workstreams` mapping differs from what is on
    disk, so an unchanged tenant never produces a diff.
    """
    import json

    target = tenant_root / PATHS_INDEX_REL
    rows = paths_index_rows(projects)
    if target.is_file():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            existing = None
        if (
            isinstance(existing, dict)
            and existing.get("version") == PATHS_INDEX_VERSION
            and existing.get("workstreams") == rows
        ):
            return None
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_dump_paths_index(rows, stamp), encoding="utf-8")
    return target


def load_paths_index(tenant_root: Path) -> dict[str, PathEntry]:
    """Read `.cp-engine/paths.json` → `{code: PathEntry}`; `{}` when the file
    is absent, unreadable or not the version this engine writes (a walk
    then resolves everything, exactly as before the index existed)."""
    import json

    target = tenant_root / PATHS_INDEX_REL
    if not target.is_file():
        return {}
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict) or doc.get("version") != PATHS_INDEX_VERSION:
        return {}
    rows = doc.get("workstreams")
    if not isinstance(rows, dict):
        return {}
    out: dict[str, PathEntry] = {}
    for code, row in rows.items():
        if not isinstance(row, dict) or not isinstance(row.get("path"), str):
            continue
        out[code] = PathEntry(
            code=code,
            path=row["path"],
            parent=row.get("parent"),
            has_agreement=bool(row.get("has_agreement", False)),
            label=row.get("label"),
            mc2_id=row.get("mc2_id"),
            company=row.get("company"),
            status=str(row.get("status") or ""),
        )
    return out


def indexed_dir(tenant_root: Path, code: str, mc2_id: str | None = None) -> Path | None:
    """The working dir the index names for `code` — verified to exist, and
    (when `mc2_id` is given and the dir is stamped) to carry that stamp.
    None on any miss; the caller walks."""
    entry = load_paths_index(tenant_root).get(code)
    if entry is None:
        return None
    candidate = tenant_root / entry.path
    if not candidate.is_dir():
        return None
    if mc2_id and entry.mc2_id and entry.mc2_id != mc2_id:
        return None
    return candidate


def iter_workstream_dirs(parent: Path, *, include_inactive: bool = False) -> "Iterator[Path]":
    """Breadth-first walk of the working-dir candidates under `parent`.

    Every direct child of `parent` is a candidate and is descended into;
    deeper dirs are descended into only when they carry a `cp.md` (a
    workstream whose children may be workstreams — a program, an account).
    A job's own subdirs (`spine/`, `meetings/`, `sessions/`) are yielded as
    candidates at their level but never descended, so the walk is bounded
    by the tree's shape, not by a hard-coded depth. `inactive/` bins,
    dot-dirs and `_`-prefixed engine dirs (`_stakeholders/`) are skipped
    unless `include_inactive` names the bins back in.
    """
    from collections import deque

    if not parent.is_dir():
        return
    queue: "deque[tuple[Path, int]]" = deque([(parent, 0)])
    while queue:
        current, depth = queue.popleft()
        try:
            children = sorted(c for c in current.iterdir() if c.is_dir())
        except OSError:
            continue
        for child in children:
            name = child.name
            if name.startswith(".") or name.startswith("_"):
                continue
            if name == INACTIVE_DIR_NAME and not include_inactive:
                continue
            yield child
            if depth == 0 or (child / "cp.md").is_file() or name == INACTIVE_DIR_NAME:
                queue.append((child, depth + 1))


def match_dir_by_name(parent: Path, code: str, *, include_inactive: bool = False) -> Path | None:
    """Name match anywhere under `parent`: an exact `<code>` dir wins over a
    `<code>-<slug>` prefix match; at equal rank the shallower (then
    alphabetical) dir wins. The `inactive` bin itself is never a match."""
    if not code or code == INACTIVE_DIR_NAME:
        return None
    prefix = f"{code}-"
    first_prefix: Path | None = None
    for candidate in iter_workstream_dirs(parent, include_inactive=include_inactive):
        if candidate.name == code:
            return candidate
        if first_prefix is None and candidate.name.startswith(prefix):
            first_prefix = candidate
    return first_prefix


def resolve_project_dir(
    tenant_root: Path,
    project: "ProjectState",
    by_code: "Mapping[str, ProjectState] | None" = None,
) -> Path:
    """Where `project`'s working dir is: the index's answer when it names an
    existing dir, else `path_for` over whatever roster the caller has."""
    hit = indexed_dir(tenant_root, project.code, project.mc2_id)
    if hit is not None:
        return hit
    return tenant_root / path_for(project, by_code or {})


_SLUG_NON_ALPHANUM = re.compile(r"[^a-z0-9]+")


def dir_slug(code: str, name: str | None = None) -> str:
    """Working-directory name for a project = its slugified `code`.

    Since `code` is now the canonical full_job_name slug
    ("ibx-5192-platform-sales-readiness-summit"), the dir IS the code. `name`
    is retained for signature compatibility with existing call sites but no
    longer affects the result (the code already carries the description)."""
    return _SLUG_NON_ALPHANUM.sub("-", code.lower().strip()).strip("-")


def slug_full_job_name(full_job_name: str | None) -> str:
    """Slugify MC-2's `full_job_name` ("IBX 5192 Platform Sales Readiness
    Summit") into the canonical, filesystem/LLM-friendly project id
    ("ibx-5192-platform-sales-readiness-summit"). Returns "" for empty input
    (caller decides the fallback)."""
    if not full_job_name:
        return ""
    return _SLUG_NON_ALPHANUM.sub("-", full_job_name.lower()).strip("-")


@dataclass(frozen=True)
class ProjectState:
    """One workstream in cp-engine's master CP — ONE entry kind (#301).

    Every entry is an MC-2 `projects` row: a client job, a client account
    or program node, or an internal workstream (Mission Control, StoryOS).
    There is no `source` discriminator any more — the engine branches on
    the row's SHAPE: `has_agreement` (a commercial envelope exists),
    `parent_code` (where it sits in its company's tree) and
    `company_kind`. `label` is the derived display word.

    Repos linked to a workstream (`repos.project_id`) do NOT appear as
    separate entries — they render as `_repo-<name>.md` files inside the
    parent's working dir (`linked_repos`).
    """

    code: str  # canonical id: slug(full_job_name), e.g. ggl-5188-activation
    name: str  # full_job_name
    company_kind: CompanyKind
    company_code: str | None  # GGL, IBX, 1PI, CNC, ...
    company_name: str | None  # Google, First Person, Canonic, ...

    # One vocabulary for every workstream: MC_STATUSES
    # (Deal | Open | Holding | Closed | Archived). Active = Deal ∪ Open.
    status: str

    # MC-2's flag as stored. Internal workstreams carry True; it gates
    # NOTHING in the engine any more (they deserve working dirs like every
    # other workstream) and is kept for the allocation rollup's
    # engagement-vs-internal hours split.
    is_internal: bool
    owner: str | None
    last_touched: datetime | None
    deadline: datetime | None  # not tracked yet for either source
    one_line_summary: str | None = None  # regenerated during deepening pass

    # Days the hand-written Exec Summary trails real project activity, or None
    # when it is current / undatable. The one_line_summary above is derived
    # from that hand-written region and nothing automated refreshes it, so a
    # busy project can render months-old prose as current fact. Set during the
    # same deepening pass; see summary.summary_stale_days.
    summary_stale_days: int | None = None

    # The newest dated decision recorded for this project since the Exec
    # Summary was written, as (text, ISO date) — None when the summary is
    # current or nothing newer exists. Rendered BESIDE the summary, never
    # as it: the model owns summary prose (2026-06-30-exec-summary.md).
    latest_signal: tuple[str, str] | None = None

    # When the WORK last moved, from the newest human-dated sprint-file bullet
    # (#260). Distinct from `last_touched` above, which is MC-2's
    # `projects.updated_at` — the job RECORD's mtime, moved by a status flip or
    # a budget edit and not by a week of delivery. Ten projects rendered a May
    # date on 2026-09-15 while several had spine writes that same week.
    #
    # `last_touched` is NOT redefined: `render.is_closed_recent` asks "when did
    # the record change" and is right to. This is a second field, not a fix to
    # that one.
    #
    # WHY NOT mtime. #260 first tried the spine-file mtime `summary_stale_days`
    # uses. Every project landed on the SAME date, because one tenant-wide
    # ingest rewrites every sprint file — a column reading "today" for all 31
    # encodes when sync ran, which is worse than an obviously stale May date
    # because it looks current. Human-assigned dates do not move when a file is
    # rewritten; measured across 51 sprint-file projects they spread May →
    # September and differentiate (ggl-5185 correctly reads August).
    #
    # None when the project has no dated bullet at all — 6 of the rendered
    # projects, all repos/initiatives. Renders as an em dash: no signal is the
    # honest answer, and inventing one is the failure mode above.
    last_activity: date | None = None

    # MC-2 row uuid (`projects.id`). Threaded through so the spine mirror
    # can key `spine_elements.project_id` without re-querying. None only
    # when a fake/legacy state is built without one.
    mc2_id: str | None = None

    # The commercial envelope, when `has_agreement`.
    deal_stage: str | None = None
    budget: float | None = None
    dropbox_folder_url: str | None = None

    # Kept for the `_repo.md`-era view shape; no reader sets them from MC-2
    # any more (standalone repos are gone — every repo hangs off a
    # workstream via `linked_repos`). Templates still read the keys.
    github_org: str | None = None
    repo_name: str | None = None
    description: str | None = None

    # Populated from tenant config's per-project `contacts` array. Plain
    # dicts (typically `{"name": "...", "role": "..."}`) keep the shape
    # flexible without forcing a contact schema on every consumer.
    contacts: tuple[dict, ...] = ()

    # Repos in MC-2 linked to this workstream via `repos.project_id`. Each
    # gets its own `_repo-<repo-name>.md` written into the working dir.
    linked_repos: tuple[LinkedRepo, ...] = ()

    # Workstream shape (mc-2 mig 190+, cp-engine #300/#301). `parent_code`
    # is the canonical code of the parent workstream, None at the top of a
    # company. `has_agreement` is `deal_stage IS NOT NULL` — the commercial
    # envelope exists — and is what the engine branches on; `label` is the
    # DERIVED display word (account | program | job | initiative), never
    # authored (design doc 2026-09-22, decision 3).
    parent_code: str | None = None
    has_agreement: bool = False
    label: WorkstreamLabel | None = None


# The derived display label. Evaluated in this order, first match wins:
# account = no parent, under a client company; program = has children;
# job = has an agreement; initiative = none of the above.
WorkstreamLabel = Literal["account", "program", "job", "initiative"]


def children_of(
    code: str, by_code: "Mapping[str, ProjectState]"
) -> tuple["ProjectState", ...]:
    """Direct children of `code` in the roster, sorted by code."""
    return tuple(
        sorted(
            (p for p in by_code.values() if p.parent_code == code and p.code != code),
            key=lambda p: p.code,
        )
    )


def descendants_of(
    code: str, by_code: "Mapping[str, ProjectState]"
) -> tuple["ProjectState", ...]:
    """Every workstream below `code` (children, grandchildren, …), depth-first
    by code. Bounded by the roster, so a `parent_code` cycle cannot loop."""
    out: list[ProjectState] = []
    seen: set[str] = {code}
    stack = list(reversed(children_of(code, by_code)))
    while stack:
        p = stack.pop()
        if p.code in seen:
            continue
        seen.add(p.code)
        out.append(p)
        stack.extend(reversed(children_of(p.code, by_code)))
    return tuple(out)


def tree_depth(project: "ProjectState", by_code: "Mapping[str, ProjectState]") -> int:
    """How many roster parents sit above `project` (0 for a top-level node)."""
    depth = 0
    seen: set[str] = {project.code}
    current = project
    while current.parent_code and current.parent_code in by_code:
        parent = by_code[current.parent_code]
        if parent.code in seen or depth >= _MAX_TREE_DEPTH:
            break
        seen.add(parent.code)
        depth += 1
        current = parent
    return depth


def effective_label(
    project: "ProjectState", by_code: "Mapping[str, ProjectState]"
) -> WorkstreamLabel:
    """`project.label` when the reader set it, else derived from the roster
    (a fake state built without a label still renders a real word).

    The fallback checks the agreement BEFORE the account rule: an account
    node never carries one (mig 191), so a parentless client row WITH an
    agreement is a job whose parent is simply not in hand — not an account.
    `derive_label` keeps its documented order for the reader, which
    computes `has_children` from the whole table.
    """
    if project.label:
        return project.label
    if children_of(project.code, by_code):
        return "program"
    if project.has_agreement:
        return "job"
    if project.parent_code is None and project.company_kind == "client":
        return "account"
    return "initiative"


def display_name(
    project: "ProjectState", by_code: "Mapping[str, ProjectState] | None" = None
) -> str:
    """The reference-style name for a workstream (CLAUDE.md "Reference
    style", label-driven — #305).

    An account, a program or an initiative is its name alone. A job is the
    SHORT code plus the short name: ``ggl-5168 Activation``. `name` is MC-2's
    `full_job_name` (``GGL 5168 Activation``), so the ``<CO> <number>``
    prefix is stripped before the short code is prepended — the #303 form
    ``<code> <full_job_name>`` read ``ggl-5168-activation GGL 5168
    Activation`` and doubled the identity. A name that does not carry the
    prefix is kept whole.
    """
    label = effective_label(project, by_code or {})
    if label != "job":
        return project.name
    return f"{short_code(project.code)} {short_name(project.name, project.code)}"


def short_code(code: str) -> str:
    """``<company>-<number>`` for any spelling of a workstream code; a code
    the parser rejects is returned unchanged."""
    from cp_engine.codes import parse_code

    parsed = parse_code(code)
    return parsed.short if parsed else code


def short_name(name: str | None, code: str | None = None) -> str:
    """`full_job_name` minus its ``<CO> <number>`` head (``GGL 5168
    Activation`` → ``Activation``); the whole name when the head is absent
    or the strip would leave nothing."""
    from cp_engine.codes import parse_code

    text = (name or "").strip()
    parsed = parse_code(code) if code else None
    if parsed is None:
        parsed = parse_code(text)
    if parsed is None:
        return text
    head = re.compile(
        rf"^\s*{re.escape(parsed.company)}[\s\-_]*{parsed.number}(?:[\s\-_:·—]+|$)",
        re.IGNORECASE,
    )
    stripped = head.sub("", text, count=1).strip()
    return stripped or text


def derive_label(
    *, company_kind: str, parent_code: str | None, has_agreement: bool, has_children: bool
) -> WorkstreamLabel:
    """The display label for a workstream, from its shape alone.

    Rendering and reference-style only — the engine branches on
    `has_agreement`, `parent_code` and `company_kind` directly.

    An account never carries an agreement (mig 191 creates it with
    `deal_stage NULL`), so the account rule requires `not has_agreement`:
    a parentless client row WITH an agreement is a job whose parent is not
    in hand (an archived-only company had no account node until mig 195),
    never an account. Five SentinelOne jobs rendered "Account" before this.
    """
    if parent_code is None and company_kind == "client" and not has_agreement:
        return "account"
    if has_children:
        return "program"
    if has_agreement:
        return "job"
    return "initiative"


@dataclass(frozen=True)
class LinkedRepo:
    """A repo in MC-2 linked to a workstream via `repos.project_id`.

    Carries just enough to render `_repo-<repo-name>.md` under the parent
    working dir: the GitHub coordinate, status, and a short description.
    """

    repo_name: str
    github_org: str
    status: str  # repos.status (Active | Holding | Inactive); Inactive is filtered by the reader
    description: str | None = None


@dataclass(frozen=True)
class Issue:
    """One tracked GitHub Issue, surfaced in a project CP's tracked-issues table."""

    number: int
    title: str
    status: str
    owner: str | None
    updated: datetime | None


@dataclass(frozen=True)
class PersonHours:
    """One person's hours on one project for one week."""

    person_name: str
    hours: float


@dataclass(frozen=True)
class ProjectAllocation:
    """All allocations for one project in one week, sorted by hours desc."""

    project_code: str  # canonical id (matches ProjectState.code)
    is_internal: bool  # excludes from per-row rendering, included in per-person rollup
    entries: tuple[PersonHours, ...]

    @property
    def total_hours(self) -> float:
        return sum(e.hours for e in self.entries)


@dataclass(frozen=True)
class PersonRollup:
    """One person's total hours for the week, split engagement vs internal admin."""

    person_name: str
    engagement_hours: float
    engagement_project_count: int
    internal_hours: float

    @property
    def total_hours(self) -> float:
        return self.engagement_hours + self.internal_hours


@dataclass(frozen=True)
class WeeklyAllocations:
    """All allocations for one week, indexed two ways."""

    week_start: str  # ISO date (YYYY-MM-DD)
    by_project: dict[str, ProjectAllocation]
    rollup: tuple[PersonRollup, ...]  # sorted by total_hours desc


@dataclass(frozen=True)
class ClientAsk:
    """One open question to the client, captured during sprint planning or via deepening."""

    text: str
    asked_date: str  # ISO date
    status: str      # "open" | "answered" | "dropped"
    who: str | None = None
    # Optional due date (`· by YYYY-MM-DD` in the bracket) — the deadline the
    # ask carries; prep + the attention digest escalate against it (#70).
    by: str | None = None


@dataclass(frozen=True)
class Risk:
    """One dependency or risk on a project's sprint plan."""

    text: str
    severity: str    # "escalated" | "watching" | "dependency"
    category: str    # value from tenant config risk_categories
    raised_date: str
    why_it_matters: str | None = None


@dataclass(frozen=True)
class HorizonItem:
    """One forward-looking item (milestone, decision, opportunity) on a project's horizon."""

    text: str
    bucket: str      # "milestone" | "decision" | "opportunity"
    target_date: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class Outbound:
    """One outbound client message: sent, drafted, or queued."""

    text: str
    status: str      # "sent" | "draft" | "queued"
    date: str
    note: str | None = None


@dataclass(frozen=True)
class InboundUpdate:
    """One inbound update from the client, captured in a sprint file."""

    date: str
    who: str
    text: str


@dataclass(frozen=True)
class Deliverable:
    """One deliverable in a sprint plan; position drives priority."""

    text: str
    position: int    # 1-indexed priority


@dataclass(frozen=True)
class MeetingNotes:
    """Decisions plus prose discussion from a partners' weekly review."""

    source: str | None = None
    attendees: str | None = None
    duration: str | None = None
    decisions: tuple[str, ...] = ()
    discussion_prose: str = ""


@dataclass(frozen=True)
class SprintFacts:
    """Engine-managed facts strip at the top of a sprint file."""

    stage: str | None
    owner: str | None
    budget_short: str | None
    last_touched_short: str | None
    last_sprint_hours_line: str | None
    sessions_this_week: int
    open_issues: int


@dataclass(frozen=True)
class SprintCommit:
    """One commit referenced in a sprint file's recent-activity block."""

    sha_short: str
    subject: str
    author: str
    when_short: str


@dataclass(frozen=True)
class WhereItStands:
    """Engine-managed snapshot of recent activity for one project's sprint."""

    last_session_date: str | None
    last_session_who: str | None
    last_session_summary: str | None
    recent_commits: tuple[SprintCommit, ...]
    open_tracked_issues: tuple[Issue, ...]


@dataclass(frozen=True)
class CarryForward:
    """Open items rolled forward from the prior sprint."""

    asks: tuple[ClientAsk, ...]
    risks: tuple[Risk, ...]
    horizon: tuple[HorizonItem, ...]


@dataclass(frozen=True)
class Stakeholder:
    """One person on the client side, with their role and context.

    Parsed from the `### Stakeholders` subsection inside `## Client communication`
    in a sprint file. Bracket convention: `[name · role · context]`.
    """

    name: str
    role: str | None = None
    context: str | None = None


@dataclass(frozen=True)
class Theme:
    """One key thread of discussion for a sprint week.

    Parsed from `## Themes` in `sprints/<W##>/_week.md` (week-scope, not
    per-project). Bracket convention: `[theme · YYYY-MM-DD] text`.
    """

    text: str
    date: str  # ISO date


@dataclass(frozen=True)
class DecisionEntry:
    """One structured decision recorded during a sprint deepening.

    Distinct from `MeetingNotes.decisions` (which is a plain tuple of
    strings for backward compatibility with the freeform partners-review
    surface). DecisionEntry carries the metadata needed for projection
    into project cp.md `recent-decisions-strip` and master-cp.md's
    tenant rollup (the latter only when `cross_cutting=True`).

    Bracket convention in the sprint file's `### Decisions` block:
    `[decision · YYYY-MM-DD][cross-cutting] text` (the `[cross-cutting]`
    flag is optional and absent when False).
    """

    text: str
    date: str  # ISO date
    cross_cutting: bool = False


@dataclass(frozen=True)
class SprintFile:
    """All parsed content of one project's sprint file for one week."""

    project_code: str
    week_iso: str
    week_start: str
    week_end: str
    prior_sprint: str | None
    facts: SprintFacts
    where_it_stands: WhereItStands
    carry_forward: CarryForward
    client_outbound: tuple[Outbound, ...]
    client_open_asks: tuple[ClientAsk, ...]
    client_inbound: tuple[InboundUpdate, ...]
    risks: tuple[Risk, ...]
    allocation: tuple[PersonHours, ...]
    deliverables: tuple[Deliverable, ...]
    definition_of_done: str
    horizon: tuple[HorizonItem, ...]
    meeting_notes: MeetingNotes | None
    # Phase 1.2 (v0.8.5) additions — projected up into project cp.md
    # and master-cp.md engine-managed regions during sync. Empty when
    # the sprint file pre-dates v0.8.5 (no `### Stakeholders` subsection
    # or no bracket-formatted decisions).
    stakeholders: tuple[Stakeholder, ...] = ()
    decisions: tuple[DecisionEntry, ...] = ()

    @property
    def total_allocation_hours(self) -> float:
        return sum(p.hours for p in self.allocation)

    @property
    def escalated_risk_count(self) -> int:
        return sum(1 for r in self.risks if r.severity == "escalated")
