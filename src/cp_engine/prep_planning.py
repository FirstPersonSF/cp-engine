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
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

import httpx

from cp_engine.agenda import (
    WeeklyDecision,
    _filter_active,
    _short_iso_date,
    _to_datetime,
    parse_weekly_decisions,
)
from cp_engine.config import TenantConfig
from cp_engine.sprints import (
    _bullets,
    _parse_bracketed_bullet,
    _subsection,
    current_sprint_week_iso,
    sprint_week_dates,
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


@dataclass
class ProjectPlanningBlock:
    """Per-project rendered content."""

    project: ProjectState
    quick_resume_line: str | None
    milestones: tuple[Milestone, ...]
    client_asks: tuple[Milestone, ...]
    sprint_open_asks: tuple[SprintAsk, ...]
    urgent: tuple[dict, ...]  # flags from _detect_urgent (may be empty)
    fetch_error: str | None  # non-None when ClickUp fetch failed


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
    # Task 8: capacity-binding owners + cross-cutting decisions partners owe
    # each other. Populated by build_planning_result; consumed by
    # _render_cross_cutting. Both default to empty so the renderer can
    # safely skip the corresponding sub-blocks.
    capacity_binding: tuple[tuple[str, int], ...] = ()
    cross_cutting_decisions: tuple[WeeklyDecision, ...] = ()
    errors: list[str] = field(default_factory=list)
    generated_at: str = ""

    def to_summary_dict(self) -> dict:
        return {
            "week_iso": self.week_iso,
            "week_dates": self.week_dates,
            "project_count": self.project_count,
            "estimated_minutes": self.estimated_minutes,
            "tenant_hours_last_week": self.tenant_hours_last_week,
            "milestone_counts": self.milestone_counts,
            "urgent_counts": self.urgent_counts,
            "capacity_binding": [
                {"owner": name, "count": count}
                for name, count in self.capacity_binding
            ],
            "cross_cutting_decisions_count": len(self.cross_cutting_decisions),
            "errors": self.errors,
        }


# ──────────────────────────────────────────────────────────────────────
#  ClickUp REST client
# ──────────────────────────────────────────────────────────────────────


_CLICKUP_BASE = "https://api.clickup.com/api/v2"
_CLICKUP_TIMEOUT = 15.0


def _clickup_token() -> str | None:
    """Read the ClickUp Personal API Token from ``CLICKUP_API_TOKEN``."""
    return os.environ.get("CLICKUP_API_TOKEN")


def _fetch_clickup_milestones(
    list_id: str,
    *,
    tag: str = "milestone",
    token: str | None = None,
    client: httpx.Client | None = None,
) -> list[dict]:
    """Fetch tasks tagged ``tag`` from ClickUp list ``list_id``.

    Raises ``RuntimeError`` on any 4xx/5xx or network error so the caller
    can degrade per-project. The renderer wraps this in a try/except and
    surfaces the error as a "could not fetch" placeholder.
    """
    if token is None:
        token = _clickup_token()
    if not token:
        raise RuntimeError("CLICKUP_API_TOKEN not set")

    headers = {"Authorization": token}
    params: list[tuple[str, str]] = [
        ("tags[]", tag),
        ("include_closed", "false"),
        ("subtasks", "true"),
    ]
    url = f"{_CLICKUP_BASE}/list/{list_id}/task"

    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=_CLICKUP_TIMEOUT)
    try:
        resp = client.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"ClickUp network error: {exc}") from exc
    finally:
        if owns_client:
            client.close()

    if resp.status_code >= 400:
        raise RuntimeError(
            f"ClickUp returned {resp.status_code} for list {list_id}: "
            f"{resp.text[:200]}"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise RuntimeError(f"ClickUp returned non-JSON: {exc}") from exc
    return data.get("tasks") or []


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
        date=_from_clickup_timestamp(task.get("due_date")),
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


_QR_CURRENT_RE = re.compile(
    r"\*\*Current work:\*\*\s*(?P<text>.+?)(?=\n\s*\*\*|\n##|\Z)",
    re.DOTALL,
)


def _extract_current_work(cp_md_body: str) -> str | None:
    """Pull the ``**Current work:**`` line from a project's cp.md Quick Resume."""
    m = _QR_CURRENT_RE.search(cp_md_body)
    if not m:
        return None
    text = m.group("text").strip()
    # Drop the engine placeholder so an unfilled QR doesn't surface as content.
    if not text or "_<" in text:
        return None
    return text


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


# Lenient section-header regex: matches `## <heading>` even when the
# heading carries a trailing modifier (e.g. `## Horizon — 4–8 weeks out`).
# Note: ``sprints._section_body`` requires the heading line to be exactly
# `## <heading>\s*$`, which doesn't match the real sprint scaffold whose
# Horizon heading carries the `— 4–8 weeks out` suffix. We use this looser
# matcher just for urgent detection so Rule 2 / Rule 4 work on real files.
# Non-greedy on the suffix and the body so DOTALL doesn't eat across
# sections — see test debugging notes in this PR for the gotcha.
_LOOSE_SECTION_RE = "^## {heading}(?:\\s+—\\s+.*?)?\\s*$(.*?)(?=^## |\\Z)"


# TODO(v0.16): backport lenient matcher to sprints._section_body so agenda
# stops silently dropping decisions-due under the suffixed Horizon heading.
def _loose_section_body(body: str, heading: str) -> str:
    pattern = re.compile(
        _LOOSE_SECTION_RE.format(heading=re.escape(heading)),
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(body)
    return m.group(1) if m else ""


def _parse_decisions_due_from_body(body: str) -> tuple[dict, ...]:
    """Pull `## Horizon → ### Decisions due` items from a raw sprint file body."""
    horizon_section = _loose_section_body(body, "Horizon")
    decisions_sub = _subsection(horizon_section, "Decisions due")
    out: list[dict] = []
    for first, _cont in _bullets(decisions_sub):
        parsed = _parse_bracketed_bullet(first)
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
    """Pull `## Dependencies & risks` items from a raw sprint file body."""
    risks_section = _loose_section_body(body, "Dependencies & risks")
    out: list[dict] = []
    for first, _cont in _bullets(risks_section):
        parsed = _parse_bracketed_bullet(first)
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
) -> ProjectPlanningBlock:
    """Assemble all per-project rendering data."""
    # Quick Resume line from the project's cp.md
    scope = account_scope_for(project)
    slug = dir_slug(project.code, project.name)
    cp_md_path = config.root / scope / slug / "cp.md"
    current_work: str | None = None
    if cp_md_path.is_file():
        try:
            current_work = _extract_current_work(
                cp_md_path.read_text(encoding="utf-8")
            )
        except OSError as exc:
            log.warning("cp.md read failed for %s: %s", project.code, exc)

    # Sprint-file open asks (bridging period — not yet in ClickUp).
    sprint_file_path = (
        config.root / "sprints" / week_iso / f"{project.code}.md"
    )
    sprint_asks = _parse_sprint_open_asks(sprint_file_path)
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
        fetch_error = "no clickup_list_id"
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

    return ProjectPlanningBlock(
        project=project,
        quick_resume_line=current_work,
        milestones=milestones,
        client_asks=client_asks,
        sprint_open_asks=sprint_asks,
        urgent=tuple(urgent_list),
        fetch_error=fetch_error,
    )


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
) -> str:
    """One-line ``**Active:** N · **Last sprint:** ...`` strip.

    This piece IS in Task 6's scope. Task 8 adds escalated-risk and
    past-due-ask aggregates around it.
    """
    if tenant_hours:
        ordered = sorted(tenant_hours.items(), key=lambda kv: -kv[1])
        hours_str = ", ".join(f"{name} {h}h" for name, h in ordered)
    else:
        hours_str = "—"
    return f"**Active:** {project_count} · **Last sprint:** {hours_str}"


