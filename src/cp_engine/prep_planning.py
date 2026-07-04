"""Forward-looking sprint-planning doc renderer — Task 6 of v0.15.0.

Replaces ``cp prep-agenda`` for sprint planning. Where prep-agenda surfaces
*backward-looking* state (recent inbound, aged asks, last-week decisions),
this command assembles a *forward-looking* doc anchored on ClickUp
milestones: per-account project blocks each with a Where line, a Forward
Calendar (milestones with due dates), and an Open Commitments table that
two-ways tracks "us → them" (milestones we own) and "them → us" (client-
asks we're waiting on).

Source of truth for milestones is ClickUp. Each active project (engagement
OR initiative) carries a ``clickup_list_id`` in MC-2; this module fetches
``tags[]=milestone`` and ``tags[]=client-ask`` tasks from each list via the
ClickUp REST API.

``_detect_urgent`` surfaces per-project attention items via four urgency
rules (slip_risk, decision_due, past_due_ask, escalated_risk). See its
docstring for rule semantics.

``_render_cross_cutting`` returns the tenant strip plus capacity-binding
owners (>=5 projects of record) and ``weekly-cp.md`` cross-cutting
decisions (last 4 weeks, unresolved). Implicit-owner detection from
sprint-file asks/commitments is deferred to v2.

The CLI entry point (``cp prep-planning``) lives in cli.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, TypedDict

import httpx

from cp_engine.agenda import (
    WeeklyDecision,
    filter_active,
    parse_weekly_decisions,
    short_iso_date,
    to_datetime,
)
from cp_engine.config import TenantConfig
from cp_engine.render import (
    exec_summary_is_authored,
    slice_exec_summary_region,
)
from cp_engine.sprints import (
    bullets,
    current_sprint_week_iso,
    parse_bracketed_bullet,
    section_body,
    sprint_week_dates,
    subsection,
)
from cp_engine.state import (
    ProjectState,
    account_scope_for,
    dir_slug,
    scope_for,
)

log = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
#  Shapes
# ──────────────────────────────────────────────────────────────────────


class Milestone(TypedDict, total=False):
    """Normalized ClickUp milestone task.

    Consumed by the table renderer and by ``_detect_urgent`` (which reads
    ``status``, ``date``, ``confidence``, ``depends_on``, and
    ``deliverable``). ``task_type`` is either ``"milestone"`` or
    ``"client_ask"`` — the same module fetches both shapes from ClickUp;
    the renderer routes by ``task_type``.
    """

    id: str
    task_type: str  # "milestone" | "client_ask"
    deliverable: str
    date: str  # ISO YYYY-MM-DD, "" if no due_date
    owner: str
    confidence: str  # "high" | "medium" | "low"
    depends_on: list[str]
    status: str
    linked_to: list[str]


class SprintAsk(TypedDict):
    """Open ask parsed from a sprint file (markdown-only, not yet in ClickUp)."""

    text: str
    who: str
    asked: str  # ISO date
    by: str  # ISO date, "" if no due-date
    hash: str


class CapacityBindingOwner(TypedDict, total=False):
    """A single capacity-binding owner entry.

    Two shapes, distinguished by ``CapacityBinding["basis"]``:
      - ``planned_allocations`` → ``{"owner", "planned_hours", "project_count"}``
        (from the planning week's MC-2 sprint_allocations);
      - ``owner_of_record`` → ``{"owner", "count"}`` (fallback when the
        planning week has no allocation rows: projects-of-record count,
        an account-management fact, labeled honestly as such).
    """

    owner: str
    count: int
    planned_hours: int
    project_count: int


class CapacityBinding(TypedDict):
    """Capacity-binding block: which fact it's based on + the owners."""

    basis: str  # "planned_allocations" | "owner_of_record"
    owners: list[CapacityBindingOwner]


@dataclass
class ProjectPlanningBlock:
    """Per-project rendered content."""

    project: ProjectState
    exec_summary: str | None
    milestones: tuple[Milestone, ...]
    client_asks: tuple[Milestone, ...]
    sprint_open_asks: tuple[SprintAsk, ...]
    urgent: tuple[dict, ...]  # flags from _detect_urgent (may be empty)
    fetch_error: str | None  # non-None when ClickUp fetch failed OR list is unset/empty
    # Optional whole-project sweep synthesis (Project Spine slice 3, Phase B).
    # None on the default fast path — only populated when ``cp prep-planning
    # --sweep`` injects a ``sweep_llm`` and the project has a backfilled spine.
    # Best-effort per project: a sweep failure leaves this None and the block
    # still renders. See ``build_project_block``'s ``sweep_llm`` handling.
    sweep_synthesis: str | None = None
    # ``fetch_error`` carries three distinct sentinels alongside genuine
    # network/4xx error strings — keep the rendering branches in
    # ``_render_forward_calendar`` and the error-aggregation guard in
    # ``build_planning_result`` in sync with this list:
    #   "no_clickup_list"      — project has no clickup_list_id in MC-2
    #   "no_milestones_tagged" — list_id present, fetch returned 0 milestones
    #                            AND 0 client-asks (back-population pending)
    #   <other string>         — real ClickUp/network/4xx error (counts as
    #                            errored in milestone_counts, surfaces in
    #                            result.errors)


@dataclass
class PlanningResult:
    """Top-level structured output. The renderer calls ``to_markdown``."""

    week_iso: str
    week_dates: str  # "Jun 8 – Jun 14"
    project_count: int
    estimated_minutes: int
    tenant_hours_last_week: dict[str, int]
    blocks_by_account: dict[str, list[ProjectPlanningBlock]]
    milestone_counts: dict[str, int]  # {"total", "fetched", "errored"}
    urgent_counts: dict[str, int]
    # Planning-week allocations (forward capacity, issue #16). Empty dict =
    # no allocation rows entered for the planning week yet — that absence is
    # itself signal and renders as an explicit note.
    tenant_hours_planned: dict[str, int] = field(default_factory=dict)
    # Task 8: capacity-binding owners + cross-cutting decisions partners owe
    # each other. Populated by build_planning_result; consumed by
    # _render_cross_cutting. Both default to empty so the renderer can
    # safely skip the corresponding sub-blocks.
    capacity_binding: CapacityBinding = field(
        default_factory=lambda: {"basis": "owner_of_record", "owners": []}
    )
    cross_cutting_decisions: tuple[WeeklyDecision, ...] = ()
    # Window-filter accounting for the decisions surface: how many entries
    # were dropped as stale (older than the 28-day lookback) and how many
    # carry no date at all (kept, but flagged so the rot stays visible).
    cross_cutting_decisions_stale_count: int = 0
    cross_cutting_decisions_undated_count: int = 0
    errors: list[str] = field(default_factory=list)
    generated_at: str = ""

    def to_summary_dict(self) -> dict:
        return {
            "week_iso": self.week_iso,
            "week_dates": self.week_dates,
            "project_count": self.project_count,
            "estimated_minutes": self.estimated_minutes,
            "tenant_hours_last_week": self.tenant_hours_last_week,
            "tenant_hours_planned": self.tenant_hours_planned,
            "milestone_counts": self.milestone_counts,
            "urgent_counts": self.urgent_counts,
            "capacity_binding": {
                "basis": self.capacity_binding.get("basis", "owner_of_record"),
                "owners": [
                    dict(b) for b in self.capacity_binding.get("owners", [])
                ],
            },
            "cross_cutting_decisions_count": len(self.cross_cutting_decisions),
            "cross_cutting_decisions_stale_count": (
                self.cross_cutting_decisions_stale_count
            ),
            "cross_cutting_decisions_undated_count": (
                self.cross_cutting_decisions_undated_count
            ),
            "errors": self.errors,
        }


# ──────────────────────────────────────────────────────────────────────
#  ClickUp REST client
# ──────────────────────────────────────────────────────────────────────


_CLICKUP_BASE = "https://api.clickup.com/api/v2"
_CLICKUP_TIMEOUT = 15.0


# ClickUp token env-var names, in preference order. The engine canonically
# uses ``CLICKUP_API_TOKEN``; MC-2's ``backend/.env`` stores the same
# credential as ``CLICKUP_API_KEY``. Accept both so the token resolves whether
# it's exported in the shell or only present in the MC-2 clone's .env.
_CLICKUP_TOKEN_KEYS = ("CLICKUP_API_TOKEN", "CLICKUP_API_KEY")


def _clickup_token() -> str | None:
    """Read the ClickUp token from the environment (either accepted name)."""
    for key in _CLICKUP_TOKEN_KEYS:
        val = os.environ.get(key)
        if val:
            return val
    return None


def _resolve_clickup_token(config: TenantConfig) -> str | None:
    """Resolve the ClickUp token: env first, then ``<mc-2 clone>/backend/.env``.

    Mirrors ``sync_mc2._load_supabase_creds`` so ``cp prep-planning`` run from
    a fresh shell resolves the token the same way SUPABASE creds do — instead
    of silently failing every project's milestone fetch. Accepts both
    ``CLICKUP_API_TOKEN`` and ``CLICKUP_API_KEY`` (MC-2's .env uses the latter).
    Prints a one-line note on .env fallback so the implicit dependency stays
    visible. Returns None if neither source has it.
    """
    token = _clickup_token()
    if token:
        return token

    from cp_engine.sync_mc2 import _mc2_env_file, _read_dotenv

    env_file = _mc2_env_file(config)
    if env_file is None:
        return None
    file_creds = _read_dotenv(env_file, _CLICKUP_TOKEN_KEYS)
    for key in _CLICKUP_TOKEN_KEYS:
        val = file_creds.get(key)
        if val:
            print(f"Loaded {key} from {env_file}", file=sys.stderr)
            return val
    return None


# Sentinel substrings used by ``build_planning_result`` to recognize
# auth-failure ``fetch_error`` strings without re-parsing them. Keep these
# substrings present in the corresponding ``RuntimeError`` messages in
# ``_fetch_clickup_milestones`` or the tenant-wide dedup goes silent.
_AUTH_ERROR_SUBSTRINGS = (
    "ClickUp auth failed",
    "CLICKUP_API_TOKEN not set",
)


def _is_auth_error(fetch_error: str) -> bool:
    """True when ``fetch_error`` came from a missing/invalid ClickUp token."""
    return any(s in fetch_error for s in _AUTH_ERROR_SUBSTRINGS)


_CLICKUP_PAGE_SIZE = 100  # ClickUp /list/{id}/task max page size
_CLICKUP_MAX_PAGES = 100  # safety cap: 100 pages × 100/page = 10k tasks


def _fetch_clickup_milestones(
    list_id: str,
    *,
    tag: str = "milestone",
    token: str | None = None,
    client: httpx.Client | None = None,
) -> list[dict]:
    """Fetch tasks tagged ``tag`` from ClickUp list ``list_id``.

    Paginates over ``GET /list/{id}/task`` (ClickUp caps each page at 100
    tasks); the loop stops when a page returns fewer than 100 tasks. A
    ``_CLICKUP_MAX_PAGES`` safety raises ``RuntimeError`` past 10k tasks
    on a single list so we never spin forever on a misconfigured tag.

    Raises ``RuntimeError`` on any 4xx/5xx or network error so the caller
    can degrade per-project. The renderer wraps this in a try/except and
    surfaces the error as a "could not fetch" placeholder.
    """
    if token is None:
        token = _clickup_token()
    if not token:
        # The "not set in environment" phrasing is load-bearing — the
        # per-project loop in ``build_planning_result`` greps it (along
        # with "ClickUp auth failed") to surface one tenant-wide entry
        # in ``result.errors`` regardless of how many projects hit the
        # same missing token.
        raise RuntimeError("CLICKUP_API_TOKEN not set in environment")

    headers = {"Authorization": token}
    url = f"{_CLICKUP_BASE}/list/{list_id}/task"

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=_CLICKUP_TIMEOUT)

    all_tasks: list[dict] = []
    page = 0
    try:
        while True:
            if page >= _CLICKUP_MAX_PAGES:
                raise RuntimeError(
                    f"ClickUp pagination exceeded {_CLICKUP_MAX_PAGES} pages "
                    f"for list {list_id} (over "
                    f"{_CLICKUP_MAX_PAGES * _CLICKUP_PAGE_SIZE} tasks)"
                )
            params: list[tuple[str, str]] = [
                ("tags[]", tag),
                ("include_closed", "false"),
                ("subtasks", "true"),
                ("page", str(page)),
            ]
            try:
                resp = client.get(url, headers=headers, params=params)
            except httpx.HTTPError as exc:
                raise RuntimeError(f"ClickUp network error: {exc}") from exc

            if resp.status_code in (401, 403):
                # Single explicit auth-failure shape — the per-project
                # loop matches the "ClickUp auth failed" substring to
                # dedupe a tenant-wide entry in result.errors.
                raise RuntimeError(
                    f"ClickUp auth failed (HTTP {resp.status_code}): "
                    f"check CLICKUP_API_TOKEN"
                )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"ClickUp returned {resp.status_code} for list "
                    f"{list_id}: {resp.text[:200]}"
                )
            try:
                data = resp.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"ClickUp returned non-JSON: {exc}"
                ) from exc
            page_tasks = data.get("tasks") or []
            all_tasks.extend(page_tasks)
            if len(page_tasks) < _CLICKUP_PAGE_SIZE:
                break
            page += 1
    finally:
        if owns_client:
            client.close()

    return all_tasks


