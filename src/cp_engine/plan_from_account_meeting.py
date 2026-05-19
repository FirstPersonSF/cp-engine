"""Generate a `cp ingest` plan from an account-meeting transcript.

This is the engine half of Phase D.4 (account meetings). The cp-engine-
webhook's /api/auto-ingest-account endpoint calls into `generate_account_plan()`
when a Fathom meeting has been tagged as an account meeting for a specific
company.

Difference from `plan_from_transcript`: instead of one project's context,
the prompt loads ALL active projects for the company (engagements for
client companies, initiatives for self-fpsf/self-canonic) and asks Claude
to route content per-project in a single pass. Output is a multi-project
plan that the existing `cp_engine.ingest.execute_plan` can consume,
plus an `account_summary` block (paragraph) and the existing
`account_decisions` block (one-liners).

See `cp/docs/plans/2026-05-14-account-meetings.md` for the full design.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from cp_engine.config import TenantConfig
from cp_engine.ingest import IngestPlanError, _validate_plan
from cp_engine.plan_from_transcript import (
    _MAX_PROJECT_CP_CHARS,
    _call_claude,
    _extract_yaml,
    _find_project_dir,
    _is_engagement_code,
    _load_recent_account_decisions,
)
from cp_engine.sprints import current_sprint_week_iso
from cp_engine.state import ProjectState

# Conservative ceiling for the multi-project context block. Each project's
# cp.md gets a hard truncation; we don't try to fit ALL sprint files in
# the prompt (they'd blow past Anthropic's context for an account with
# 6+ active projects).
_MAX_PER_PROJECT_CP_CHARS = 2500
_MAX_TRANSCRIPT_CHARS = 60_000


class AccountPlanError(Exception):
    """Raised when Claude fails to return a valid account-meeting plan."""


@dataclass
class GeneratedAccountPlan:
    plan: dict
    raw_response: str
    company_code: str
    meeting_id: str
    project_codes: tuple[str, ...]
    model: str


def generate_account_plan(
    *,
    config: TenantConfig,
    company_code: str,
    meeting_id: str,
    transcript_text: str,
    active_projects: list[ProjectState],
    week_iso: str | None = None,
    model: str = "claude-opus-4-7",
    api_key: str | None = None,
) -> GeneratedAccountPlan:
    """Generate the account-meeting plan via one Claude call.

    `active_projects` is the list of ProjectStates the webhook fetched
    via _list_active_for_company — engagements for client companies,
    initiatives for self-fpsf/self-canonic. Each gets its compact context
    in the prompt; Claude routes per-project verbs based on transcript
    relevance, then emits one tenant-wide account_summary paragraph.

    `week_iso` defaults to the current sprint week if not passed; account
    meetings always run live so we don't usually need to override it.
    """
    if not active_projects:
        raise AccountPlanError(
            f"company {company_code!r} has no active projects to route to"
        )

    week = week_iso or current_sprint_week_iso(datetime.now())

    # Truncate transcript at the same cap as plan_from_transcript so a
    # large account meeting doesn't blow the prompt budget.
    transcript = (
        transcript_text
        if len(transcript_text) <= _MAX_TRANSCRIPT_CHARS
        else transcript_text[:_MAX_TRANSCRIPT_CHARS]
        + "\n\n[... transcript truncated ...]\n"
    )

    project_block = _format_active_projects(config, active_projects)
    account_decisions_context = _load_recent_account_decisions(config.root)

    prompt = _build_account_prompt(
        company_code=company_code,
        company_label=_company_label(active_projects),
        week=week,
        active_projects_block=project_block,
        account_decisions_context=account_decisions_context,
        transcript=transcript,
        team=config.team,
    )

    response_text = _call_claude(prompt, model=model, api_key=api_key)
    yaml_text = _extract_yaml(response_text)

    try:
        plan = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise AccountPlanError(
            f"Claude returned non-YAML output: {exc}\n--- response ---\n{response_text[:500]}"
        ) from exc

    if not isinstance(plan, dict):
        raise AccountPlanError(
            f"Claude returned a non-mapping plan: {type(plan).__name__}"
        )

    # Inject company + week into account_summary if Claude omitted them.
    # The webhook always knows both — no need to depend on prompt
    # adherence for these two values (mirrors plan_from_slack pattern).
    summary = plan.get("account_summary")
    if isinstance(summary, dict):
        summary.setdefault("company", company_code.lower())
        summary.setdefault("week", week)
    elif isinstance(summary, list):
        for item in summary:
            if isinstance(item, dict):
                item.setdefault("company", company_code.lower())
                item.setdefault("week", week)

    # Same defensive injection for account_decisions: stamp the company
    # code so Claude doesn't need to know it.
    for ad in plan.get("account_decisions") or []:
        if isinstance(ad, dict):
            ad.setdefault("company", company_code.lower())

    try:
        _validate_plan(plan)
    except IngestPlanError as exc:
        raise AccountPlanError(f"plan failed validation: {exc}") from exc

    project_codes = tuple(p.code for p in active_projects)
    return GeneratedAccountPlan(
        plan=plan,
        raw_response=response_text,
        company_code=company_code,
        meeting_id=meeting_id,
        project_codes=project_codes,
        model=model,
    )


def _format_active_projects(
    config: TenantConfig, projects: list[ProjectState]
) -> str:
    """Render the active project list with each project's compact context."""
    parts: list[str] = []
    for p in projects:
        header = f"### `{p.code}` — {p.name}"
        if p.source == "initiative":
            header += " (initiative)"
        parts.append(header)
        if p.one_line_summary:
            parts.append(f"_{p.one_line_summary}_")
        # Pull a small slice of the project's cp.md for context.
        project_dir = _find_project_dir(config.root, p.code)
        if project_dir is not None:
            cp_md = project_dir / "cp.md"
            if cp_md.is_file():
                text = cp_md.read_text(encoding="utf-8")
                if len(text) > _MAX_PER_PROJECT_CP_CHARS:
                    text = (
                        text[:_MAX_PER_PROJECT_CP_CHARS]
                        + "\n[... cp.md truncated ...]\n"
                    )
                parts.append(text)
        parts.append("")  # blank line between projects
    return "\n".join(parts)