# ──────────────────────────────────────────────────────────────────────
#  Task 8: cross-cutting detection (capacity binding + partner decisions)
# ──────────────────────────────────────────────────────────────────────


# Owners with >= this many projects of record become "capacity binding".
# The retro pegged 5 as the floor at which an owner stops being able to
# context-switch cleanly between projects in a single sprint.
_CAPACITY_BINDING_FLOOR = 5

# Cross-cutting decisions are surfaced when their parser date is within
# this many days of `today` — the weekly-cp.md section header itself
# scopes to "last 4 weeks", and we mirror that bound here so old hand-
# written entries don't bleed forward indefinitely.
_CROSS_CUTTING_LOOKBACK_DAYS = 28

# Decisions tagged with this inline marker have already been resolved and
# should drop out of the planning surface. Matches forms like
# "[decided: 2026-05-30]" or "[decided yes]".
_DECIDED_MARKER_RE = re.compile(r"\[decided[:\s][^\]]*\]", re.IGNORECASE)


def _detect_capacity_binding(
    projects: tuple[ProjectState, ...],
) -> tuple[tuple[str, int], ...]:
    """Owners carrying >= ``_CAPACITY_BINDING_FLOOR`` projects of record.

    Returns ``(name, count)`` pairs ordered by count desc, then name asc
    for stable rendering. ``"—"`` and ``None`` owners are ignored so an
    unassigned-owner cluster never trips the binding flag.

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
    binding = [
        (name, count)
        for name, count in counts.most_common()
        if count >= _CAPACITY_BINDING_FLOOR
    ]
    # most_common preserves insertion order for ties; re-sort to break
    # ties alphabetically so output is deterministic across runs.
    binding.sort(key=lambda nc: (-nc[1], nc[0].lower()))
    return tuple(binding)


def _is_resolved_decision(text: str) -> bool:
    """True if the decision text carries a ``[decided: ...]`` marker."""
    return bool(_DECIDED_MARKER_RE.search(text))


def _load_cross_cutting_decisions(
    tenant_root: Path,
    *,
    today: date,
    lookback_days: int = _CROSS_CUTTING_LOOKBACK_DAYS,
) -> tuple[WeeklyDecision, ...]:
    """Read ``weekly-cp.md`` and return decisions still owed across partners.

    Filters:
      - Section absent or empty → ``()``.
      - Entry's date older than ``lookback_days`` → dropped.
      - Entry text contains a ``[decided: ...]`` marker → dropped.

    Reuses ``agenda.parse_weekly_decisions`` so the parsing contract is
    shared with ``cp prep-agenda``. The order returned mirrors the
    handwritten numbering in ``weekly-cp.md`` (newest entries at top).
    """
    weekly_path = tenant_root / "weekly-cp.md"
    if not weekly_path.is_file():
        return ()
    body = weekly_path.read_text(encoding="utf-8")
    decisions = parse_weekly_decisions(body)
    if not decisions:
        return ()
    cutoff = today - timedelta(days=lookback_days)
    out: list[WeeklyDecision] = []
    for d in decisions:
        try:
            d_date = date.fromisoformat(d.date)
        except ValueError:
            # Malformed date — surface the entry rather than silently
            # dropping it; partners can spot the bad format.
            d_date = today
        if d_date < cutoff:
            continue
        if _is_resolved_decision(d.text):
            continue
        out.append(d)
    return tuple(out)


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
        _render_tenant_strip(result.project_count, result.tenant_hours_last_week)
    )
    out.append("")
    out.append("## Cross-cutting (read before walking projects)")

    binding = result.capacity_binding
    decisions = result.cross_cutting_decisions

    if not binding and not decisions:
        out.append("_(no cross-cutting signals this sprint)_")
        return out

    if binding:
        out.append("**Capacity binding constraints:**")
        for name, count in binding:
            suffix = "" if count == 1 else "s"
            out.append(
                f"- **{name}** — owner-of-record on {count} project{suffix}"
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
    if block.fetch_error == "no clickup_list_id":
        return [
            "**Forward calendar:**",
            "_(ClickUp list not set — milestones not tracked)_",
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
        date_short = _short_iso_date(m["date"]) or m["date"]
        owner = m.get("owner") or "—"
        confidence = m.get("confidence") or "medium"
        deliverable = m.get("deliverable") or "(untitled)"
        deps = m.get("depends_on") or []
        meta = [f"{owner}", f"{confidence} confidence"]
        if deps:
            meta.append(f"depends_on: {', '.join(deps)}")
        out.append(f"- {date_short} — {deliverable} ({'; '.join(meta)})")
    return out


def _render_commitments_table(block: ProjectPlanningBlock) -> list[str]:
    """Two-way commitments table: us→them (milestones) + them→us (asks).

    The "to/from" mapping:
        Milestone (us → them) — Who = owner, To = client, By = date.
        ClickUp client-ask    — Who = ClickUp owner (or "client" fallback),
                                To = "us", By = date.
        Sprint-file open ask  — Who = ask's "who" field, To/From inferred,
                                flagged "(sprint file)" so reviewers promote.
    """
    rows: list[tuple[str, str, str, str]] = []  # (Who, Owes what, To, By)
    for m in block.milestones:
        date_str = _short_iso_date(m.get("date")) or "—"
        rows.append(
            (
                m.get("owner") or "—",
                m.get("deliverable") or "(untitled)",
                "client",
                date_str,
            )
        )
    for ca in block.client_asks:
        date_str = _short_iso_date(ca.get("date")) or "—"
        # Client-asks: the "owner" assigned in ClickUp is who owes us the
        # answer — typically the client-side person. Surface as "external".
        who = ca.get("owner") or "client"
        rows.append((who, ca.get("deliverable") or "(untitled)", "us", date_str))
    for sa in block.sprint_open_asks:
        date_str = _short_iso_date(sa["by"]) if sa.get("by") else "—"
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
        out.append(f"| {who} | {what} | {to} | {by} |")
    return out


def _render_project_block(block: ProjectPlanningBlock) -> list[str]:
    """Render one project's section: urgent → Where → Forward → Commitments."""
    p = block.project
    owner = p.owner or "—"
    out = [f"### {p.code} {p.name} — {owner}", ""]

    if block.urgent:
        out.append("**Urgent:**")
        for item in block.urgent:
            severity = item.get("severity", "warn")
            marker = "ALERT" if severity == "alert" else "WARN"
            out.append(f"- [{marker}] {item.get('text', '(no text)')}")
        out.append("")
    else:
        out.append("_(no urgent items)_")
        out.append("")

    if block.quick_resume_line:
        out.append(f"**Where:** {block.quick_resume_line}")
    else:
        out.append("**Where:** _(Quick Resume empty — fill in before sprint planning)_")
    out.append("")

    out.extend(_render_forward_calendar(block))
    out.append("")
    out.extend(_render_commitments_table(block))
    out.append("")
    return out