# Fallback for tasks pushed by older / external integrations that put the
# date in the task name (e.g. "Workshop (due 2026-06-17, owner: Marcello)")
# but never set ClickUp's structured ``due_date`` field. Without this the
# Forward Calendar bullet is skipped because ``date == ""``. Matches the
# `(due YYYY-MM-DD` shape the v0.15 LLM emits for milestone deliverables.
# Tracked for fix in fathom-meeting-sync (Task 63) so the structured
# due_date gets set going forward; the regex stays as defensive cover for
# legacy data and any other push source that omits the field.
_NAME_DUE_DATE_RE = re.compile(r"\(due\s+(\d{4}-\d{2}-\d{2})")


def _parse_due_date_from_name(name: str) -> str:
    """Best-effort extract of ``YYYY-MM-DD`` from a ClickUp task name."""
    match = _NAME_DUE_DATE_RE.search(name or "")
    return match.group(1) if match else ""


def _from_clickup_timestamp(ts: str | int | None) -> str:
    """Convert a ClickUp due_date (ms since epoch as string) to ISO date.

    Returns "" when ``ts`` is None/empty.
    """
    if ts is None or ts == "":
        return ""
    try:
        ms = int(ts)
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()


def _normalize_status(status: str | None) -> str:
    """Lowercase the ClickUp status name. Defaults to "open"."""
    if not status:
        return "open"
    return status.strip().lower()


def _normalize_clickup_task(task: dict) -> Milestone:
    """Flatten a ClickUp task into the planner's milestone shape.

    Custom fields are matched by *name*, not UUID, because the UUIDs are
    workspace-specific. We expect three custom field names:
        Confidence  — "high" | "medium" | "low" (default "medium")
        Type        — "action_item" | "client_ask" | "milestone"
        Linked To   — comma-separated cp codes
    """
    custom = {
        f.get("name"): f.get("value")
        for f in task.get("custom_fields") or []
        if f.get("name")
    }
    assignees = task.get("assignees") or []
    owner = "—"
    if assignees:
        first = assignees[0]
        owner = first.get("username") or first.get("email") or "—"
    status_obj = task.get("status") or {}
    status_name = (
        status_obj.get("status")
        if isinstance(status_obj, dict)
        else str(status_obj)
    )
    depends_on = [
        d.get("task_id")
        for d in task.get("dependencies") or []
        if d.get("task_id")
    ]
    linked_raw = custom.get("Linked To")
    if isinstance(linked_raw, str) and linked_raw.strip():
        linked_to = [s.strip() for s in linked_raw.split(",") if s.strip()]
    else:
        linked_to = []
    type_value = custom.get("Type")
    task_type = "milestone"
    if isinstance(type_value, str) and type_value.strip():
        task_type = type_value.strip().lower()
    elif _has_tag(task, "client-ask"):
        task_type = "client_ask"

    confidence = custom.get("Confidence")
    if not isinstance(confidence, str) or confidence.strip().lower() not in (
        "high",
        "medium",
        "low",
    ):
        confidence = "medium"
    else:
        confidence = confidence.strip().lower()

    return Milestone(
        id=str(task.get("id") or ""),
        task_type=task_type,
        deliverable=str(task.get("name") or "").strip(),
        date=(
            _from_clickup_timestamp(task.get("due_date"))
            or _parse_due_date_from_name(str(task.get("name") or ""))
        ),
        owner=owner,
        confidence=confidence,
        depends_on=depends_on,
        status=_normalize_status(status_name),
        linked_to=linked_to,
    )


def _has_tag(task: dict, tag: str) -> bool:
    """True if the task carries the named tag."""
    target = tag.lower()
    for t in task.get("tags") or []:
        name = t.get("name") if isinstance(t, dict) else t
        if isinstance(name, str) and name.lower() == target:
            return True
    return False


# ──────────────────────────────────────────────────────────────────────
#  MC-2 project resolution
# ──────────────────────────────────────────────────────────────────────


def _resolve_clickup_list_for_project(
    project: ProjectState,
    *,
    supabase_client,
) -> str | None:
    """Resolve a ProjectState to its ``clickup_list_id`` from MC-2.

    Delegates to ``ingest._resolve_proposal_project`` so engagement vs
    initiative resolution stays in one place. Returns None if the
    project isn't in MC-2, has ClickUp disabled, or no list configured.
    """
    if supabase_client is None:
        return None
    try:
        from cp_engine.ingest import _resolve_proposal_project
    except ImportError:  # pragma: no cover — defensive
        return None
    try:
        row = _resolve_proposal_project(supabase_client, project.code)
    except Exception as exc:  # noqa: BLE001 — Supabase errors must not block
        log.warning("clickup-list resolve failed for %s: %s", project.code, exc)
        return None
    if not row:
        return None
    return row.get("clickup_list_id")


