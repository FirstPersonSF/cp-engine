"""Generate a `cp ingest` plan from a meeting transcript via Claude.

This is the engine half of Phase C (auto-ingest). The CLI command
`cp ingest-from-transcript` and the eventual cp-engine-webhook service
both call into `generate_plan()`.

Design constraints:
- The plan Claude returns MUST pass `cp_engine.ingest._validate_plan`.
  We don't try to repair invalid output; we surface the error so the
  caller can decide whether to retry or punt to human review.
- The prompt assumes the transcript is for *one* project at a time.
  Multi-project meetings (sprint planning, account reviews) are a
  different workflow with different prompting.
- Project context is bounded — we don't dump the entire `cp.md` and
  every sprint file. The model needs enough to recognize ongoing
  threads (the project cp.md's exec-summary, the current sprint's Open
  asks, recent decisions) without being buried in history.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from cp_engine.config import TenantConfig
from cp_engine.ingest import IngestPlanError, _content_hash, _validate_plan
from cp_engine.sprints import current_sprint_week_iso

# Conservative ceilings to keep prompt latency + cost reasonable.
# The transcript is the bulk; project context stays small on purpose.
_MAX_PROJECT_CP_CHARS = 6000
_MAX_SPRINT_FILE_CHARS = 4000
_MAX_TRANSCRIPT_CHARS = 60000


class PlanGenerationError(Exception):
    """Raised when Claude fails to return a valid plan after retries."""


@dataclass
class GeneratedPlan:
    plan: dict
    raw_response: str
    project_code: str
    transcript_path: Path
    model: str


def generate_plan(
    *,
    config: TenantConfig,
    project_code: str,
    transcript_path: Path,
    action_items: list[dict] | None = None,
    model: str = "claude-opus-4-7",
    api_key: str | None = None,
) -> GeneratedPlan:
    """Read transcript + project context, ask Claude for a plan, validate it.

    If `action_items` is provided (the Fathom meeting's `action_items`
    JSONB), each item becomes a deterministic `record-ask` appended to
    the plan's `projects[<project_code>]["record-ask"]` list. The hash
    recipe matches `ingest._content_hash`, so `_write_ask`'s dedupe will
    treat re-ingests of the same meeting as no-ops AND the ClickUp ↔ cp
    round-trip (Task 1.7) can match a closed ClickUp task back to its
    source ask.

    Raises PlanGenerationError if the response can't be parsed or doesn't
    pass `_validate_plan`. The caller should catch and decide what to do
    (retry once with the validation error fed back, log + punt, etc.).
    """
    transcript = _read_transcript(transcript_path)
    project_ctx = _load_project_context(config, project_code)
    prompt = _build_prompt(
        transcript=transcript,
        project_context=project_ctx,
        project_code=project_code,
        transcript_path=transcript_path,
        team=config.team,
    )

    response_text = _call_claude(prompt, model=model, api_key=api_key)
    yaml_text = _extract_yaml(response_text)

    try:
        plan = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise PlanGenerationError(
            f"Claude returned non-YAML output: {exc}\n--- response ---\n{response_text[:500]}"
        ) from exc

    if not isinstance(plan, dict):
        raise PlanGenerationError(
            f"Claude returned a non-mapping plan: {type(plan).__name__}"
        )

    try:
        _validate_plan(plan)
    except IngestPlanError as exc:
        raise PlanGenerationError(f"plan failed validation: {exc}") from exc

    # Merge Fathom action_items as deterministic record-ask items.
    # Runs AFTER _validate_plan because the items are constructed by us
    # (deterministic helper, well-formed by construction) — not by the
    # LLM. The merge appends to projects[code]["record-ask"], a verb
    # already in _SUPPORTED_VERBS; each item is a dict; extra fields
    # (`verb`, `hash`) are ignored by _validate_plan's per-item check
    # (it only verifies isinstance(item, dict)) and by _write_ask (it
    # reads only `text`, `who`, `by`, `date`, `status`). Re-running
    # _validate_plan here would therefore still pass — we skip it only
    # to avoid the redundant pass on a now-trusted plan.
    if action_items:
        today_iso = datetime.now().strftime("%Y-%m-%d")
        ask_items = _action_items_to_ask_items(
            code=project_code, action_items=action_items, today_iso=today_iso
        )
        if ask_items:
            proj = plan.setdefault("projects", {}).setdefault(project_code, {})
            proj.setdefault("record-ask", []).extend(ask_items)

    return GeneratedPlan(
        plan=plan,
        raw_response=response_text,
        project_code=project_code,
        transcript_path=transcript_path,
        model=model,
    )


def _read_transcript(path: Path) -> str:
    raw = path.read_text(encoding="utf-8")
    if len(raw) > _MAX_TRANSCRIPT_CHARS:
        return raw[:_MAX_TRANSCRIPT_CHARS] + "\n\n[... transcript truncated ...]\n"
    return raw


def _load_project_context(config: TenantConfig, project_code: str) -> str:
    """Compact, prompt-shaped view of what's already known about this project.

    Returns:
    - the project's `cp.md` (truncated)
    - the current sprint file (truncated)
    - recent account-level decisions from weekly-cp.md (filtered by company prefix)

    All three are markdown; the model reads them as-is.
    """
    project_dir = _find_project_dir(config.root, project_code)
    parts: list[str] = []

    if project_dir is None:
        parts.append(f"(No working directory found for {project_code} — project may be inactive.)")
    else:
        cp_md = project_dir / "cp.md"
        if cp_md.is_file():
            text = cp_md.read_text(encoding="utf-8")
            if len(text) > _MAX_PROJECT_CP_CHARS:
                text = text[:_MAX_PROJECT_CP_CHARS] + "\n[... cp.md truncated ...]\n"
            parts.append(f"### Project cp.md ({cp_md.relative_to(config.root)})\n\n{text}")

    week_iso = current_sprint_week_iso(datetime.now())
    sprint_file = config.root / "sprints" / week_iso / f"{project_code}.md"
    if sprint_file.is_file():
        text = sprint_file.read_text(encoding="utf-8")
        if len(text) > _MAX_SPRINT_FILE_CHARS:
            text = text[:_MAX_SPRINT_FILE_CHARS] + "\n[... sprint file truncated ...]\n"
        parts.append(
            f"### Current sprint file ({sprint_file.relative_to(config.root)})\n\n{text}"
        )
    else:
        parts.append(
            f"(No sprint file at sprints/{week_iso}/{project_code}.md yet — "
            "this would be the first ingest of the sprint.)"
        )

    account_decisions = _load_recent_account_decisions(config.root)
    if account_decisions:
        parts.append(
            "### Recent account-level decisions (already in weekly-cp.md)\n\n"
            "These are decisions that already exist in the tenant. If the\n"
            "transcript restates one of these, do NOT emit it again — even\n"
            "if the wording is slightly different.\n\n"
            + account_decisions
        )

    return "\n\n".join(parts)


# A decision line in weekly-cp.md follows the shape:
#   <N>. **<title>** ...optional body with arbitrary parens... (YYYY-MM-DD, source: <kind>)
# The numbered prefix + bold title is the load-bearing signal; the source
# clause tells us *what kind* of decision it is. We pull the title (first
# **...** on the line) and date for the prompt.
_DECISION_NUM_RE = re.compile(r"^\s*(?P<num>\d+)\.\s+\*\*(?P<title>[^*]+)\*\*", re.MULTILINE)
_DECISION_DATE_RE = re.compile(
    r"\((?P<date>\d{4}-\d{2}-\d{2}),\s*source:\s*(?P<source>[^)]+)\)"
)


def _load_recent_account_decisions(tenant_root: Path, *, max_lines: int = 25) -> str:
    """Pull recent account-level decision titles from weekly-cp.md.

    Conservative: just the bold-title sentence + date, not the full body.
    Prevents Claude from re-emitting account-wide commitments as project-level
    ones (the "Go Safety launches 6/1" duplication pattern from C.1 testing).

    Two-pass parse because decision bodies contain arbitrary parentheses
    (e.g. "(2-4 weeks)", "(payroll)") so a single regex anchoring on
    `(YYYY-MM-DD, source: ...)` from the title block is fragile. Instead:
    walk numbered decisions, then find their date+source clause in the
    same line as the title.

    Returns "" if no weekly-cp.md or no decisions match.
    """
    weekly_cp = tenant_root / "weekly-cp.md"
    if not weekly_cp.is_file():
        return ""

    body = weekly_cp.read_text(encoding="utf-8")
    decisions: list[dict] = []
    for line in body.splitlines():
        title_m = _DECISION_NUM_RE.match(line)
        if not title_m:
            continue
        date_m = _DECISION_DATE_RE.search(line)
        if not date_m:
            continue
        source = date_m.group("source").strip().lower()
        # Account-level signals: explicit "account:" tag (Phase B format),
        # the older "weekly account meeting" tag, or any per-project source
        # like "ggl-5136" (these are project-level decisions but they affect
        # related projects in the same client family — including them keeps
        # Claude from re-emitting cross-project context).
        if not (source.startswith("account:") or "account meeting" in source or "-" in source):
            continue
        decisions.append({
            "title": title_m.group("title").strip(),
            "date": date_m.group("date"),
            "source": source,
        })

    if not decisions:
        return ""

    decisions.sort(key=lambda d: d["date"], reverse=True)
    decisions = decisions[:max_lines]
    return "\n".join(
        f"- ({d['date']}, source: {d['source']}) {d['title']}" for d in decisions
    )


def _find_project_dir(tenant_root: Path, project_code: str) -> Path | None:
    """Search 1p/, firstpersonsf/, canonic/ for a directory matching the code.

    Working directories are named `<code>-<slug>` (per cp-engine v0.7+).
    We match on the code prefix.
    """
    for scope in ("1p", "firstpersonsf", "canonic"):
        scope_dir = tenant_root / scope
        if not scope_dir.is_dir():
            continue
        for entry in scope_dir.iterdir():
            if entry.is_dir() and (entry.name == project_code or entry.name.startswith(f"{project_code}-")):
                return entry
        # Also check inactive/
        inactive_dir = scope_dir / "inactive"
        if inactive_dir.is_dir():
            for entry in inactive_dir.iterdir():
                if entry.is_dir() and (entry.name == project_code or entry.name.startswith(f"{project_code}-")):
                    return entry
    return None


# Engagement codes match `<3-letter>-<digits>` (e.g. ggl-5168). Anything
# else (storyos, mission-control, mc-2) is an initiative or a standalone
# repo. We don't distinguish initiative vs. repo for prompt-shaping
# purposes — both are "internal", neither has a client-communication
# surface — so the boolean `is_engagement` is the only discriminator
# that matters.
_ENGAGEMENT_CODE_RE = re.compile(r"^[a-z]{2,4}-\d{3,5}$")


def _is_engagement_code(project_code: str) -> bool:
    return bool(_ENGAGEMENT_CODE_RE.match(project_code or ""))


def _build_prompt(
    *,
    transcript: str,
    project_context: str,
    project_code: str,
    transcript_path: Path,
    team: tuple[str, ...] = (),
) -> str:
    today = datetime.now().date().isoformat()
    if team:
        team_block = (
            "These names are INTERNAL TEAM MEMBERS, not project stakeholders.\n"
            "Never add them as new entries to the `stakeholders` verb:\n"
            + ", ".join(team)
        )
    else:
        team_block = "(No team roster declared in tenant config.)"

    # Initiatives (Mission Control, StoryOS, etc.) have no client side
    # and no external stakeholders, so the engagement-shape verbs
    # `inbound` and `stakeholders` don't apply — the prompt drops them
    # from the schema and emphasizes decisions/risks/asks instead.
    template = (
        _PROMPT_TEMPLATE
        if _is_engagement_code(project_code)
        else _INITIATIVE_PROMPT_TEMPLATE
    )
    return template.format(
        today=today,
        project_code=project_code,
        transcript_relpath=str(transcript_path),
        project_context=project_context,
        transcript=transcript,
        team_block=team_block,
    )


_PROMPT_TEMPLATE = """\
You are extracting structured updates from a meeting transcript for the cp-engine
context-protocol system. Your output is a YAML plan that `cp ingest` will execute
against the project's sprint file.