def _company_label(active_projects: list[ProjectState]) -> str:
    """Friendly label for the company. Pulled from the first project's
    company_name, or the company_code as fallback. Used in the prompt
    header for human-readability."""
    for p in active_projects:
        if p.company_name:
            return p.company_name
    return active_projects[0].company_code or "(unknown)"


def _build_account_prompt(
    *,
    company_code: str,
    company_label: str,
    week: str,
    active_projects_block: str,
    account_decisions_context: str,
    transcript: str,
    team: tuple[str, ...] = (),
) -> str:
    today = datetime.now().date().isoformat()
    if team:
        team_block = (
            "These names are INTERNAL TEAM MEMBERS, not stakeholders.\n"
            "Never add them as new entries to a `stakeholders` verb:\n"
            + ", ".join(team)
        )
    else:
        team_block = "(No team roster declared in tenant config.)"

    if account_decisions_context:
        decisions_block = (
            "### Recent account-level decisions (already in weekly-cp.md)\n\n"
            "Don't re-emit these as either project-level decisions OR new "
            "account_decisions; the system already knows them.\n\n"
            + account_decisions_context
        )
    else:
        decisions_block = ""

    return _PROMPT_TEMPLATE.format(
        today=today,
        week=week,
        company_code=company_code.lower(),
        company_label=company_label,
        active_projects_block=active_projects_block,
        decisions_block=decisions_block,
        team_block=team_block,
        transcript=transcript,
    )