# ──────────────────────────────────────────────────────────────────────
#  Per-project data assembly
# ──────────────────────────────────────────────────────────────────────


def _extract_exec_summary(cp_md_body: str) -> str | None:
    """Return the inner text of a project cp.md's engine-managed
    ``exec-summary`` region — the model-authored 6-field state box (Objective,
    Status, Where it stands, Next up, Blockers, Updates + Last session).

    The marker lines themselves are stripped; leading/trailing blank lines are
    trimmed. Returns ``None`` when:

      * the region markers are absent, or
      * the region is entirely UNAUTHORED — every ``**Label:**`` field value is
        still a ``_<...>_`` placeholder AND there are no real seed bullets (the
        only bullet allowed to count as "no content" is the auto-stamped
        ``- <date> — migrated from Quick Resume`` migration line).

    Pure function on the body string. Region slicing + the authored-check
    are shared with agenda.py via render.py's authoritative copies.
    """
    region = slice_exec_summary_region(cp_md_body)
    if region is None:
        return None
    if not exec_summary_is_authored(region):
        return None
    return region


# Open-ask bullet shape from sprint files. Mirrors attention_digest._OPEN_ASK_RE.
_SPRINT_OPEN_ASK_RE = re.compile(
    r"^- \[open · (?P<asked>\d{4}-\d{2}-\d{2}) · (?P<who>[^\]]+?)"
    r"(?: · by (?P<by>\d{4}-\d{2}-\d{2}))?\]\s+(?P<text>.+?)"
    r"\s+<!--\s*cp:hash=(?P<hash>[0-9a-f]{8})\s*-->\s*$",
    re.MULTILINE,
)


def _parse_sprint_open_asks(sprint_file_path: Path) -> tuple[SprintAsk, ...]:
    """Read a project's current sprint file and return its open asks.

    Returns () when the file doesn't exist. The bridging period (before all
    asks live in ClickUp) puts these into the Open Commitments table flagged
    "(sprint file)" so reviewers know to promote them.
    """
    if not sprint_file_path.is_file():
        return ()
    body = sprint_file_path.read_text(encoding="utf-8")
    out: list[SprintAsk] = []
    for m in _SPRINT_OPEN_ASK_RE.finditer(body):
        text = m.group("text").strip()
        # Strip the trailing hash marker should it leak past the non-greedy
        # capture (defensive — the regex's lookahead handles the common case).
        text = re.sub(r"\s*<!--\s*cp:hash=[0-9a-f]+\s*-->\s*$", "", text).strip()
        out.append(
            SprintAsk(
                text=text,
                who=m.group("who").strip(),
                asked=m.group("asked"),
                by=m.group("by") or "",
                hash=m.group("hash"),
            )
        )
    return tuple(out)


_URGENT_HORIZON_KEYWORDS = ("this sprint", "next sprint", "by workshop")
_URGENT_HORIZON_DAYS = 14


def _check_dep_stale(
    dep: str,
    sprint_asks: tuple[SprintAsk, ...],
    today: date,
) -> int | None:
    """Return staleness in days if a sprint ask mentions ``dep`` past its `by`."""
    if not dep:
        return None
    needle = dep.lower()
    for ask in sprint_asks:
        text = (ask.get("text") or "").lower()
        if needle not in text:
            continue
        by = ask.get("by") or ""
        if not by:
            continue
        try:
            by_date = date.fromisoformat(by)
        except ValueError:
            continue
        if by_date < today:
            return (today - by_date).days
    return None


# Sprint scaffold placeholder shape: a bullet whose body (after the leading
# `- `) is entirely italicized angle-bracket hint text, e.g.
# ``- _<choice — `[by W##]` prefix>_`` or ``- _<risk — `[severity · category
# · date]` prefix>_``. Real human-written bullets never wrap their entire
# body in single-underscore italics + angle brackets; placeholders always do.
# Surveyed against the live tenant's W20–W23 sprint files — all 13 distinct
# placeholder shapes match this pattern (decisions-due, risks, milestones,
# stakeholders, outbound/inbound, Slack digest, opportunities, etc.).
# Matches ingest.py's `placeholder_re` so the two stay in lockstep.
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"^\s*-\s+_<[^>]+>_\s*$")


def _is_template_placeholder(bullet_first_line: str) -> bool:
    """True when a bullet's first line is an unfilled scaffold placeholder.

    ``bullets`` returns ``(first_line, continuation)`` pairs whose first_line
    still carries the leading ``- ``; the regex anchors on that.
    """
    return bool(_TEMPLATE_PLACEHOLDER_RE.match(bullet_first_line))


def _parse_decisions_due_from_body(body: str) -> tuple[dict, ...]:
    """Pull `## Horizon → ### Decisions due` items from a raw sprint file body.

    Skips unfilled scaffold placeholder bullets (``- _<choice — ...>_``) so
    they don't surface as spurious ``decision_due`` urgency flags — the
    placeholder text ``[by W##]`` would otherwise trip
    ``_is_decision_horizon_urgent``'s ISO-week substring match.
    """
    horizon_section = section_body(body, "Horizon")
    decisions_sub = subsection(horizon_section, "Decisions due")
    out: list[dict] = []
    for first, _cont in bullets(decisions_sub):
        if _is_template_placeholder(first):
            continue
        parsed = parse_bracketed_bullet(first)
        if parsed:
            parts, text = parsed
            target = parts[0] if parts else ""
            out.append({"text": text, "target_date": target})
        else:
            # Plain bullet without bracket meta: treat as urgent (no date).
            text = first.lstrip("- ").strip()
            if text:
                out.append({"text": text, "target_date": ""})
    return tuple(out)


def _parse_risks_from_body(body: str) -> tuple[dict, ...]:
    """Pull `## Dependencies & risks` items from a raw sprint file body.

    Skips unfilled scaffold placeholder bullets (``- _<risk — ...>_``). The
    risk placeholder leads with ``[severity · category · date]`` — the
    literal token ``severity`` would slip past ``parse_bracketed_bullet``
    as a non-escalated severity today, but the same scaffold could just as
    easily be misread tomorrow. Filter at the source so the rule only ever
    sees human-written bullets.
    """
    risks_section = section_body(body, "Dependencies & risks")
    out: list[dict] = []
    for first, _cont in bullets(risks_section):
        if _is_template_placeholder(first):
            continue
        parsed = parse_bracketed_bullet(first)
        if not parsed:
            continue
        parts, text = parsed
        severity = parts[0] if parts else "watching"
        out.append({"severity": severity, "text": text})
    return tuple(out)


def _is_decision_horizon_urgent(target_date: str, today: date) -> bool:
    """True when a Decisions-due target falls in the next two sprints (blank = urgent)."""
    if not target_date:
        return True
    lowered = target_date.lower()
    if any(k in lowered for k in _URGENT_HORIZON_KEYWORDS):
        return True
    # Try the whole thing as an ISO date.
    try:
        target = date.fromisoformat(target_date)
        return 0 <= (target - today).days <= _URGENT_HORIZON_DAYS
    except ValueError:
        pass
    # Last shot: hunt an ISO-ish substring (e.g. "by 2026-06-11").
    m = re.search(r"(\d{4}-\d{2}-\d{2})", target_date)
    if m:
        try:
            target = date.fromisoformat(m.group(1))
            return 0 <= (target - today).days <= _URGENT_HORIZON_DAYS
        except ValueError:
            return False
    return False