# Today
{today}

# Target project
{project_code}

# Internal team
{team_block}

# What's already known about this project
{project_context}

# Schema you must produce

```yaml
transcript:
  source: fathom
  path: {transcript_relpath}

projects:
  {project_code}:
    inbound:        # things the client said/did to/at us
      - text: "..."
        date: "YYYY-MM-DD"
        who: "<who said it>"
    asks:           # outstanding requests we made of the client
      - text: "..."
        who: "<who we're asking>"
        by: "YYYY-MM-DD"          # optional deadline
        date: "YYYY-MM-DD"        # when we asked; defaults to today
    decisions:
      - text: "..."
        date: "YYYY-MM-DD"
        cross_cutting: false      # true → also surfaces in weekly-cp.md
    risks:
      - text: "..."
        severity: "watching"      # or "escalated", "dependency"
        category: "schedule"      # or contract, scope, technical, etc.
        date: "YYYY-MM-DD"
    stakeholders:                 # only NEW stakeholders not already in cp.md
      - name: "<name>"
        role: "<role>"            # optional
        context: "<one-line context>"  # optional

    # Forward-looking sprint-planning verbs (v0.15+). These propose
    # ClickUp tasks behind the dashboard's human review gate — they
    # do NOT write to the sprint file. Use them ONLY when the
    # transcript names a concrete, dated commitment (not aspiration).
    set-milestone:                # internal deliverables that ship with a date
      - deliverable: "<what's being shipped>"
        date: "YYYY-MM-DD"        # ship date
        owner: "<internal person>"
        confidence: "high"        # high | medium | low — your read of certainty
        depends_on: ["<other deliverable / event>"]  # optional
        linked_to: ["<other_project_code>"]          # optional
    set-client-ask-task:          # external commitment we're waiting on
      - what: "<what we're waiting on>"
        from_party: "<external stakeholder name>"
        expected_by: "YYYY-MM-DD" # optional
```