_PROMPT_TEMPLATE = """\
You are extracting structured updates from an ACCOUNT-LEVEL meeting
transcript — a weekly sync that touches multiple projects under a
single client (or internal team). Your output is a YAML plan that
`cp ingest` will execute against the tenant.

Unlike a single-project meeting, you must ROUTE each piece of content
to whichever project it's actually about. The active project list for
this account is below; emit per-project verbs only when content clearly
relates to a specific project.

# Today
{today}

# Sprint week
{week}

# Account
{company_label} (canonical code: `{company_code}`)

# Internal team
{team_block}

# Active projects under this account

{active_projects_block}

{decisions_block}

# Schema you must produce

```yaml
transcript:
  source: fathom
  path: account-meeting

projects:                     # one entry per project that has content
  <project-code>:
    inbound:                  # things the client said/did to/at us
      - text: "..."
        date: "YYYY-MM-DD"
        who: "<who said it>"
    asks:                     # outstanding requests we made of the client
      - text: "..."
        who: "<who we're asking>"
        by: "YYYY-MM-DD"
        date: "YYYY-MM-DD"
    decisions:
      - text: "..."
        date: "YYYY-MM-DD"
        cross_cutting: false
    risks:
      - text: "..."
        severity: "watching"   # or "escalated", "dependency"
        category: "schedule"
        date: "YYYY-MM-DD"

account_summary:              # ONE entry — paragraph capturing the gestalt
  text: "..."                  # 60–150 words, narrative not bullet list
  # company + week are injected server-side; you don't need them

account_decisions:            # OPTIONAL — tenant-wide decisions
  - text: "..."
    date: "YYYY-MM-DD"
```

# Rules

1. **Route per-project, don't broadcast.** A bullet for `ggl-5168` should
   only land in `projects.ggl-5168`. If content mentions multiple
   projects, split it. If it's truly cross-cutting (affects all
   projects), put it in `account_summary` or `account_decisions`,
   not duplicated across N project entries.

2. **Don't over-extract.** A project mentioned in passing ("oh, and
   we should check in on 5151 next week") doesn't need a verb entry —
   that's account_summary material. Reserve project verbs for
   substantive content.

3. **The `account_summary` is REQUIRED.** Exactly one entry. Write a
   60–150 word paragraph capturing the gestalt of this account meeting:
   what got covered across all projects, dominant themes, status of
   the relationship. This goes in `weekly-cp.md` for partner-review
   visibility.

4. **`account_decisions` are TENANT-WIDE structured one-liners.**
   Examples: "All Google consultant invoices route through Brandon
   going forward." "Maria leaves the account 2026-05-31; transition
   plan to Geoff." Don't put project-specific decisions here — those
   go in the project's `decisions` verb.

5. **Decisions vs inbound.** Decision = commitment made in the meeting
   (who's doing what, by when). Inbound = information conveyed.
   Same as the single-project rule.

6. **Skip stakeholders entirely** for account meetings. The team
   roster is known; new external contacts almost never get introduced
   in a weekly sync.

7. **Don't duplicate what's already known.** The "Recent account-level
   decisions" list above is what the tenant already remembers. If the
   meeting restates one of those, skip it.

8. **Date fields:** use the meeting date if known, otherwise today
   ({today}). All dates as ISO YYYY-MM-DD.

9. **Quote-like fidelity.** Reflect what was said. No interpretation
   or speculation. Vague commitments aren't decisions.

10. **No empty lists.** Omit any project entry / verb that has no
    items. The `account_summary` is the only field that's always
    present.

# Output format

Respond with ONLY the YAML plan inside a single ```yaml fenced code
block. No preamble, no explanation, no postscript.

# Transcript

{transcript}
"""


def list_active_for_company(
    config: TenantConfig, company_code: str
) -> list[ProjectState]:
    """Return all active projects (engagements OR initiatives) for a company.

    Used by the cp-engine-webhook account-meeting endpoint to decide
    which projects to load context for. The discriminator is
    `companies.kind` (resolved by sync_mc2's company joins):
      - `client`        → active engagements (Deal | Open, not internal)
      - `self-fpsf`     → active initiatives (status='Active')
      - `self-canonic`  → active initiatives (status='Active')

    company_code is the canonical company code (e.g. 'GGL', '1PI', 'CNC').
    Lookup is case-insensitive.
    """
    from cp_engine.status import is_active_initiative_status, is_active_status
    from cp_engine.sync import _default_backend_factory

    backend = _default_backend_factory(config.sync.backend)
    all_projects = backend.read_projects(config)

    code_lc = (company_code or "").strip().lower()
    out: list[ProjectState] = []
    for p in all_projects:
        if not p.company_code or p.company_code.lower() != code_lc:
            continue
        if p.source == "engagement":
            if is_active_status(p.status) and not p.is_internal:
                out.append(p)
        elif p.source == "initiative":
            if is_active_initiative_status(p.status):
                out.append(p)
        # standalone repos are skipped — they're not assignment targets
        # for account meetings (they're either part of an initiative
        # or unowned).
    out.sort(key=lambda p: p.code)
    return out