def _detect_urgent(
    project: ProjectState,
    milestones: tuple[Milestone, ...],
    sprint_asks: tuple[SprintAsk, ...],
    *,
    today: date | None = None,
    sprint_file_body: str | None = None,
) -> list[dict]:
    """Surface this-week-attention items for the project's planning block.

    Returns a flat list of flag dicts shaped::

        {"type": <rule>, "text": <one-line>, "severity": "warn" | "alert"}

    Four rules in order (deterministic for rendering + assertions):

      1. **slip_risk** — milestone due within ``_URGENT_HORIZON_DAYS`` whose
         status is still open AND has at least one risk signal (low
         confidence or a stale-by-sprint-ask ``depends_on``).
      2. **decision_due** — entry under ``## Horizon → ### Decisions due``
         whose target lands this/next sprint (see
         ``_is_decision_horizon_urgent``).
      3. **past_due_ask** — sprint_ask with ``by`` strictly before today.
      4. **escalated_risk** — entry under ``## Dependencies & risks`` whose
         bracket-meta begins with ``escalated``.

    Sprint-file-sourced rules (2 + 4) take the raw markdown body as a
    keyword arg so tests don't need a full scaffold and the caller stays
    in charge of file IO. None / missing body → just no sprint-derived
    flags from this project.
    """
    if today is None:
        today = date.today()
    flags: list[dict] = []

    # Rule 1 — slip_risk
    for m in milestones:
        if (m.get("status") or "").lower() in ("shipped", "slipped", "closed", "done"):
            continue
        due_str = m.get("date") or ""
        if not due_str:
            continue
        try:
            due_date = date.fromisoformat(due_str)
        except ValueError:
            continue
        days_to_due = (due_date - today).days
        if not (0 <= days_to_due <= _URGENT_HORIZON_DAYS):
            continue

        reasons: list[str] = []
        if (m.get("confidence") or "").lower() == "low":
            reasons.append("low confidence")
        # Substring match between dep ID and ask text — v1 simplicity. Authors
        # spell depends_on ClickUp task IDs as short tags in ask text, so false
        # positives are tolerable since flags surface to humans, not actions.
        for dep in m.get("depends_on") or []:
            stale_days = _check_dep_stale(dep, sprint_asks, today)
            if stale_days is not None:
                reasons.append(f"{dep} stale {stale_days}d")

        if reasons:
            deliverable = m.get("deliverable") or "(untitled)"
            flags.append(
                {
                    "type": "slip_risk",
                    "text": f"{deliverable} (due {due_str}): "
                    + ", ".join(reasons),
                    "severity": "warn",
                }
            )

    # Rule 2 — decision_due
    if sprint_file_body:
        for d in _parse_decisions_due_from_body(sprint_file_body):
            if _is_decision_horizon_urgent(d["target_date"], today):
                flags.append(
                    {
                        "type": "decision_due",
                        "text": d["text"],
                        "severity": "alert",
                    }
                )

    # Rule 3 — past_due_ask
    for ask in sprint_asks:
        by = ask.get("by") or ""
        if not by:
            continue
        try:
            by_date = date.fromisoformat(by)
        except ValueError:
            continue
        if by_date >= today:
            continue
        age_days = (today - by_date).days
        text = ask.get("text") or "(no text)"
        flags.append(
            {
                "type": "past_due_ask",
                "text": f"{text} ({age_days}d past due)",
                "severity": "warn" if age_days < _URGENT_HORIZON_DAYS else "alert",
            }
        )

    # Rule 4 — escalated_risk
    if sprint_file_body:
        for r in _parse_risks_from_body(sprint_file_body):
            if (r.get("severity") or "").lower() == "escalated":
                flags.append(
                    {
                        "type": "escalated_risk",
                        "text": r.get("text") or "(no text)",
                        "severity": "alert",
                    }
                )

    return flags


def build_project_block(
    project: ProjectState,
    *,
    config: TenantConfig,
    supabase_client,
    today: date,
    week_iso: str,
    clickup_client: httpx.Client | None,
    clickup_token: str | None,
    list_id_override: str | None = None,
    clickup_task_ids: dict[str, str] | None = None,
    sweep_llm: Callable[[str], str] | None = None,
) -> ProjectPlanningBlock:
    """Assemble all per-project rendering data.

    ``clickup_task_ids`` maps cp_ask_hash → clickup_task_id for asks that
    have already been promoted to ClickUp. During the bridging period a
    sprint-file ask may also exist as a ClickUp client-ask; dedupe so the
    Open Commitments table doesn't render the same ask twice.

    ``sweep_llm`` (Project Spine slice 3, Phase B) is the opt-in seam: when
    provided, this loads the project's on-disk spine elements and runs a
    whole-project sweep synthesis via ``run_sweep``, attaching it to the
    block as ``sweep_synthesis``. When None (the default fast path), no
    spine load and no LLM call happen at all — the default planning doc is
    unchanged and free. Best-effort: a sweep failure (or a project with no
    backfilled spine) logs + leaves ``sweep_synthesis=None`` without aborting.
    """
    # Exec Summary region from the project's cp.md
    scope = account_scope_for(project)
    slug = dir_slug(project.code, project.name)
    cp_md_path = config.root / scope / slug / "cp.md"
    exec_summary: str | None = None
    if cp_md_path.is_file():
        try:
            exec_summary = _extract_exec_summary(
                cp_md_path.read_text(encoding="utf-8")
            )
        except OSError as exc:
            log.warning("cp.md read failed for %s: %s", project.code, exc)

    # Sprint-file open asks (bridging period — not yet in ClickUp).
    sprint_file_path = (
        config.root / "sprints" / week_iso / f"{project.code}.md"
    )
    sprint_asks = _parse_sprint_open_asks(sprint_file_path)
    # Bridging-period dedupe: filter out asks that already have a ClickUp
    # task (they'll surface via ``client_asks`` from the ClickUp fetch
    # below). Without this, the Open Commitments table renders the same
    # ask twice — once as a ClickUp client-ask, once with a ``(sprint
    # file)`` suffix. ``clickup_task_ids`` is None in older callers
    # (degrades to no-op).
    if clickup_task_ids:
        sprint_asks = tuple(
            a for a in sprint_asks if a["hash"] not in clickup_task_ids
        )
    # Read the sprint body once so urgent-detection's Rule 2 + Rule 4 can
    # parse Decisions due / Dependencies & risks without re-reading the file.
    sprint_file_body: str | None = None
    if sprint_file_path.is_file():
        sprint_file_body = sprint_file_path.read_text(encoding="utf-8")

    # ClickUp fetch — milestones + client-asks.
    list_id = list_id_override or _resolve_clickup_list_for_project(
        project, supabase_client=supabase_client
    )
    milestones: tuple[Milestone, ...] = ()
    client_asks: tuple[Milestone, ...] = ()
    fetch_error: str | None = None
    if not list_id:
        fetch_error = "no_clickup_list"
    else:
        try:
            raw_ms = _fetch_clickup_milestones(
                list_id,
                tag="milestone",
                token=clickup_token,
                client=clickup_client,
            )
            milestones = tuple(_normalize_clickup_task(t) for t in raw_ms)
            raw_asks = _fetch_clickup_milestones(
                list_id,
                tag="client-ask",
                token=clickup_token,
                client=clickup_client,
            )
            client_asks = tuple(_normalize_clickup_task(t) for t in raw_asks)
            # List exists but nothing milestone-tagged yet — distinguish from
            # the list-not-set case so the renderer can point users to the
            # back-population work (Task 29) rather than nagging them to set
            # a list-id they already have.
            if not milestones and not client_asks:
                fetch_error = "no_milestones_tagged"
        except RuntimeError as exc:
            fetch_error = str(exc)
            log.warning("ClickUp fetch failed for %s: %s", project.code, exc)

    # Stable, oldest-first ordering by date for the forward calendar.
    milestones = tuple(
        sorted(milestones, key=lambda m: (m.get("date") or "9999-99-99"))
    )

    urgent_list = _detect_urgent(
        project,
        milestones,
        sprint_asks,
        today=today,
        sprint_file_body=sprint_file_body,
    )

    # Opt-in whole-project sweep synthesis (Project Spine slice 3, Phase B).
    # Default path (sweep_llm is None) does NOTHING here — no disk read, no
    # LLM call. When a sweep_llm is injected, load the project's spine
    # (MC-2-canonical, disk fallback) and run the sweep. The MC-2 client
    # already in scope from the ClickUp list resolution is reused. Best-effort:
    # any failure logs + leaves sweep_synthesis None.
    sweep_synthesis: str | None = None
    if sweep_llm is not None:
        sweep_synthesis = _run_project_sweep(
            project,
            config=config,
            supabase_client=supabase_client,
            today=today,
            sweep_llm=sweep_llm,
        )

    return ProjectPlanningBlock(
        project=project,
        exec_summary=exec_summary,
        milestones=milestones,
        client_asks=client_asks,
        sprint_open_asks=sprint_asks,
        urgent=tuple(urgent_list),
        fetch_error=fetch_error,
        sweep_synthesis=sweep_synthesis,
    )


