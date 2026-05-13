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
  threads (Quick Resume, current sprint's Open asks, recent decisions)
  without being buried in history.
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
    model: str = "claude-opus-4-7",
    api_key: str | None = None,
) -> GeneratedPlan:
    """Read transcript + project context, ask Claude for a plan, validate it.

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

    Returns the project's `cp.md` (truncated) + its current sprint file
    (truncated). Both are markdown; the model can read them as-is.
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

    return "\n\n".join(parts)


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


def _build_prompt(
    *,
    transcript: str,
    project_context: str,
    project_code: str,
    transcript_path: Path,
) -> str:
    today = datetime.now().date().isoformat()
    return _PROMPT_TEMPLATE.format(
        today=today,
        project_code=project_code,
        transcript_relpath=str(transcript_path),
        project_context=project_context,
        transcript=transcript,
    )


_PROMPT_TEMPLATE = """\
You are extracting structured updates from a meeting transcript for the cp-engine
context-protocol system. Your output is a YAML plan that `cp ingest` will execute
against the project's sprint file.

# Today
{today}

# Target project
{project_code}

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
```

# Rules

1. **Only include verbs that have entries.** Empty lists like `decisions: []`
   are forbidden. If you have nothing to say for a verb, omit it entirely.
2. **Don't duplicate what's already in the project context.** If a decision
   or ask already appears in the sprint file or cp.md (even paraphrased),
   skip it. The system has its own dedup, but you should still try.
3. **Decisions vs inbound:** a decision is a *commitment made in the meeting*
   (who's doing what, by when, what we agreed to). Inbound is *information*
   the client conveyed (status, opinions, constraints, concerns).
4. **Asks** are open loops *we're waiting on someone for*. If the meeting
   resolved an existing ask, do NOT create a new entry — we'll handle
   close-ask in a different flow.
5. **Stakeholders only when genuinely new.** Don't restate Maria, Brandon,
   etc. who already appear in cp.md.
6. **Date fields:** use the meeting date if known (parse from transcript
   header), otherwise today ({today}). All dates as ISO YYYY-MM-DD.
7. **Quote-like fidelity, no embellishment.** The text should reflect what
   was actually said. Don't add interpretation or speculation. If a
   commitment is vague ("we should look at that"), it's NOT a decision.
8. **One project only.** Even if the meeting touches other projects, only
   record items relevant to {project_code}. Cross-project items belong in
   themes (handled separately).

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


def _call_claude(prompt: str, *, model: str, api_key: str | None) -> str:
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

    client = Anthropic(api_key=key)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
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
    return text


_FENCE_RE = re.compile(r"```(?:yaml)?\s*\n?(.*?)\n?```", re.DOTALL)


def _extract_yaml(response_text: str) -> str:
    """Pull the YAML out of a fenced code block. If no fence, return as-is."""
    match = _FENCE_RE.search(response_text)
    if match:
        return match.group(1).strip()
    return response_text.strip()
