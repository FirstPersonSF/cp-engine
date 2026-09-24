"""Generate a `cp ingest` plan from a parent-workstream meeting transcript.

This is the engine half of Phase D.4 (account meetings), re-keyed on the
workstream tree (#305, plan §3.7). The cp-engine-webhook's
/api/auto-ingest-account endpoint calls `generate_account_plan()` when a
Fathom meeting has been tagged to a workstream that HAS CHILDREN — an
account node (`ggl-5216-google`) or a program. "Account meeting" means
exactly that: a meeting tagged to a node with children.

Difference from `plan_from_transcript`: instead of one project's context,
the prompt loads every active workstream BELOW the node (`list_active_
subtree`) and asks Claude to route content per-child in a single pass.
Output is a multi-project plan `cp_engine.ingest.execute_plan` consumes,
plus an `account_summary` paragraph (→ the node's sprint file) and
`account_decisions` one-liners (→ the node's `cp.md`).

Sprint-planning scopes (`1p`, `fpsf`, `canonic`, `storyos-mc`) keep their
per-child fan-out; their summary goes to `sprints/<W##>/_week.md` and their
decisions to master-cp.md's hand-written cross-cutting section (D8). No
pseudo-company exists any more.

See `cp/docs/plans/2026-05-14-account-meetings.md` for the original design
and `cp/docs/plans/2026-09-24-company-workstream-implementation-plan.md`
§3.7 for the tree form.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
import logging
from pathlib import Path

import yaml

from cp_engine.config import TenantConfig
from cp_engine.ingest import IngestPlanError, _validate_plan
from cp_engine.plan_from_transcript import (
    _MAX_PROJECT_CP_CHARS,
    _call_claude,
    _extract_yaml,
    _find_project_dir,
    _load_recent_account_decisions,
)
from cp_engine.sprints import current_sprint_week_iso
from cp_engine.codes import parse_code
from cp_engine.state import (
    ProjectState,
    descendants_of,
    display_name,
    effective_label,
)

log = logging.getLogger(__name__)

# Conservative ceiling for the multi-project context block. Each project's
# cp.md gets a hard truncation; we don't try to fit ALL sprint files in
# the prompt (they'd blow past Anthropic's context for an account with
# 6+ active projects).
_MAX_PER_PROJECT_CP_CHARS = 2500
# Transcript ceiling: ~100k tokens — far past any real meeting. The old
# 60k-char cap sat exactly at a one-hour meeting's length, silently cutting
# the END of longer 1P sprint-planning sessions — the projects discussed
# last got nothing routed. Truncation (now effectively unreachable) logs a
# warning instead of being invisible.
_MAX_TRANSCRIPT_CHARS = 400_000


def _truncate_transcript(transcript_text: str, *, context: str) -> str:
    """Cap the transcript at _MAX_TRANSCRIPT_CHARS, loudly."""
    if len(transcript_text) <= _MAX_TRANSCRIPT_CHARS:
        return transcript_text
    log.warning(
        "%s: transcript truncated %d -> %d chars — content near the end "
        "of the meeting will NOT be routed",
        context, len(transcript_text), _MAX_TRANSCRIPT_CHARS,
    )
    return (
        transcript_text[:_MAX_TRANSCRIPT_CHARS]
        + "\n\n[... transcript truncated ...]\n"
    )


class AccountPlanError(Exception):
    """Raised when Claude fails to return a valid account-meeting plan."""


@dataclass
class GeneratedAccountPlan:
    plan: dict
    raw_response: str
    # The workstream the meeting was tagged to (the parent node), or the
    # `sprint-planning:<scope>` key for a scope meeting.
    code: str
    meeting_id: str
    project_codes: tuple[str, ...]
    model: str


def generate_account_plan(
    *,
    config: TenantConfig,
    code: str,
    meeting_id: str,
    transcript_text: str,
    active_projects: list[ProjectState],
    node: ProjectState | None = None,
    week_iso: str | None = None,
    model: str = "claude-opus-4-8",
    api_key: str | None = None,
) -> GeneratedAccountPlan:
    """Generate the parent-workstream meeting plan via one Claude call.

    `code` is the NODE the meeting was tagged to (an account or program —
    any workstream with children); `active_projects` is its active subtree
    from `list_active_subtree` (the node itself excluded). Each child gets
    its compact context in the prompt; Claude routes per-child verbs on
    transcript relevance, then emits one `account_summary` paragraph for
    the node and optional `account_decisions` for the node's `cp.md`. Both
    are stamped with `code` here, so the executor never has to guess.

    `node` (when the caller has it) names the level in the prompt with the
    reference-style name; `week_iso` defaults to the current sprint week.
    """
    if not active_projects:
        raise AccountPlanError(
            f"workstream {code!r} has no active children to route to"
        )

    week = week_iso or current_sprint_week_iso(datetime.now())

    transcript = _truncate_transcript(
        transcript_text, context=f"account-plan {code}"
    )

    project_block = _format_active_projects(config, active_projects)
    account_decisions_context = _load_recent_account_decisions(config.root, code=code)

    prompt = _build_account_prompt(
        code=code,
        node_label=_node_label(node, active_projects),
        week=week,
        active_projects_block=project_block,
        account_decisions_context=account_decisions_context,
        transcript=transcript,
        team=config.team,
    )

    response_text = _call_claude(
        prompt, model=model, api_key=api_key, timeout=300
    )
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

    # Stamp the node code + week onto account_summary; the webhook knows
    # both, so nothing depends on prompt adherence (mirrors plan_from_slack).
    # The level is NAMED here, never inferred from content (#304).
    _stamp_level(plan, code=code, week=week)

    try:
        _validate_plan(plan)
    except IngestPlanError as exc:
        raise AccountPlanError(f"plan failed validation: {exc}") from exc

    project_codes = tuple(p.code for p in active_projects)
    return GeneratedAccountPlan(
        plan=plan,
        raw_response=response_text,
        code=code,
        meeting_id=meeting_id,
        project_codes=project_codes,
        model=model,
    )


def _stamp_level(
    plan: dict, *, week: str, code: str | None = None, scope: str | None = None
) -> None:
    """Write the level every account-level item lands on: `code` (the node)
    or `scope` (a sprint-planning meeting), plus the week on summaries. A
    stale `company` key from an old plan is dropped — it names nothing
    now."""
    key, value = ("code", code) if code else ("scope", scope)
    summary = plan.get("account_summary")
    items = summary if isinstance(summary, list) else [summary]
    for item in items:
        if isinstance(item, dict):
            item.pop("company", None)
            item[key] = value
            item.setdefault("week", week)
    for ad in plan.get("account_decisions") or []:
        if isinstance(ad, dict):
            ad.pop("company", None)
            ad[key] = value


def _format_active_projects(
    config: TenantConfig, projects: list[ProjectState]
) -> str:
    """Render the active project list with each project's compact context.

    The header carries the derived label word for every non-job (`program`,
    `initiative`, `account`) — the same word `master-cp.md` shows — so the
    model reads the level, not a type name."""
    by_code = {p.code: p for p in projects}
    parts: list[str] = []
    for p in projects:
        header = f"### `{p.code}` — {p.name}"
        label = effective_label(p, by_code)
        if label != "job":
            header += f" ({label})"
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


def _node_label(
    node: ProjectState | None, active_projects: list[ProjectState]
) -> str:
    """The human name of the level the meeting was tagged to: the node's
    reference-style name (an account or program is its name alone), else
    the children's company name, else the company code."""
    if node is not None:
        return display_name(node)
    for p in active_projects:
        if p.company_name:
            return p.company_name
    if active_projects and active_projects[0].company_code:
        return active_projects[0].company_code
    return "(unknown)"