def _run_project_sweep(
    project: ProjectState,
    *,
    config: TenantConfig,
    supabase_client,
    today: date,
    sweep_llm: Callable[[str], str],
) -> str | None:
    """Run a best-effort whole-project sweep for one project (Phase B).

    Loads the project's spine elements MC-2-canonical with a disk fallback
    (matching ``cp spine`` / ``cp sweep``) and runs ``run_sweep``. The disk
    working dir is resolved via ``find_spine_dir`` (prefix-tolerant), not a
    hand-built exact-slug path — name-drifted / legacy-bare-code dirs still
    resolve. Returns the synthesis text, or None when there's nothing to
    attach:
      - the project has no spine in MC-2 *and* no backfilled ``spine/`` dir
        on disk (find_spine_dir raises / empty elements);
      - the sweep raises (LLM/transport error, missing ANTHROPIC_API_KEY) —
        logged as a warning so the planning doc still builds for every other
        project.
    """
    from cp_engine.spine import (
        SpineDirNotFound,
        find_spine_dir,
        load_spine,
        load_spine_from_mc2,
    )
    from cp_engine.spine_sweep import run_sweep

    try:
        # Load the project's spine — MC-2 canonical, disk fallback (matches
        # cp spine/sweep). Reuse the client already in scope; if none, try a
        # best-effort connect (any failure → disk fallback, no propagation).
        elements: tuple = ()
        client = supabase_client
        if client is None:
            from cp_engine import mc2_db

            # offline / no creds → None → disk path (fail-soft by contract)
            client = mc2_db.get_client(config, required=False)
        if client is not None:
            try:
                elements = load_spine_from_mc2(client, project.code)
            except Exception:  # noqa: BLE001 — read error → disk fallback
                elements = ()
        if not elements:
            try:
                project_dir = find_spine_dir(config.root, project.code)
            except SpineDirNotFound:
                log.debug(
                    "no spine elements for %s — skipping sweep", project.code
                )
                return None  # no spine on disk either → no sweep
            elements = load_spine(project_dir)
        if not elements:
            log.debug(
                "no spine elements for %s — skipping sweep", project.code
            )
            return None  # no spine backfilled → nothing to synthesize
        result = run_sweep(project.code, elements, today=today, llm=sweep_llm)
    except Exception as exc:  # noqa: BLE001 — best-effort per project
        log.warning("sweep failed for %s: %s", project.code, exc)
        return None
    return result.synthesis_text


# ──────────────────────────────────────────────────────────────────────
#  Account grouping
# ──────────────────────────────────────────────────────────────────────


_INTERNAL_ACCOUNT_FOR_SCOPE = {
    "firstpersonsf": "First Person SF (internal)",
    "canonic": "Canonic (internal)",
}


def _account_display_name(project: ProjectState) -> str:
    """Human-readable account header for a project.

    Engagements (company_kind="client") group by their company name. FPSF
    and Canonic initiatives/repos group under one synthetic header each
    so the partner's eye walks one "account" per company in the doc.
    """
    if project.company_kind == "client" and project.company_name:
        return project.company_name
    scope = scope_for(project.company_kind)
    return _INTERNAL_ACCOUNT_FOR_SCOPE.get(scope, scope)


def _group_by_account(
    blocks: tuple[ProjectPlanningBlock, ...],
) -> dict[str, list[ProjectPlanningBlock]]:
    """Bucket blocks by account, preserving project order inside each bucket.

    Account-header order is alphabetical, matching how master-cp.md walks
    accounts (and how agenda.py sorts its projects).
    """
    buckets: dict[str, list[ProjectPlanningBlock]] = {}
    for b in blocks:
        buckets.setdefault(_account_display_name(b.project), []).append(b)
    return dict(sorted(buckets.items(), key=lambda kv: kv[0].lower()))


# ──────────────────────────────────────────────────────────────────────
#  Renderer
# ──────────────────────────────────────────────────────────────────────


def _render_tenant_strip(
    project_count: int,
    tenant_hours: dict[str, int],
    tenant_hours_planned: dict[str, int] | None = None,
) -> str:
    """The ``**Active:** N · **Last sprint:** ...`` strip, plus the
    planning-week allocations line (forward capacity, issue #16).

    An empty planned dict is rendered as an explicit note rather than
    omitted — "allocations not entered yet" is itself a planning signal.
    """

    def _fmt(hours: dict[str, int]) -> str:
        ordered = sorted(hours.items(), key=lambda kv: -kv[1])
        return ", ".join(f"{name} {h}h" for name, h in ordered)

    hours_str = _fmt(tenant_hours) if tenant_hours else "—"
    strip = f"**Active:** {project_count} · **Last sprint:** {hours_str}"
    if tenant_hours_planned:
        strip += f"\n**Planned (this sprint):** {_fmt(tenant_hours_planned)}"
    else:
        strip += (
            "\n**Planned (this sprint):** _(no allocations entered for the "
            "planning week yet)_"
        )
    return strip


# ──────────────────────────────────────────────────────────────────────
#  Task 8: cross-cutting detection (capacity binding + partner decisions)
# ──────────────────────────────────────────────────────────────────────


# Owners with >= this many projects of record become "capacity binding".
# The retro pegged 5 as the floor at which an owner stops being able to
# context-switch cleanly between projects in a single sprint.
_CAPACITY_BINDING_FLOOR = 5

# Planned-allocations basis (issue #16 follow-through): an owner is
# capacity-binding when their PLANNED hours for the planning week reach
# this floor, or when they're allocated across >= _CAPACITY_BINDING_FLOOR
# projects that week. 40h = a full week already committed before the
# sprint starts.
_CAPACITY_BINDING_PLANNED_HOURS = 40

# Cross-cutting decisions are surfaced when their parser date is within
# this many days of `today` — the weekly-cp.md section header itself
# scopes to "last 4 weeks", and we mirror that bound here so old hand-
# written entries don't bleed forward indefinitely.
_CROSS_CUTTING_LOOKBACK_DAYS = 28

# Decisions tagged with this inline marker have already been resolved and
# should drop out of the planning surface. Matches forms like
# "[decided: 2026-05-30]", "[decided yes]", or "[resolved: 2026-05-30]" —
# partners use the two verbs interchangeably.
_DECIDED_MARKER_RE = re.compile(
    r"\[(?:decided|resolved)[:\s][^\]]*\]",
    re.IGNORECASE,
)


def _detect_capacity_binding(
    projects: tuple[ProjectState, ...],
) -> tuple[CapacityBindingOwner, ...]:
    """Owners carrying >= ``_CAPACITY_BINDING_FLOOR`` projects of record.

    Returns ``CapacityBindingOwner`` dicts ordered by count desc, then
    name asc for stable rendering. ``"—"`` and ``None`` owners are
    ignored so an unassigned-owner cluster never trips the binding flag.

    TODO(v2): layer implicit ownership — for each project, parse its
    current sprint file's ``### Open asks`` and ``### Commitments`` so a
    person who's named on many asks without being owner-of-record (the
    Tony shape from the W19 retro) also surfaces here. Skipped in v1
    because the parsing is gnarly and the explicit count is a useful
    floor on its own.
    """
    counts: Counter[str] = Counter()
    for p in projects:
        owner = (p.owner or "").strip()
        if not owner or owner == "—":
            continue
        counts[owner] += 1
    binding: list[CapacityBindingOwner] = [
        {"owner": name, "count": count}
        for name, count in counts.most_common()
        if count >= _CAPACITY_BINDING_FLOOR
    ]
    # most_common preserves insertion order for ties; re-sort to break
    # ties alphabetically so output is deterministic across runs.
    binding.sort(key=lambda b: (-b["count"], b["owner"].lower()))
    return tuple(binding)


def _detect_capacity_binding_planned(planned_allocations) -> list[CapacityBindingOwner]:
    """Capacity binding from the planning week's actual allocations.

    An owner binds when their planned hours reach
    ``_CAPACITY_BINDING_PLANNED_HOURS`` OR they're allocated on >=
    ``_CAPACITY_BINDING_FLOOR`` projects that week. Reads a
    ``state.WeeklyAllocations`` (rollup for hours; by_project for the
    per-owner project spread). Ordered by planned hours desc, then name.
    """
    project_counts: Counter[str] = Counter()
    for alloc in (planned_allocations.by_project or {}).values():
        for entry in alloc.entries:
            name = (entry.person_name or "").strip()
            if name:
                project_counts[name] += 1
    out: list[CapacityBindingOwner] = []
    for r in planned_allocations.rollup or ():
        name = (r.person_name or "").strip()
        if not name:
            continue
        hours = int(round(r.total_hours))
        count = project_counts.get(name, 0)
        if hours >= _CAPACITY_BINDING_PLANNED_HOURS or count >= _CAPACITY_BINDING_FLOOR:
            out.append(
                {"owner": name, "planned_hours": hours, "project_count": count}
            )
    out.sort(key=lambda b: (-b["planned_hours"], b["owner"].lower()))
    return out


def _is_resolved_decision(text: str) -> bool:
    """True if the decision text carries a ``[decided: ...]`` or
    ``[resolved: ...]`` marker — partners use the two verbs interchangeably
    to signal an entry has been settled and should drop from the surface.
    """
    return bool(_DECIDED_MARKER_RE.search(text))