# Phase D.5: tenant-scope sprint planning. Three valid scopes mapping
# to companies.kind values; each pulls a different active set.
_SCOPE_TO_KIND = {
    "1p": "client",         # all active client engagements
    "fpsf": "self-fpsf",    # all active FPSF initiatives
    "canonic": "self-canonic",  # all active Canonic initiatives
}

# Phase D.7: explicit-list scopes that don't map to a single company.kind.
# `storyos-mc` is Drew + Tony's standing weekly Canonic + Mission Control
# product/engineering sync — pairs StoryOS (Canonic initiative) with
# Mission Control (FPSF initiative). If you add a new explicit-list scope
# later, list its target initiative codes here; list_active_for_scope
# falls through to a code-allowlist lookup when the scope isn't in
# _SCOPE_TO_KIND.
_SCOPE_TO_EXPLICIT_CODES = {
    "storyos-mc": ("storyos", "mission-control"),
}

# Pseudo-company codes for the account_summary's `company` field. These
# don't match any real `companies.code` — they're scoped distinctly
# from per-account summaries to avoid hash collisions and to make the
# weekly-cp.md `## Account summaries` section readable.
_SCOPE_TO_PSEUDO_COMPANY = {
    "1p": "1p-clients",
    "fpsf": "fpsf-internal",
    "canonic": "canonic-internal",
    "storyos-mc": "storyos-mc",
}

_SCOPE_LABEL = {
    "1p": "1P (all active client engagements)",
    "fpsf": "First Person internal (all active FPSF initiatives)",
    "canonic": "Canonic internal (all active Canonic initiatives)",
    "storyos-mc": "StoryOS + Mission Control (Drew + Tony product/engineering sync)",
}


def list_active_for_scope(
    config: TenantConfig, scope: str
) -> list[ProjectState]:
    """Return all active projects for a sprint-planning scope.

    Two scope shapes:
      1. Kind-based (mapped via _SCOPE_TO_KIND):
         - '1p'       → all active client engagements (company.kind='client')
         - 'fpsf'     → all active initiatives under self-fpsf companies
         - 'canonic'  → all active initiatives under self-canonic companies
      2. Explicit-code (mapped via _SCOPE_TO_EXPLICIT_CODES):
         - 'storyos-mc' → fixed pair: StoryOS + Mission Control

    Sister function to `list_active_for_company` but the discriminator
    is the company kind OR an explicit code list. Used by the cp-engine-
    webhook /api/auto-ingest-sprint-planning endpoint.
    """
    from cp_engine.status import is_active_initiative_status, is_active_status
    from cp_engine.state import scope_for
    from cp_engine.sync import _default_backend_factory

    valid_scopes = set(_SCOPE_TO_KIND) | set(_SCOPE_TO_EXPLICIT_CODES)
    if scope not in valid_scopes:
        raise AccountPlanError(
            f"unknown sprint-planning scope {scope!r}; "
            f"expected one of {sorted(valid_scopes)}"
        )

    backend = _default_backend_factory(config.sync.backend)
    all_projects = backend.read_projects(config)

    # Explicit-code scopes: return only the listed project codes, in the
    # order they appear in the explicit list (preserves intent — e.g.
    # 'storyos-mc' renders StoryOS first because that's how the meeting
    # name reads).
    if scope in _SCOPE_TO_EXPLICIT_CODES:
        target_codes = _SCOPE_TO_EXPLICIT_CODES[scope]
        by_code = {p.code: p for p in all_projects}
        out: list[ProjectState] = []
        for code in target_codes:
            p = by_code.get(code)
            if p is None:
                # Skip silently — a project listed in an explicit scope
                # that's no longer in MC-2 shouldn't block the rest.
                continue
            # Apply the same activity check that the kind-based scopes
            # use, but be tolerant: explicit lists are curated by humans
            # who may want to keep On-hold initiatives in scope for a
            # bit longer.
            if p.source == "initiative" and is_active_initiative_status(p.status):
                out.append(p)
            elif p.source == "engagement" and is_active_status(p.status) and not p.is_internal:
                out.append(p)
        return out

    target_kind = _SCOPE_TO_KIND[scope]

    out: list[ProjectState] = []
    for p in all_projects:
        if p.company_kind != target_kind:
            continue
        if scope == "1p":
            # Client engagements: Deal | Open, not internal.
            if p.source == "engagement" and is_active_status(p.status) and not p.is_internal:
                out.append(p)
        else:
            # FPSF / Canonic: active initiatives only (skip standalone repos
            # — they're either initiative-linked or unowned and don't fit
            # the "what we're planning this sprint" frame).
            if p.source == "initiative" and is_active_initiative_status(p.status):
                out.append(p)
    out.sort(key=lambda p: (scope_for(p.company_kind), p.code))
    return out