def _build_account_prompt(
    *,
    code: str,
    node_label: str,
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
            "### Recent account-level decisions (already recorded on this "
            "workstream or above it)\n\n"
            "Don't re-emit these as either project-level decisions OR new "
            "account_decisions; the system already knows them.\n\n"
            + account_decisions_context
        )
    else:
        decisions_block = ""

    return _PROMPT_TEMPLATE.format(
        today=today,
        week=week,
        code=code,
        node_label=node_label,
        active_projects_block=active_projects_block,
        decisions_block=decisions_block,
        team_block=team_block,
        transcript=transcript,
    )


_PROMPT_TEMPLATE = """\
You are extracting structured updates from a PARENT-WORKSTREAM meeting
transcript — a sync tagged to a workstream that has children (a client
account such as Google, or a program), touching several of the
workstreams below it. Your output is a YAML plan that `cp ingest` will
execute against the tenant.

Unlike a single-project meeting, you must ROUTE each piece of content
to whichever child workstream it's actually about. The active list is
below; emit per-project verbs only when content clearly relates to a
specific one.

# Today
{today}

# Sprint week
{week}

# Parent workstream
{node_label} (canonical code: `{code}`)

# Internal team
{team_block}

# Active workstreams under this parent

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

account_summary:              # A MAPPING, not a list. Exactly one.
  text: "..."                  # 60–150 words, narrative not bullet list
  # company + week are injected server-side; you don't need them
  # NOTE: `account_summary` takes a mapping with a `text:` key — NOT a list
  # of strings, and NOT a bare string. A list here fails validation and the
  # ENTIRE plan is discarded, every project's routing with it.

account_decisions:            # OPTIONAL — tenant-wide decisions
  - text: "..."
    date: "YYYY-MM-DD"
```

# Rules

0. **One verb name per concept, and never a global list.** Use `asks`,
   `decisions`, `risks` — NOT their canonical aliases (`record-ask`,
   `add-decision`, `record-risk`) — and never both. On 2026-09-15 a plan used
   `asks` per-project AND `record-ask` carrying the same six items under all
   18 projects: 108 bullets where 6 belonged, a Teleflex ask filed on a Google
   project. **If an item belongs to the week rather than to a project, it does
   not go in `projects` at all.**

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
   60–150 word paragraph capturing the gestalt of this meeting: what
   got covered across the children, dominant themes, status of the
   relationship. It lands on the PARENT workstream's sprint file for
   partner-review visibility.

4. **`account_decisions` are PARENT-LEVEL structured one-liners** —
   decisions that belong to the account or program as a whole, not to
   one child. Examples: "All Google consultant invoices route through
   Brandon going forward." "Maria leaves the account 2026-05-31;
   transition plan to Geoff." They land on the parent workstream's
   `cp.md`. Don't put project-specific decisions here — those go in
   the child's `decisions` verb.

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


def load_roster(config: TenantConfig) -> list[ProjectState]:
    """Every workstream MC-2 knows, whatever its status — the roster the
    tree helpers (`list_active_subtree`, `resolve_node`) take."""
    from cp_engine.sync import _default_backend_factory

    backend = _default_backend_factory(config.sync.backend)
    return list(backend.read_projects(config))


def list_active_subtree(
    code: str, projects: "list[ProjectState] | tuple[ProjectState, ...]"
) -> list[ProjectState]:
    """Every ACTIVE workstream below `code` — children, grandchildren, … —
    sorted by code. The node itself is excluded: it is where the summary
    and the account-level decisions land, not a fan-out target (#305).

    `projects` is the whole roster (`load_roster`), so an inactive program
    between the node and an active job does not hide the job. A code that
    is not in the roster has no subtree.
    """
    from cp_engine.status import is_active_status

    by_code = {p.code: p for p in projects}
    node = resolve_node(code, projects)
    if node is None:
        return []
    out = [
        p for p in descendants_of(node.code, by_code)
        if p.code != node.code and is_active_status(p.status)
    ]
    out.sort(key=lambda p: p.code)
    return out


def resolve_node(
    code: str | None, projects: "list[ProjectState] | tuple[ProjectState, ...]"
) -> ProjectState | None:
    """The roster entry for `code` in any spelling: the canonical slug,
    the short form (`ggl-5216`), MC-2's display name. None when nothing
    matches — a caller that guesses a parent lands captures on the wrong
    level, so an unresolved code is refused upstream."""
    if not code:
        return None
    wanted = code.strip().lower()
    for p in projects:
        if p.code.lower() == wanted:
            return p
    parsed = parse_code(code)
    if parsed is None:
        return None
    for p in projects:
        mine = parse_code(p.code)
        if mine and (mine.company, mine.number) == (parsed.company, parsed.number):
            return p
    return None


def resolve_account_node(
    company_code: str | None, projects: "list[ProjectState] | tuple[ProjectState, ...]"
) -> ProjectState | None:
    """The account node of a company (the legacy `company_code` payload a
    fathom-meeting-sync row may still send): the roster entry labelled
    `account` under that company code, case-insensitive. None when the
    company has no account node in the roster."""
    if not company_code:
        return None
    wanted = company_code.strip().lower()
    by_code = {p.code: p for p in projects}
    for p in projects:
        if (p.company_code or "").lower() != wanted:
            continue
        if effective_label(p, by_code) == "account":
            return p
    return None


def list_active_all(config: TenantConfig) -> list[ProjectState]:
    """Return every active workstream across the tenant (Deal | Open, all
    companies, every shape — #301).

    Used as the cross-project detection roster (#88): the single-project
    ingest classifier scores extracted items against this list to spot
    items that belong to a DIFFERENT active project than the one the
    meeting was tagged to.
    """
    from cp_engine.status import is_active_status
    from cp_engine.sync import _default_backend_factory

    backend = _default_backend_factory(config.sync.backend)
    all_projects = backend.read_projects(config)

    out: list[ProjectState] = [p for p in all_projects if is_active_status(p.status)]
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
# product/engineering sync — pairs StoryOS with Mission Control. If you add
# a new explicit-list scope later, list its target workstream codes here;
# list_active_for_scope falls through to a code-allowlist lookup when the
# scope isn't in _SCOPE_TO_KIND.
_SCOPE_TO_EXPLICIT_CODES = {
    # The merged workstream codes (mc-2 mig 192, cp-engine #301).
    "storyos-mc": ("cnc-9004-storyos", "1pi-9005-mission-control"),
}

# Valid scope names: the kind-based ones plus the explicit-code ones.
VALID_SCOPES = frozenset(_SCOPE_TO_KIND) | frozenset(_SCOPE_TO_EXPLICIT_CODES)

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

    Sister function to `list_active_subtree` but the discriminator is the
    company kind OR an explicit code list, not a parent node. Used by the
    cp-engine-webhook /api/auto-ingest-sprint-planning endpoint.
    """
    from cp_engine.status import is_active_status
    from cp_engine.state import scope_for
    from cp_engine.sync import _default_backend_factory

    if scope not in VALID_SCOPES:
        raise AccountPlanError(
            f"unknown sprint-planning scope {scope!r}; "
            f"expected one of {sorted(VALID_SCOPES)}"
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
            # Apply the same activity check that the kind-based scopes use.
            if is_active_status(p.status):
                out.append(p)
        return out

    target_kind = _SCOPE_TO_KIND[scope]

    out: list[ProjectState] = []
    # One rule for every scope (#301): active workstreams (Deal | Open)
    # under the scope's company kind.
    for p in all_projects:
        if p.company_kind != target_kind:
            continue
        if is_active_status(p.status):
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
    model: str = "claude-opus-4-8",
    api_key: str | None = None,
) -> GeneratedAccountPlan:
    """Generate a sprint-planning plan via one Claude call.

    Same shape as `generate_account_plan` but the prompt frames this
    as tenant-scope sprint planning — the meeting touches every active
    workstream under the named scope (1P, FPSF, Canonic, StoryOS + MC).
    The `account_summary` is stamped with the `scope` and lands in
    `sprints/<W##>/_week.md` under `## Sprint planning summaries` (plan
    D8); `account_decisions` land in master-cp.md's hand-written
    cross-cutting section. No pseudo-company node exists (#305).
    """
    if scope not in VALID_SCOPES:
        raise AccountPlanError(
            f"unknown sprint-planning scope {scope!r}; "
            f"expected one of {sorted(VALID_SCOPES)}"
        )
    if not active_projects:
        raise AccountPlanError(
            f"scope {scope!r} has no active projects to route to"
        )

    week = week_iso or current_sprint_week_iso(datetime.now())
    scope_label = _SCOPE_LABEL[scope]

    transcript = _truncate_transcript(
        transcript_text, context=f"sprint-planning {scope}"
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

    response_text = _call_claude(
        prompt, model=model, api_key=api_key, timeout=300
    )
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

    # Stamp the scope + week onto account_summary / account_decisions.
    _stamp_level(plan, scope=scope, week=week)

    try:
        _validate_plan(plan)
    except IngestPlanError as exc:
        raise AccountPlanError(f"plan failed validation: {exc}") from exc

    project_codes = tuple(p.code for p in active_projects)
    return GeneratedAccountPlan(
        plan=plan,
        raw_response=response_text,
        code=f"sprint-planning:{scope}",
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
            "### Recent cross-cutting decisions (already recorded in the "
            "tenant)\n\n"
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

account_summary:              # A MAPPING, not a list. Exactly one.
  text: "..."                  # 80–200 words, narrative not bullets
  # company + week injected server-side; you don't need them
  # NOTE: `account_summary` takes a mapping with a `text:` key — NOT a list
  # of strings, and NOT a bare string. A list here fails validation and the
  # ENTIRE plan is discarded, every project's routing with it.

account_decisions:            # OPTIONAL — tenant-wide decisions
  - text: "..."
    date: "YYYY-MM-DD"
```

# Rules

0. **One verb name per concept, and never a global list.** Use `asks`,
   `decisions`, `risks` — NOT their canonical aliases (`record-ask`,
   `add-decision`, `record-risk`) — and never both. On 2026-09-15 a plan used
   `asks` per-project AND `record-ask` carrying the same six items under all
   18 projects: 108 bullets where 6 belonged, a Teleflex ask filed on a Google
   project. **If an item belongs to the week rather than to a project, it does
   not go in `projects` at all.**

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
   surface in the week file (`sprints/<W##>/_week.md`).

4. **`account_decisions` are TENANT-WIDE structured one-liners.**
   Examples: "Sprint cadence shifts to 2-week cycles starting
   2026-W22." "Holding reviews moved from Friday to Tuesday."
   They land in master-cp.md's hand-written cross-cutting decisions.
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