def _load_cross_cutting_decisions(
    tenant_root: Path,
    *,
    today: date,
    lookback_days: int = _CROSS_CUTTING_LOOKBACK_DAYS,
) -> tuple[tuple[WeeklyDecision, ...], list[str], int, int]:
    """Read ``weekly-cp.md`` and return decisions still owed across partners.

    Returns ``(decisions, errors, stale_count, undated_count)`` — caller
    appends ``errors`` to the PlanningResult and surfaces the two counts
    in ``--summary`` so section rot is visible instead of silent.

    Filters:
      - Section absent or empty → ``((), [], 0, 0)``.
      - Entry's date older than ``lookback_days`` → dropped, counted in
        ``stale_count``.
      - Entry has NO date at all → KEPT (safe default — an undated entry
        is more likely fresh-and-unstamped than ancient) and counted in
        ``undated_count``.
      - Entry text contains a ``[decided: ...]`` or ``[resolved: ...]``
        marker → dropped.
      - Entry's date is malformed (not ISO-8601) → SKIPPED entirely +
        warning logged + error appended. Pre-v0.15 we fell through with
        ``d_date = today``, which surfaced typo'd entries with the
        wrong implicit date — worse than dropping them, because partners
        couldn't tell anything was wrong.

    Reuses ``agenda.parse_weekly_decisions`` so the parsing contract is
    shared with ``cp prep-agenda``. The order returned mirrors the
    handwritten numbering in ``weekly-cp.md`` (newest entries at top).
    """
    weekly_path = tenant_root / "weekly-cp.md"
    if not weekly_path.is_file():
        return (), [], 0, 0
    body = weekly_path.read_text(encoding="utf-8")
    decisions = parse_weekly_decisions(body)
    if not decisions:
        return (), [], 0, 0
    cutoff = today - timedelta(days=lookback_days)
    out: list[WeeklyDecision] = []
    errors: list[str] = []
    stale_count = 0
    undated_count = 0
    for d in decisions:
        if _is_resolved_decision(d.text):
            continue
        if not d.date:
            undated_count += 1
            out.append(d)
            continue
        try:
            d_date = date.fromisoformat(d.date)
        except (ValueError, TypeError):
            log.warning(
                "malformed date in weekly-cp.md decision: %r (entry: %s)",
                d.date,
                d.text[:60],
            )
            errors.append(
                f"malformed date in cross-cutting decision: {d.date!r}"
            )
            continue
        if d_date < cutoff:
            stale_count += 1
            continue
        out.append(d)
    return tuple(out), errors, stale_count, undated_count


def _render_cross_cutting(
    result: PlanningResult,
) -> list[str]:
    """Render the top-of-doc tenant strip + cross-cutting block.

    The shape is:

        ## Tenant strip
        **Active:** N · **Last sprint:** ...

        ## Cross-cutting (read before walking projects)
        **Capacity binding constraints:**
        - **<name>** — owner-of-record on N projects
        ...
        **Decisions partners owe each other this week:**
        1. <decision text>
        ...

    Either sub-block is omitted entirely when its source data is empty,
    so a clean tenant week renders just the tenant strip + a "no
    cross-cutting signals" line.
    """
    out = ["## Tenant strip"]
    out.append(
        _render_tenant_strip(
            result.project_count,
            result.tenant_hours_last_week,
            result.tenant_hours_planned,
        )
    )
    out.append("")
    out.append("## Cross-cutting (read before walking projects)")

    basis = result.capacity_binding.get("basis", "owner_of_record")
    binding = result.capacity_binding.get("owners", [])
    decisions = result.cross_cutting_decisions

    if not binding and not decisions:
        out.append("_(no cross-cutting signals this sprint)_")
        return out

    if binding:
        out.append("**Capacity binding constraints:**")
        for entry in binding:
            name = entry["owner"]
            if basis == "planned_allocations":
                hours = entry.get("planned_hours", 0)
                count = entry.get("project_count", 0)
                suffix = "" if count == 1 else "s"
                out.append(
                    f"- **{name}** — {hours}h planned across "
                    f"{count} project{suffix} this sprint"
                )
            else:
                count = entry.get("count", 0)
                suffix = "" if count == 1 else "s"
                out.append(
                    f"- **{name}** — owner-of-record on {count} "
                    f"project{suffix} _(no planning-week allocations yet — "
                    "count is projects of record, not planned hours)_"
                )

    if decisions:
        if binding:
            out.append("")
        out.append("**Decisions partners owe each other this week:**")
        for i, d in enumerate(decisions, 1):
            out.append(f"{i}. {d.text}")

    return out


def _render_forward_calendar(block: ProjectPlanningBlock) -> list[str]:
    """Render a per-project Forward calendar bullet list.

    Skips milestones with no date (no anchor to render). Falls back to a
    placeholder when nothing remains.
    """
    if block.fetch_error == "no_clickup_list":
        return [
            "**Forward calendar:**",
            "_(ClickUp list not set in MC-2 — milestones not tracked)_",
        ]
    if block.fetch_error == "no_milestones_tagged":
        return [
            "**Forward calendar:**",
            "_(no milestones tagged in ClickUp yet — see Task 29 back-population)_",
        ]
    if block.fetch_error:
        return [
            "**Forward calendar:**",
            "_Could not fetch milestones — check ClickUp connection._",
        ]
    dated = [m for m in block.milestones if m.get("date")]
    if not dated:
        return [
            "**Forward calendar:**",
            "_(no milestones tracked yet)_",
        ]
    out = ["**Forward calendar:**"]
    for m in dated:
        date_short = short_iso_date(m["date"]) or m["date"]
        owner = m.get("owner") or "—"
        confidence = m.get("confidence") or "medium"
        deliverable = m.get("deliverable") or "(untitled)"
        deps = m.get("depends_on") or []
        meta = [f"{owner}", f"{confidence} confidence"]
        if deps:
            meta.append(f"depends_on: {', '.join(deps)}")
        out.append(f"- {date_short} — {deliverable} ({'; '.join(meta)})")
    return out


def _md_table_cell(text: str | None) -> str:
    """Escape markdown-table-breaking characters in a cell value.

    A literal `|` in a cell terminates the column (corrupting the row);
    a literal newline collapses the row into multiple lines and breaks
    the table outright. ClickUp task names and Fathom-derived asks both
    contain pipes and newlines in the wild — the latter especially after
    LLM cleanup. Carriage returns get the same treatment for Windows
    payloads.

    None becomes "" so callers don't have to pre-coerce.
    """
    if text is None:
        return ""
    s = str(text).replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    return s.strip()


def _render_commitments_table(block: ProjectPlanningBlock) -> list[str]:
    """Two-way commitments table: us→them (milestones) + them→us (asks).

    The "to/from" mapping:
        Milestone (us → them) — Who = owner, To = client, By = date.
        ClickUp client-ask    — Who = ClickUp owner (or "client" fallback),
                                To = "us", By = date.
        Sprint-file open ask  — Who = ask's "who" field, To/From inferred,
                                flagged "(sprint file)" so reviewers promote.

    All cell values flow through ``_md_table_cell`` so literal `|` or
    newline in a ClickUp task name doesn't corrupt the rendered table row.
    """
    rows: list[tuple[str, str, str, str]] = []  # (Who, Owes what, To, By)
    for m in block.milestones:
        date_str = short_iso_date(m.get("date")) or "—"
        rows.append(
            (
                m.get("owner") or "—",
                m.get("deliverable") or "(untitled)",
                "client",
                date_str,
            )
        )
    for ca in block.client_asks:
        date_str = short_iso_date(ca.get("date")) or "—"
        # Client-asks: the "owner" assigned in ClickUp is who owes us the
        # answer — typically the client-side person. Surface as "external".
        who = ca.get("owner") or "client"
        rows.append((who, ca.get("deliverable") or "(untitled)", "us", date_str))
    for sa in block.sprint_open_asks:
        date_str = short_iso_date(sa["by"]) if sa.get("by") else "—"
        rows.append(
            (
                sa.get("who") or "—",
                f"{sa.get('text')} _(sprint file)_",
                "us",
                date_str,
            )
        )
    if not rows:
        return ["**Open commitments:** _(none)_"]
    out = [
        "**Open commitments:**",
        "| Who | Owes what | To | By |",
        "|---|---|---|---|",
    ]
    for who, what, to, by in rows:
        out.append(
            f"| {_md_table_cell(who)} | {_md_table_cell(what)} "
            f"| {_md_table_cell(to)} | {_md_table_cell(by)} |"
        )
    return out


# Shared sub-blocks used by both the human-doc and bundle project renderers,
# so the identical surface (header collapse, urgent loop, the unauthored
# sentinel) stays single-sourced and the two renderers can't drift.

# Emitted for a block whose exec_summary was never authored. Single-sourced
# so the wording matches across the doc + bundle renderers.
_EXEC_SUMMARY_UNAUTHORED_MARKER = (
    "_(Exec Summary not yet authored — fill in at wrap up before "
    "sprint planning)_"
)


def _render_block_header(p: ProjectState) -> list[str]:
    """The `### <code> <name> — <owner>` header + blank line. Standalone repos
    (and some initiatives) carry name == code, which used to render as a
    duplicated header like "### cp cp — Drew and Tony"; collapse to one slug in
    that case — cosmetic only, exact em-dash and spacing preserved."""
    owner = p.owner or "—"
    if p.code == p.name:
        return [f"### {p.code} — {owner}", ""]
    return [f"### {p.code} {p.name} — {owner}", ""]