# ──────────────────────────────────────────────────────────────────────
#  Phase D.5: tenant-scope sprint planning plan generator
# ──────────────────────────────────────────────────────────────────────


def generate_sprint_planning_plan(
    *,
    config: TenantConfig,
    scope: str,
    meeting_id: str,
    transcript_text: str,
    active_projects: list[ProjectState],
    week_iso: str | None = None,
    model: str = "claude-opus-4-7",
    api_key: str | None = None,
) -> GeneratedAccountPlan:
    """Generate a sprint-planning plan via one Claude call.

    Same shape as `generate_account_plan` but the prompt frames this
    as tenant-scope sprint planning — the meeting touches every active
    project under the named scope (1P, FPSF, or Canonic). The
    `account_summary` lands in weekly-cp.md tagged with a pseudo-
    company code (`1p-clients`, `fpsf-internal`, `canonic-internal`)
    so it's distinguishable from per-account summaries.
    """
    if scope not in _SCOPE_TO_PSEUDO_COMPANY:
        raise AccountPlanError(
            f"unknown sprint-planning scope {scope!r}; "
            f"expected one of {sorted(_SCOPE_TO_PSEUDO_COMPANY)}"
        )
    if not active_projects:
        raise AccountPlanError(
            f"scope {scope!r} has no active projects to route to"
        )

    week = week_iso or current_sprint_week_iso(datetime.now())
    pseudo_company = _SCOPE_TO_PSEUDO_COMPANY[scope]
    scope_label = _SCOPE_LABEL[scope]

    transcript = (
        transcript_text
        if len(transcript_text) <= _MAX_TRANSCRIPT_CHARS
        else transcript_text[:_MAX_TRANSCRIPT_CHARS]
        + "\n\n[... transcript truncated ...]\n"
    )

    project_block = _format_active_projects(config, active_projects)
    account_decisions_context = _load_recent_account_decisions(config.root)

    prompt = _build_sprint_planning_prompt(
        scope=scope,
        scope_label=scope_label,
        week=week,
        active_projects_block=project_block,
        account_decisions_context=account_decisions_context,
        transcript=transcript,
        team=config.team,
    )

    response_text = _call_claude(prompt, model=model, api_key=api_key)
    yaml_text = _extract_yaml(response_text)

    try:
        plan = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise AccountPlanError(
            f"Claude returned non-YAML output: {exc}\n--- response ---\n{response_text[:500]}"
        ) from exc

    if not isinstance(plan, dict):
        raise AccountPlanError(
            f"Claude returned a non-mapping plan: {type(plan).__name__}"
        )

    # Inject pseudo-company + week into account_summary defensively.
    summary = plan.get("account_summary")
    if isinstance(summary, dict):
        summary.setdefault("company", pseudo_company)
        summary.setdefault("week", week)
    elif isinstance(summary, list):
        for item in summary:
            if isinstance(item, dict):
                item.setdefault("company", pseudo_company)
                item.setdefault("week", week)

    # Same defensive injection for account_decisions.
    for ad in plan.get("account_decisions") or []:
        if isinstance(ad, dict):
            ad.setdefault("company", pseudo_company)

    try:
        _validate_plan(plan)
    except IngestPlanError as exc:
        raise AccountPlanError(f"plan failed validation: {exc}") from exc

    project_codes = tuple(p.code for p in active_projects)
    return GeneratedAccountPlan(
        plan=plan,
        raw_response=response_text,
        company_code=pseudo_company,
        meeting_id=meeting_id,
        project_codes=project_codes,
        model=model,
    )


