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

Boundary with Task 7 / Task 8:
    ``_detect_urgent`` is stubbed (returns ``[]``) — Task 7 fills the
    rules in. The renderer always calls it and skips the urgent block when
    the list is empty, so dropping in real detection later doesn't touch
    rendering code.

    ``_render_cross_cutting`` returns only the tenant strip today. Task 8
    layers capacity-binding analysis + ``weekly-cp.md`` cross-cutting
    decisions on top.

The CLI entry point (``cp prep-planning``) lives in cli.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TypedDict

import httpx

from cp_engine.agenda import (
    _filter_active,
    _short_iso_date,
    _to_datetime,
)
from cp_engine.config import TenantConfig
from cp_engine.sprints import (
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
    """Normalized ClickUp milestone task. Task 7 relies on these keys.

    ``task_type`` is either ``"milestone"`` or ``"client_ask"`` — the
    same module fetches both shapes from ClickUp; the table renderer
    routes by ``task_type``.
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
    from_party: str  # client_ask only


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
    urgent: tuple[dict, ...]  # always () until Task 7
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


def _detect_urgent(
    project: ProjectState,
    milestones: tuple[Milestone, ...],
    sprint_asks: tuple[SprintAsk, ...],
) -> list[dict]:
    """STUB for Task 7. Returns ``[]`` until urgent-flag rules are implemented.

    The renderer calls this and only emits an urgent block when the list is
    non-empty, so dropping in Task 7's real detection here doesn't touch
    rendering code.

    Task 7 will surface signals like:
      - slip risk: milestone due in <= N days with low confidence
      - decision due: depends_on links to an unresolved upstream
      - past-due ask: sprint_asks with ``by`` < today
      - escalated risk: pulled from the sprint file's risk section

    Each returned dict carries at minimum:
        {"type": str, "text": str, "severity": "warn"|"alert"}
    """
    return []


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

    urgent_list = _detect_urgent(project, milestones, sprint_asks)

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


def _render_cross_cutting(
    result: PlanningResult,
) -> list[str]:
    """STUB for Task 8. Returns only the tenant strip today.

    Task 8 will add capacity-binding analysis (e.g. "Drew has 8 milestones
    landing this week against 24h headroom") and cross-cutting decisions
    pulled from ``weekly-cp.md``. Until then this is the headline strip
    plus a one-line placeholder so Task 8 has a clear anchor to grow into.
    """
    out = ["## Tenant strip"]
    out.append(_render_tenant_strip(result.project_count, result.tenant_hours_last_week))
    out.append("")
    out.append("## Cross-cutting (read before walking projects)")
    out.append("_(stub — Task 8 layers capacity-binding analysis here)_")
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
        ClickUp client-ask    — Who = from_party (client), To = "us", By = date.
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
        date_str = _short_iso_date(sa.get("by")) or "—" if sa.get("by") else "—"
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

    # Urgent counts are zero today (Task 7 stub returns []).
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