def _render_urgent(block: ProjectPlanningBlock) -> list[str]:
    """The urgent-flags sub-block (or the no-urgent-items placeholder)."""
    if not block.urgent:
        return ["_(no urgent items)_", ""]
    out = ["**Urgent:**"]
    for item in block.urgent:
        severity = item.get("severity", "warn")
        marker = "ALERT" if severity == "alert" else "WARN"
        out.append(f"- [{marker}] {item.get('text', '(no text)')}")
    out.append("")
    return out


def _render_project_block(block: ProjectPlanningBlock) -> list[str]:
    """Render one project's section: urgent → Where → Forward → Commitments."""
    out = _render_block_header(block.project)
    out.extend(_render_urgent(block))

    if block.exec_summary:
        out.append(block.exec_summary)
    else:
        out.append(_EXEC_SUMMARY_UNAUTHORED_MARKER)
    out.append("")

    # Whole-project sweep synthesis (Project Spine slice 3, Phase B). Only
    # present when ``cp prep-planning --sweep`` ran and the project had a
    # backfilled spine; absent on the default fast path.
    if block.sweep_synthesis:
        out.append("**Sweep:**")
        out.append("")
        out.append(block.sweep_synthesis.strip())
        out.append("")

    out.extend(_render_forward_calendar(block))
    out.append("")
    out.extend(_render_commitments_table(block))
    out.append("")
    return out


def _render_account_section(
    account_name: str,
    blocks: list[ProjectPlanningBlock],
    *,
    project_renderer=_render_project_block,
) -> list[str]:
    """Render one account's section: header + every project block.

    ``project_renderer`` selects the per-project block shape — the default
    human-doc block, or ``_render_bundle_project_block`` for ``--bundle``. The
    account framing (header + ``---`` separators) is identical either way, so
    it lives here once."""
    out = [f"## {account_name} ({len(blocks)} projects)", ""]
    for i, block in enumerate(blocks):
        if i:
            out.append("---")
            out.append("")
        out.extend(project_renderer(block))
    return out


def render_planning_doc_markdown(result: PlanningResult) -> str:
    """Render the final markdown document."""
    lines: list[str] = []
    lines.append(
        f"# Sprint {result.week_iso} Planning — {result.week_dates}"
    )
    lines.append(
        f"_Generated {result.generated_at} by `cp prep-planning "
        f"--legacy-render` · "
        f"{result.project_count} active projects · "
        f"{result.estimated_minutes} min target_"
    )
    lines.append("")
    lines.extend(_render_cross_cutting(result))
    lines.append("")
    lines.append("---")
    lines.append("")
    for account_name, blocks in result.blocks_by_account.items():
        lines.extend(_render_account_section(account_name, blocks))
        lines.append("---")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_bundle_project_block(block: ProjectPlanningBlock) -> list[str]:
    """Render one project's bundle section: header → urgent → FULL exec
    summary → forward calendar → commitments → fetch_error.

    Shares the header + urgent sub-blocks with ``_render_project_block`` (via
    ``_render_block_header`` / ``_render_urgent``). Diverges from it in three
    intentional ways: (1) labels the exec summary with a `**Exec Summary:**`
    heading; (2) OMITS the ``sweep_synthesis`` block (the bundle is raw source
    material — sweep is a doc-only synthesis, mirroring the bundle entry
    point's "minus sweep_llm"); (3) appends the ``fetch_error`` note so the
    model sees gaps in the source material. The exec_summary body itself is
    emitted verbatim in BOTH renderers — the difference is the surrounding
    framing, not truncation.
    """
    out = _render_block_header(block.project)
    out.extend(_render_urgent(block))

    # FULL exec summary verbatim — every project, including unauthored ones
    # (explicit marker, never silently omitted).
    out.append("**Exec Summary:**")
    out.append("")
    if block.exec_summary:
        out.append(block.exec_summary)
    else:
        out.append(_EXEC_SUMMARY_UNAUTHORED_MARKER)
    out.append("")

    out.extend(_render_forward_calendar(block))
    out.append("")
    out.extend(_render_commitments_table(block))
    out.append("")

    if block.fetch_error:
        out.append(f"_(fetch note: {block.fetch_error})_")
        out.append("")
    return out


def render_planning_bundle(result: PlanningResult) -> str:
    """Render the model-facing structured bundle.

    This is NOT a polished human doc — it is the consistent, current raw
    material the model reads to synthesize ``_planning.md`` in-session
    (Task 8 skill). It mirrors ``render_planning_doc_markdown``'s structure
    (header → cross-cutting metrics → per-account project blocks) and reuses
    the same render helpers, but emits each project's FULL ``exec_summary``
    verbatim and surfaces every project (incl. unauthored ones) with an
    explicit marker rather than a trimmed view.

    All data is READ from ``result`` — nothing is recomputed. ``build_planning_
    result`` already populated ``exec_summary`` per block and all the tenant
    metrics (capacity binding, cross-cutting decisions, milestone/urgent
    counts).
    """
    lines: list[str] = []
    lines.append(
        f"# Sprint {result.week_iso} Planning Bundle — {result.week_dates}"
    )
    lines.append(
        f"_Generated {result.generated_at} by `cp prep-planning --bundle` · "
        f"{result.project_count} active projects · "
        f"{result.estimated_minutes} min target_"
    )
    lines.append(
        "_Raw material for in-session synthesis — full per-project exec "
        "summaries + deterministic metrics. Not a finished doc._"
    )
    lines.append("")
    # Tenant-level deterministic metrics (capacity binding, cross-cutting
    # decisions, tenant hours) — reuse the cross-cutting renderer verbatim.
    lines.extend(_render_cross_cutting(result))
    lines.append("")
    # Milestone / urgent counts — read straight from result.
    mc = result.milestone_counts
    uc = result.urgent_counts
    lines.append(
        "**Milestones:** "
        f"{mc.get('total', 0)} total · {mc.get('fetched', 0)} fetched · "
        f"{mc.get('errored', 0)} errored"
    )
    lines.append(
        "**Urgent flags:** "
        + " · ".join(f"{k} {v}" for k, v in uc.items())
    )
    lines.append("")
    lines.append("---")
    lines.append("")
    for account_name, blocks in result.blocks_by_account.items():
        lines.extend(
            _render_account_section(
                account_name, blocks, project_renderer=_render_bundle_project_block
            )
        )
        lines.append("---")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ──────────────────────────────────────────────────────────────────────
#  Top-level entry point
# ──────────────────────────────────────────────────────────────────────


def _make_supabase_client(config: TenantConfig):
    """Build a Supabase client for MC-2 reads. Returns None on missing creds.

    Delegates env resolution to ``sync_mc2._load_supabase_creds`` so both
    ``cp sync`` and ``cp prep-planning`` use the same precedence rules:
    os.environ first, then ``<mc-2 clone>/backend/.env`` via the tenant's
    ``[local-repos]`` config. Without this, ``cp prep-planning`` ran from
    a fresh shell sees os.environ only, silently fails to resolve any
    project's clickup_list_id, and renders "(ClickUp list not set)" for
    every project — killing the v0.15 Forward Calendar feature.
    """
    from cp_engine import mc2_db

    client = mc2_db.get_client(config, required=False)
    if client is None:
        log.info("Supabase env not set; ClickUp list resolution disabled.")
    return client