def _build_sprint_planning_prompt(
    *,
    scope: str,
    scope_label: str,
    week: str,
    active_projects_block: str,
    account_decisions_context: str,
    transcript: str,
    team: tuple[str, ...] = (),
) -> str:
    today = datetime.now().date().isoformat()
    if team:
        team_block = (
            "These names are INTERNAL TEAM MEMBERS, not stakeholders.\n"
            "Never add them as new entries to a `stakeholders` verb:\n"
            + ", ".join(team)
        )
    else:
        team_block = "(No team roster declared in tenant config.)"

    if account_decisions_context:
        decisions_block = (
            "### Recent account-level decisions (already in weekly-cp.md)\n\n"
            "Don't re-emit these as either project-level decisions OR new "
            "account_decisions; the system already knows them.\n\n"
            + account_decisions_context
        )
    else:
        decisions_block = ""

    return _SPRINT_PLANNING_PROMPT_TEMPLATE.format(
        today=today,
        week=week,
        scope=scope,
        scope_label=scope_label,
        active_projects_block=active_projects_block,
        decisions_block=decisions_block,
        team_block=team_block,
        transcript=transcript,
    )


_SPRINT_PLANNING_PROMPT_TEMPLATE = """\
You are extracting structured updates from a TENANT-SCOPE SPRINT
PLANNING transcript — a weekly meeting where the team plans work
across every active project in scope. Different from a single-account
meeting: the project list spans MULTIPLE clients (or all internal
initiatives), so per-project routing must be especially careful.

Your output is a YAML plan that `cp ingest` will execute against the
tenant.

# Today
{today}

# Sprint week
{week}

# Scope
{scope_label}

# Internal team
{team_block}

# Active projects in scope

{active_projects_block}

{decisions_block}

# Schema you must produce

```yaml
transcript:
  source: fathom
  path: sprint-planning

projects:                     # one entry per project that has content
  <project-code>:
    inbound:                  # things a stakeholder said
      - text: "..."
        date: "YYYY-MM-DD"
        who: "<who said it>"
    asks:                     # outstanding requests
      - text: "..."
        who: "<who we're asking>"
        by: "YYYY-MM-DD"
        date: "YYYY-MM-DD"
    decisions:
      - text: "..."
        date: "YYYY-MM-DD"
        cross_cutting: false
    risks:
      - text: "..."
        severity: "watching"
        category: "schedule"
        date: "YYYY-MM-DD"

account_summary:              # ONE entry — paragraph capturing the gestalt
  text: "..."                  # 80–200 words, narrative not bullets
  # company + week injected server-side; you don't need them

account_decisions:            # OPTIONAL — tenant-wide decisions
  - text: "..."
    date: "YYYY-MM-DD"
```

# Rules

1. **Route per-project, don't broadcast.** A bullet for `ggl-5168`
   only goes in `projects.ggl-5168`. Cross-project items belong in
   `account_summary` or `account_decisions`, not duplicated across
   N projects.

2. **Don't over-extract.** Sprint planning often touches projects
   only briefly ("we'll pick up 5151 next week"). That's not a
   per-project verb — it's a summary mention. Reserve project
   verbs for substantive content.

3. **The `account_summary` is REQUIRED.** Exactly one entry.
   80–200 words covering: which projects got attention this sprint,
   key allocation/capacity decisions, dominant risks, anything
   that crosses project boundaries. This is the partner-review
   surface in weekly-cp.md.

4. **`account_decisions` are TENANT-WIDE structured one-liners.**
   Examples: "Sprint cadence shifts to 2-week cycles starting
   2026-W22." "Holding reviews moved from Friday to Tuesday."
   Don't put project-specific decisions here.

5. **Decisions vs inbound.** Decision = commitment made in the
   meeting (who's doing what, by when). Inbound = information
   conveyed.

6. **Skip stakeholders entirely** for sprint planning — the team
   roster is known; new external contacts almost never get
   introduced in sprint planning.

7. **Don't duplicate what's already known.** If the meeting
   restates a recent account-level decision (above), skip it.

8. **Date fields:** use the meeting date if known, otherwise
   today ({today}). All dates as ISO YYYY-MM-DD.

9. **Quote-like fidelity.** Reflect what was said. No
   interpretation or speculation.

10. **No empty lists.** Omit any project entry / verb that has no
    items. The `account_summary` is the only field that's always
    present.

# Output format

Respond with ONLY the YAML plan inside a single ```yaml fenced code
block. No preamble, no explanation, no postscript.

# Transcript

{transcript}
"""