def _render_account_section(
    account_name: str,
    blocks: list[ProjectPlanningBlock],
) -> list[str]:
    """Render one account's section: header + every project block."""
    out = [f"## {account_name} ({len(blocks)} projects)", ""]
    for i, block in enumerate(blocks):
        if i:
            out.append("---")
            out.append("")
        out.extend(_render_project_block(block))
    return out


def render_planning_doc_markdown(result: PlanningResult) -> str:
    """Render the final markdown document."""
    lines: list[str] = []
    lines.append(
        f"# Sprint {result.week_iso} Planning — {result.week_dates}"
    )
    lines.append(
        f"_Generated {result.generated_at} by `cp prep-planning` · "
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


# ──────────────────────────────────────────────────────────────────────
#  Top-level entry point
# ──────────────────────────────────────────────────────────────────────


def _make_supabase_client(config: TenantConfig):
    """Build a Supabase client for MC-2 reads. Returns None on missing creds.

    Mirrors ``cli._fetch_clickup_task_ids_for_hashes``: silent degrade is OK
    because every per-project ClickUp fetch already handles a missing
    list_id by rendering a "not set" placeholder.
    """
    try:
        from supabase import create_client
    except ImportError:  # pragma: no cover
        return None
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url or not key:
        log.info("Supabase env not set; ClickUp list resolution disabled.")
        return None
    return create_client(url, key)


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
    """
    list_id_lookup = list_id_lookup or {}

    active = tuple(_filter_active(projects))
    if project_filter:
        wanted = {c.lower() for c in project_filter}
        active = tuple(p for p in active if p.code.lower() in wanted)
    # Sort like agenda.py does: scope then code, alphabetical.
    active_sorted = tuple(
        sorted(active, key=lambda p: (scope_for(p.company_kind), p.code))
    )

    week_iso = current_sprint_week_iso(_to_datetime(today))
    week_start_iso, week_end_iso = sprint_week_dates(_to_datetime(today))
    week_dates_label = (
        f"{_short_iso_date(week_start_iso)} – {_short_iso_date(week_end_iso)}"
    )

    blocks: list[ProjectPlanningBlock] = []
    errors: list[str] = []
    fetched = 0
    errored = 0
    total = 0
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
        )
        total += len(block.milestones) + len(block.client_asks)
        if block.fetch_error and block.fetch_error != "no clickup_list_id":
            errored += 1
            errors.append(f"{p.code}: {block.fetch_error}")
        elif block.fetch_error is None:
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
    capacity_binding = _detect_capacity_binding(active_sorted)
    cross_cutting_decisions = _load_cross_cutting_decisions(
        config.root, today=today
    )

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    return PlanningResult(
        week_iso=week_iso,
        week_dates=week_dates_label,
        project_count=len(active_sorted),
        estimated_minutes=60,
        tenant_hours_last_week=tenant_hours_last_week or {},
        blocks_by_account=blocks_by_account,
        milestone_counts={
            "total": total,
            "fetched": fetched,
            "errored": errored,
        },
        urgent_counts=urgent_counts,
        capacity_binding=capacity_binding,
        cross_cutting_decisions=cross_cutting_decisions,
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
        )
    finally:
        if client is not None:
            client.close()
    return json.dumps(result.to_summary_dict(), indent=2)