def build_planning_result(
    config: TenantConfig,
    projects: tuple[ProjectState, ...],
    *,
    today: date,
    project_filter: tuple[str, ...] | None = None,
    tenant_hours_last_week: dict[str, int] | None = None,
    supabase_client=None,
    clickup_token: str | None = None,
    clickup_client: httpx.Client | None = None,
    list_id_lookup: dict[str, str] | None = None,
    clickup_task_ids: dict[str, str] | None = None,
    sweep_llm: Callable[[str], str] | None = None,
    planned_allocations=None,
) -> PlanningResult:
    """Build a full PlanningResult. Pure-ish: passes all dependencies in.

    Args:
        config: tenant config (root path).
        projects: ProjectState tuple from MC-2 (engagements + initiatives).
        today: anchor date for the planned sprint.
        project_filter: restrict to these codes (None = all active).
        tenant_hours_last_week: name → hours dict; rendered in the strip.
        supabase_client: MC-2 read client for clickup_list_id resolution.
            None = silent skip (list_id_override fills the gap in tests).
        clickup_token: explicit token. Falls back to env CLICKUP_API_TOKEN.
        clickup_client: shared httpx.Client. None = a new client per fetch.
        list_id_lookup: optional pre-fetched code → list_id map. Tests use
            this to avoid mocking ``_resolve_proposal_project``.
        clickup_task_ids: cp_ask_hash → clickup_task_id map for bridging-
            period dedupe of sprint-file open asks against ClickUp
            client-asks. None or empty = no dedupe.
        sweep_llm: opt-in whole-project sweep synthesizer (Project Spine
            slice 3, Phase B). None (default) = no sweep, no LLM call, the
            fast path is unchanged. When provided, each project's block gets
            a best-effort sweep synthesis attached (failures log + continue).
        planned_allocations: the PLANNING week's ``state.WeeklyAllocations``
            (issue #16). Drives ``tenant_hours_planned`` and the
            planned-allocations capacity-binding basis; None or empty falls
            back to the owner-of-record count (labeled as such).
    """
    list_id_lookup = list_id_lookup or {}

    active = tuple(filter_active(projects))
    if project_filter:
        wanted = {c.lower() for c in project_filter}
        active = tuple(p for p in active if p.code.lower() in wanted)
    # Sort like agenda.py does: scope then code, alphabetical.
    active_sorted = tuple(
        sorted(active, key=lambda p: (scope_for(p.company_kind), p.code))
    )

    week_iso = current_sprint_week_iso(to_datetime(today))
    week_start_iso, week_end_iso = sprint_week_dates(to_datetime(today))
    week_dates_label = (
        f"{short_iso_date(week_start_iso)} – {short_iso_date(week_end_iso)}"
    )

    blocks: list[ProjectPlanningBlock] = []
    errors: list[str] = []
    fetched = 0
    errored = 0
    total = 0
    # Auth failures (missing/invalid CLICKUP_API_TOKEN) hit every project
    # the same way, so collapsing to a single tenant-wide ``errors`` entry
    # keeps ``--summary`` legible. Per-project blocks still carry the full
    # ``fetch_error`` string for the renderer's "could not fetch" branch.
    seen_auth_error = False
    for p in active_sorted:
        list_id = list_id_lookup.get(p.code)
        block = build_project_block(
            p,
            config=config,
            supabase_client=supabase_client,
            today=today,
            week_iso=week_iso,
            clickup_client=clickup_client,
            clickup_token=clickup_token,
            list_id_override=list_id,
            clickup_task_ids=clickup_task_ids,
            sweep_llm=sweep_llm,
        )
        total += len(block.milestones) + len(block.client_asks)
        # ``no_clickup_list`` and ``no_milestones_tagged`` are legitimate
        # empty-states, not fetch failures — don't count them as errored or
        # bubble them to ``result.errors``. ``no_milestones_tagged`` still
        # counts as a successful fetch (the request itself succeeded —
        # the list was just empty); ``no_clickup_list`` skipped the fetch
        # entirely so it's neither fetched nor errored. Anything else
        # (network errors, 4xx/5xx) is a real fetch failure.
        if block.fetch_error and block.fetch_error not in (
            "no_clickup_list",
            "no_milestones_tagged",
        ):
            errored += 1
            # Auth failures dedupe to one tenant-wide entry; everything
            # else (network errors, 4xx/5xx other than 401/403) appends
            # per-project as before.
            if _is_auth_error(block.fetch_error):
                if not seen_auth_error:
                    errors.append(
                        "ClickUp auth failed for all projects: "
                        "check CLICKUP_API_TOKEN"
                    )
                    seen_auth_error = True
            else:
                errors.append(f"{p.code}: {block.fetch_error}")
        elif block.fetch_error is None or block.fetch_error == "no_milestones_tagged":
            fetched += 1
        blocks.append(block)

    blocks_by_account = _group_by_account(tuple(blocks))

    # Urgent counts: tallied per rule type from _detect_urgent results.
    urgent_counts = {
        "slip_risk": 0,
        "decision_due": 0,
        "past_due_ask": 0,
        "escalated_risk": 0,
    }
    for block in blocks:
        for item in block.urgent:
            t = item.get("type")
            if t in urgent_counts:
                urgent_counts[t] += 1

    # Task 8: cross-cutting capacity binding + partner-owed decisions.
    # Binding basis (issue #16 follow-through): planning-week allocations
    # when they exist; owner-of-record count as the labeled fallback.
    tenant_hours_planned: dict[str, int] = {}
    if planned_allocations is not None:
        for r in planned_allocations.rollup or ():
            name = (r.person_name or "").split()
            if name:
                tenant_hours_planned[name[0]] = int(round(r.total_hours))
    if planned_allocations is not None and (
        planned_allocations.rollup or planned_allocations.by_project
    ):
        capacity_binding: CapacityBinding = {
            "basis": "planned_allocations",
            "owners": _detect_capacity_binding_planned(planned_allocations),
        }
    else:
        capacity_binding = {
            "basis": "owner_of_record",
            "owners": list(_detect_capacity_binding(active_sorted)),
        }
    (
        cross_cutting_decisions,
        cross_cutting_errors,
        stale_count,
        undated_count,
    ) = _load_cross_cutting_decisions(config.root, today=today)
    errors.extend(cross_cutting_errors)

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    return PlanningResult(
        week_iso=week_iso,
        week_dates=week_dates_label,
        project_count=len(active_sorted),
        estimated_minutes=60,
        tenant_hours_last_week=tenant_hours_last_week or {},
        tenant_hours_planned=tenant_hours_planned,
        blocks_by_account=blocks_by_account,
        milestone_counts={
            "total": total,
            "fetched": fetched,
            "errored": errored,
        },
        urgent_counts=urgent_counts,
        capacity_binding=capacity_binding,
        cross_cutting_decisions=cross_cutting_decisions,
        cross_cutting_decisions_stale_count=stale_count,
        cross_cutting_decisions_undated_count=undated_count,
        errors=errors,
        generated_at=generated_at,
    )


def render_planning_doc(
    config: TenantConfig,
    projects: tuple[ProjectState, ...],
    *,
    today: date,
    project_filter: tuple[str, ...] | None = None,
    tenant_hours_last_week: dict[str, int] | None = None,
    supabase_client=None,
    clickup_token: str | None = None,
    list_id_lookup: dict[str, str] | None = None,
    clickup_task_ids: dict[str, str] | None = None,
    planned_allocations=None,
    sweep_llm: Callable[[str], str] | None = None,
) -> str:
    """Top-level entry: assemble + render the planning doc as markdown."""
    # Share one httpx.Client across all per-project fetches.
    client: httpx.Client | None = None
    if clickup_token or _clickup_token():
        client = httpx.Client(timeout=_CLICKUP_TIMEOUT)
    try:
        result = build_planning_result(
            config,
            projects,
            today=today,
            project_filter=project_filter,
            tenant_hours_last_week=tenant_hours_last_week,
            supabase_client=supabase_client,
            clickup_token=clickup_token,
            clickup_client=client,
            list_id_lookup=list_id_lookup,
            clickup_task_ids=clickup_task_ids,
            sweep_llm=sweep_llm,
            planned_allocations=planned_allocations,
        )
    finally:
        if client is not None:
            client.close()
    return render_planning_doc_markdown(result)


def render_planning_summary(
    config: TenantConfig,
    projects: tuple[ProjectState, ...],
    *,
    today: date,
    project_filter: tuple[str, ...] | None = None,
    tenant_hours_last_week: dict[str, int] | None = None,
    supabase_client=None,
    clickup_token: str | None = None,
    list_id_lookup: dict[str, str] | None = None,
    clickup_task_ids: dict[str, str] | None = None,
    planned_allocations=None,
) -> str:
    """Return the summary JSON as a string (matches ``--summary`` mode)."""
    client: httpx.Client | None = None
    if clickup_token or _clickup_token():
        client = httpx.Client(timeout=_CLICKUP_TIMEOUT)
    try:
        result = build_planning_result(
            config,
            projects,
            today=today,
            project_filter=project_filter,
            tenant_hours_last_week=tenant_hours_last_week,
            supabase_client=supabase_client,
            clickup_token=clickup_token,
            clickup_client=client,
            list_id_lookup=list_id_lookup,
            clickup_task_ids=clickup_task_ids,
            planned_allocations=planned_allocations,
        )
    finally:
        if client is not None:
            client.close()
    return json.dumps(result.to_summary_dict(), indent=2)


def render_planning_bundle_doc(
    config: TenantConfig,
    projects: tuple[ProjectState, ...],
    *,
    today: date,
    project_filter: tuple[str, ...] | None = None,
    tenant_hours_last_week: dict[str, int] | None = None,
    supabase_client=None,
    clickup_token: str | None = None,
    list_id_lookup: dict[str, str] | None = None,
    clickup_task_ids: dict[str, str] | None = None,
    planned_allocations=None,
) -> str:
    """Assemble + render the model-facing planning bundle as markdown.

    Mirrors ``render_planning_doc`` minus ``sweep_llm`` — the bundle emits
    raw material for in-session synthesis and does not run the per-project
    spine sweep (keep it simple + free).
    """
    client: httpx.Client | None = None
    if clickup_token or _clickup_token():
        client = httpx.Client(timeout=_CLICKUP_TIMEOUT)
    try:
        result = build_planning_result(
            config,
            projects,
            today=today,
            project_filter=project_filter,
            tenant_hours_last_week=tenant_hours_last_week,
            supabase_client=supabase_client,
            clickup_token=clickup_token,
            clickup_client=client,
            list_id_lookup=list_id_lookup,
            clickup_task_ids=clickup_task_ids,
            planned_allocations=planned_allocations,
        )
    finally:
        if client is not None:
            client.close()
    return render_planning_bundle(result)