# Rules

1. **Only include verbs that have entries.** Empty lists like `decisions: []`
   are forbidden. If you have nothing to say for a verb, omit it entirely.
2. **Don't duplicate what's already in the project context.** If a decision
   or ask already appears in the sprint file, cp.md, OR the "Recent
   account-level decisions" list, skip it. The system has its own dedup,
   but you should still try. Account-wide decisions (Maria's departure,
   consultant invoice routing, Go Safety launch dates) belong in
   weekly-cp.md and are emitted via a separate `account_decisions` flow,
   not by you here.
3. **Decisions vs inbound:** a decision is a *commitment made in the meeting*
   (who's doing what, by when, what we agreed to). Inbound is *information*
   the client conveyed (status, opinions, constraints, concerns).
4. **Asks** are open loops *we're waiting on someone for*. If the meeting
   resolved an existing ask, do NOT create a new entry — we'll handle
   close-ask in a different flow.
5. **Stakeholders only when genuinely new and EXTERNAL.** Internal team
   members from the "Internal team" list above must never appear here.
   Also skip people already named in cp.md or the sprint file. Add a
   stakeholder only when it's a client-side or vendor-side person not
   yet known to the system.
6. **Date fields:** use the meeting date if known (parse from transcript
   header), otherwise today ({today}). All dates as ISO YYYY-MM-DD.
7. **Quote-like fidelity, no embellishment.** The text should reflect what
   was actually said. Don't add interpretation or speculation. If a
   commitment is vague ("we should look at that"), it's NOT a decision.
8. **One project only.** Even if the meeting touches other projects, only
   record items relevant to {project_code}. Cross-project items belong in
   themes (handled separately).
9. **Milestones vs decisions vs asks.** Use `set-milestone` when the
   transcript names an INTERNAL deliverable with a date AND an owner
   ("Pop-up final to Rena Friday", "Tony delivers redirect page
   Monday"). Use `set-client-ask-task` when someone EXTERNAL commits
   to give us something by a date ("Rena will send R3 feedback by
   Tuesday", "Joe to confirm date"). Both go into ClickUp behind a
   review gate — propose them ONLY when the commitment is concrete
   and dated. Aspirational language ("we should ship something by
   end of month") is NOT a milestone. If the date is genuinely
   unclear, prefer the existing `asks`/`decisions` verbs which write
   to the sprint file's narrative sections.

# Output format

Respond with ONLY the YAML plan inside a single ```yaml fenced code block.
No preamble, no explanation, no postscript. If there is genuinely nothing
to ingest from this transcript for this project, return:

```yaml
transcript:
  source: fathom
  path: {transcript_relpath}
projects: {{}}
```

# Transcript

{transcript}
"""


_INITIATIVE_PROMPT_TEMPLATE = """\
You are extracting structured updates from an INTERNAL meeting transcript
about an initiative (an internal workstream like Mission Control or
StoryOS — NOT a client engagement). Your output is a YAML plan that
`cp ingest` will execute against the initiative's sprint file.

This is internal team work, so there is NO client side: no inbound
client messages, no client stakeholders to record. The relevant verbs
are `decisions`, `risks`, and `asks` (open loops between team members
or between teams).

# Today
{today}

# Target initiative
{project_code}

# Internal team
{team_block}

# What's already known about this initiative
{project_context}

# Schema you must produce

```yaml
transcript:
  source: fathom
  path: {transcript_relpath}

projects:
  {project_code}:
    asks:           # open loops between team members or teams
      - text: "..."
        who: "<who we're asking>"
        by: "YYYY-MM-DD"          # optional deadline
        date: "YYYY-MM-DD"        # when we asked; defaults to today
    decisions:
      - text: "..."
        date: "YYYY-MM-DD"
        cross_cutting: false      # true → also surfaces in weekly-cp.md
    risks:
      - text: "..."
        severity: "watching"      # or "escalated", "dependency"
        category: "schedule"      # or contract, scope, technical, etc.
        date: "YYYY-MM-DD"

    # Forward-looking sprint-planning verbs (v0.15+). Propose ClickUp
    # tasks behind the dashboard's human review gate — they do NOT
    # write to the sprint file. Use ONLY for concrete, dated
    # commitments (not aspiration). `set-client-ask-task` is omitted
    # here because initiatives have no external client side.
    set-milestone:                # internal deliverables that ship with a date
      - deliverable: "<what's being shipped>"
        date: "YYYY-MM-DD"
        owner: "<internal person>"
        confidence: "high"        # high | medium | low — your read of certainty
        depends_on: ["<other deliverable / event>"]  # optional
        linked_to: ["<other_project_code>"]          # optional
```

# Rules

1. **Only include verbs that have entries.** Empty lists are forbidden.
   If you have nothing to say for a verb, omit it entirely.
2. **No `inbound` and no `stakeholders` verbs.** This is internal work;
   there's no client side to capture inbound from, and the team roster
   is already known. Do NOT emit either verb — they're intentionally
   omitted from the schema above.
3. **Decisions are commitments made in the meeting.** Who's doing what,
   by when, what we agreed to architecturally. "Maybe we should..." is
   NOT a decision; "Drew will own the migration by Friday" is.
4. **Asks are open loops we're waiting on someone for.** Cross-team
   ("waiting on Tony to finish the schema review") or task-level
   ("Maria needs the design specs by 5/22"). If the meeting resolved
   an existing ask, do NOT create a new entry.
5. **Risks are explicit concerns about schedule, scope, dependencies,
   or technical issues.** Not vibes — "worried the migration won't be
   done in time" qualifies; "we should probably watch this" does not.
6. **Cross-cutting decisions go in weekly-cp.md.** Set `cross_cutting:
   true` for decisions that affect multiple initiatives or the whole
   company (e.g. team process changes, vendor selections).
7. **Don't duplicate what's already in the project context.** If a
   decision or ask already appears in the sprint file, cp.md, OR the
   "Recent account-level decisions" list, skip it.
8. **Date fields:** use the meeting date if known (parse from
   transcript header), otherwise today ({today}). ISO YYYY-MM-DD.
9. **Quote-like fidelity, no embellishment.** Reflect what was
   actually said. No interpretation or speculation.
10. **Milestones vs decisions.** Use `set-milestone` when the
    transcript names a concrete internal deliverable with a date AND
    owner ("Tony delivers the schema migration Friday"). This
    proposes a ClickUp task behind a review gate. Aspirational or
    undated commitments stay in `decisions` (which writes to the
    sprint file).

# Output format

Respond with ONLY the YAML plan inside a single ```yaml fenced code
block. No preamble, no explanation, no postscript. If there is
genuinely nothing to ingest from this transcript for this initiative,
return:

```yaml
transcript:
  source: fathom
  path: {transcript_relpath}
projects: {{}}
```

# Transcript

{transcript}
"""


def _call_claude(
    prompt: str, *, model: str, api_key: str | None, timeout: float = 120
) -> str:
    """Single Anthropic API call. Raises PlanGenerationError on transport failure."""
    try:
        from anthropic import Anthropic
    except ImportError as exc:
        raise PlanGenerationError(
            "anthropic package not installed. Run: pip install 'anthropic>=0.40'"
        ) from exc

    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise PlanGenerationError(
            "ANTHROPIC_API_KEY not set. Export it or pass --api-key."
        )

    # Explicit timeout: the SDK default is ~10min, long enough for a wedged
    # call to hang ingest. The 120s default is ample for a single-project
    # 16k-token completion; multi-project callers (account / sprint-planning
    # plans, which carry a much larger prompt and routinely 8k+ token
    # responses) pass a larger budget.
    client = Anthropic(api_key=key, timeout=timeout)
    try:
        response = client.messages.create(
            model=model,
            # 16k headroom — multi-project plans (account meetings,
            # sprint planning across 20+ projects) routinely produce
            # 8k+ token responses. The earlier 4k ceiling silently
            # truncated mid-output, producing un-closed YAML fences
            # that _extract_yaml couldn't recover from. Single-project
            # plans are well under this; we pay no extra cost for the
            # headroom (Anthropic bills on actual output, not max).
            max_tokens=16384,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # anthropic SDK raises various subclasses
        detail = str(exc)
        body = getattr(exc, "body", None) or getattr(exc, "response", None)
        if body is not None:
            detail = f"{detail} | body={body!r}"
        raise PlanGenerationError(f"Anthropic API call failed: {detail}") from exc

    if not response.content:
        raise PlanGenerationError("Anthropic returned empty content")
    block = response.content[0]
    text = getattr(block, "text", None)
    if not text:
        raise PlanGenerationError(f"Anthropic returned non-text block: {block!r}")
    # Surface truncation as a specific, diagnosable error before the
    # caller hits a downstream YAML parse failure. Anthropic sets
    # stop_reason='max_tokens' when the response was cut at the cap.
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "max_tokens":
        raise PlanGenerationError(
            "Anthropic response truncated at max_tokens — try a larger "
            "max_tokens budget or reduce prompt scope. "
            f"Got {len(text)} chars before truncation."
        )
    return text


_FENCE_RE = re.compile(r"```(?:yaml)?\s*\n?(.*?)\n?```", re.DOTALL)
_OPEN_FENCE_RE = re.compile(r"```(?:yaml)?\s*\n?")


def _extract_yaml(response_text: str) -> str:
    """Pull the YAML out of a fenced code block.

    Three cases:
    1. Complete fenced block (open + close) → return the inside.
    2. Partial response with opening fence but no close (truncation
       slipped past the max_tokens guard, or the model just forgot to
       close) → strip the opening fence and return the rest; let YAML
       parsing decide if there's anything usable.
    3. No fence at all → return as-is.

    Case 2 was a real bug pre-2026-05-18: a truncated multi-project
    response would skip case 1, fall through to case 3, and the
    backticks at the start would cause yaml.safe_load to throw a
    parse error pointing at column 1 — completely opaque to the user.
    """
    # Case 1: complete fenced block.
    match = _FENCE_RE.search(response_text)
    if match:
        return match.group(1).strip()
    # Case 2: opening fence present but no closing fence — strip the
    # opening and return whatever followed.
    open_match = _OPEN_FENCE_RE.search(response_text)
    if open_match:
        return response_text[open_match.end():].strip()
    # Case 3: no fence at all.
    return response_text.strip()


def _action_items_to_ask_items(
    *, code: str, action_items: list[dict], today_iso: str
) -> list[dict]:
    """Convert Fathom `action_items` JSONB into deterministic `record-ask` plan items.

    Each Fathom action item becomes one ask whose hash is stable across
    re-ingests of the same meeting — so the ClickUp ↔ cp round-trip can
    match a closed ClickUp task back to its source ask. The hash uses the
    same recipe as `_content_hash(code, "record-ask", text)` in ingest.py
    so `_write_ask` recognises the item as already-present on re-ingest.
    """
    out: list[dict] = []
    for raw in action_items or []:
        if not isinstance(raw, dict):
            continue
        text = (raw.get("description") or "").strip()
        if not text:
            continue
        assignee = raw.get("assignee") or {}
        who = (assignee.get("name") or "").strip()
        out.append({
            "verb": "record-ask",
            "text": text,
            "who": who,
            "date": today_iso,
            "status": "open",
            "hash": _content_hash(code, "record-ask", text),
        })
    return out
