"""Transcript ingest engine — Tier 1 Phase 1.1 / v0.8.6.

Two surfaces:

1. **Read-only audit (`cp parse-transcript`)** — parses a Fathom-style
   transcript file and emits structured JSON: speakers, duration, gaps,
   mentioned project codes, action items. Used by ``/cp-ingest`` to
   surface a confirmation prompt before any writes happen (catches the
   W19 "14-min audio gap" + "speaker misattribution" class of errors).

2. **Plan executor (`cp ingest --plan <file>`)** — validates a YAML
   plan against a schema, then executes each entry by appending a
   bracket-formatted bullet to the right sprint-file subsection (or to
   `_week.md` for themes). Idempotent via content-hash HTML comments
   trailing each appended bullet.

The classifier + planner live in the cp-engine plugin (Claude does
that via /cp-ingest). cp-engine itself is the deterministic executor.

See cp/docs/plans/2026-05-12-tier-1-design.md for full design.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

# ──────────────────────────────────────────────────────────────────────
#  Transcript audit (cp parse-transcript)
# ──────────────────────────────────────────────────────────────────────

# Fathom transcript format: lines like `0:04 - Drew Fiero (FirstPerson)` or
# `15:21 - Brandon Grande` (HH:MM or MM:SS prefix, then speaker name).
_TIMESTAMP_LINE_RE = re.compile(
    r"^(?P<ts>\d+:\d{2}(?::\d{2})?)\s+-\s+(?P<speaker>.+?)$",
    re.MULTILINE,
)

# Action items that Fathom inlines: `ACTION ITEM: <text> - WATCH: <url>`
_ACTION_ITEM_RE = re.compile(
    r"ACTION ITEM:\s*(?P<text>.+?)(?:\s*-\s*WATCH:|$)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class TranscriptGap:
    start: str  # original timestamp string
    end: str
    duration_minutes: int


@dataclass(frozen=True)
class TranscriptActionItem:
    text: str
    timestamp: str  # original timestamp string from the surrounding line


@dataclass
class TranscriptAudit:
    speakers: list[str] = field(default_factory=list)
    duration_minutes: int = 0
    gaps: list[TranscriptGap] = field(default_factory=list)
    mentioned_codes: list[str] = field(default_factory=list)
    action_items: list[TranscriptActionItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "speakers": self.speakers,
            "duration_minutes": self.duration_minutes,
            "gaps": [
                {"start": g.start, "end": g.end, "duration_minutes": g.duration_minutes}
                for g in self.gaps
            ],
            "mentioned_codes": self.mentioned_codes,
            "action_items": [
                {"text": a.text, "timestamp": a.timestamp}
                for a in self.action_items
            ],
        }


def parse_transcript(
    path: Path,
    *,
    gap_threshold_minutes: int = 2,
    project_codes: tuple[str, ...] = (),
) -> TranscriptAudit:
    """Audit a Fathom-style transcript.

    Args:
        path: file containing the transcript text.
        gap_threshold_minutes: any gap >= this between consecutive
            timestamped utterances is flagged. Default 2 minutes.
        project_codes: known project codes to scan for in transcript
            text. Empty tuple → mentioned_codes is empty (caller can
            fetch active projects from MC-2 separately and re-scan).

    Returns:
        TranscriptAudit with speakers (de-duplicated, in first-seen order),
        duration_minutes (last timestamp seen, rounded down), gaps (list
        of TranscriptGap for any gap >= threshold), mentioned_codes (sorted
        unique list of codes from project_codes that appear in the text),
        action_items (extracted from "ACTION ITEM:" lines).
    """
    body = path.read_text(encoding="utf-8")
    audit = TranscriptAudit()

    # Speakers + timestamps (in order).
    speakers_seen: list[str] = []
    seen_set: set[str] = set()
    timestamps: list[tuple[str, int]] = []  # (raw_ts_string, total_seconds)
    for m in _TIMESTAMP_LINE_RE.finditer(body):
        speaker_raw = m.group("speaker").strip()
        # Strip parenthetical "(FirstPerson)" / "(He/Him/His)" suffixes.
        speaker = re.sub(r"\s*\([^)]+\)\s*$", "", speaker_raw).strip()
        if speaker and speaker not in seen_set:
            seen_set.add(speaker)
            speakers_seen.append(speaker)
        ts = m.group("ts")
        timestamps.append((ts, _ts_to_seconds(ts)))
    audit.speakers = speakers_seen

    # Duration: highest timestamp seen.
    if timestamps:
        last_seconds = max(s for _, s in timestamps)
        audit.duration_minutes = last_seconds // 60

    # Gaps: any consecutive pair with delta >= threshold.
    threshold_seconds = gap_threshold_minutes * 60
    for (ts_a, sec_a), (ts_b, sec_b) in zip(timestamps, timestamps[1:]):
        delta = sec_b - sec_a
        if delta >= threshold_seconds:
            audit.gaps.append(
                TranscriptGap(
                    start=ts_a,
                    end=ts_b,
                    duration_minutes=delta // 60,
                )
            )

    # Mentioned codes: case-insensitive substring match against project_codes.
    body_lower = body.lower()
    mentioned: set[str] = set()
    for code in project_codes:
        if code.lower() in body_lower:
            mentioned.add(code)
    audit.mentioned_codes = sorted(mentioned)

    # Action items: each "ACTION ITEM:" line, with the nearest preceding timestamp.
    for m in _ACTION_ITEM_RE.finditer(body):
        text = m.group("text").strip()
        if not text:
            continue
        # Find the nearest timestamp before this match.
        preceding = body[: m.start()]
        ts_match = None
        for ts_m in _TIMESTAMP_LINE_RE.finditer(preceding):
            ts_match = ts_m
        ts_str = ts_match.group("ts") if ts_match else ""
        audit.action_items.append(TranscriptActionItem(text=text, timestamp=ts_str))

    return audit


def _ts_to_seconds(ts: str) -> int:
    """Parse `MM:SS` or `HH:MM:SS` into total seconds."""
    parts = [int(p) for p in ts.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0


# ──────────────────────────────────────────────────────────────────────
#  Plan executor (cp ingest --plan)
# ──────────────────────────────────────────────────────────────────────

# Verb types supported in the YAML plan. Each maps to an internal write
# function below. Adding a new verb means: (a) add to this enum, (b)
# write a corresponding _write_<verb>() function, (c) add a dispatch
# case in _execute_step.
_SUPPORTED_VERBS = (
    "record-inbound",      # → sprint file's ### Inbound under ## Client communication
    "record-ask",          # → sprint file's ### Open asks
    "close-ask",           # → flips an existing [open ...] to [closed ...]
    "add-decision",        # → sprint file's ### Decisions under ## Meeting notes & decisions
    "record-risk",         # → sprint file's ## Dependencies & risks
    "record-stakeholder",  # → sprint file's ### Stakeholders under ## Client communication
    "record-theme",        # → sprints/<W##>/_week.md's ## Themes
    "record-slack-digest", # → sprint file's ### Slack digest under ## Client communication
)


@dataclass
class IngestPlanResult:
    """Outcome of executing one plan."""

    files_written: list[Path] = field(default_factory=list)
    skipped_duplicate: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "files_written": [str(p) for p in self.files_written],
            "skipped_duplicate": self.skipped_duplicate,
            "errors": self.errors,
        }


class IngestPlanError(Exception):
    """Plan validation failed before execution."""


def execute_plan(
    plan: dict,
    *,
    tenant_root: Path,
    today: date,
    week_iso: str | None = None,
) -> IngestPlanResult:
    """Validate + execute an ingest plan against the cp tenant.

    Plan schema (see docs/plans/2026-05-12-tier-1-design.md):

        transcript:
          source: file | fathom
          path: <relative or absolute path; for audit log only>
        projects:
          <code>:
            inbound: [{date, who, text}]
            asks: [{text, who, by, status}]   # status defaults to "open"
            decisions: [{text, date, cross_cutting}]
            risks: [{text, severity, category, date}]
            stakeholders: [{name, role, context}]
        themes:                                  # tenant-wide
          - {text, date}

    Validation: only known verbs; all required fields present; project
    codes must correspond to existing sprint files in the target week.

    Execution: each entry is dispatched to the matching internal write
    function. Idempotent — appending the same (project, verb, text)
    twice is a no-op (skipped_duplicate counter increments).

    `week_iso` overrides the default of "current week derived from `today`".
    Used by the Slack-digest pipeline (P.3+): the Sunday cron runs in
    week N+1 but writes to week N's sprint files.
    """
    _validate_plan(plan)
    result = IngestPlanResult()
    week_iso = week_iso or _current_week_iso(today)

    projects_block = plan.get("projects") or {}
    for code, entries in projects_block.items():
        sprint_path = tenant_root / "sprints" / week_iso / f"{code}.md"
        if not sprint_path.exists():
            result.errors.append(
                f"sprint file missing for {code} (week {week_iso}): {sprint_path}"
            )
            continue
        for verb, items in entries.items():
            for item in items:
                try:
                    written = _execute_step(verb, code, item, sprint_path)
                    if written:
                        if sprint_path not in result.files_written:
                            result.files_written.append(sprint_path)
                    else:
                        result.skipped_duplicate += 1
                except Exception as exc:
                    result.errors.append(f"{code}/{verb}: {exc}")

    # Themes are tenant-wide; they go in _week.md.
    themes = plan.get("themes") or []
    if themes:
        week_path = tenant_root / "sprints" / week_iso / "_week.md"
        if not week_path.exists():
            result.errors.append(f"_week.md missing: {week_path}")
        else:
            for item in themes:
                try:
                    written = _write_theme(item, week_path)
                    if written:
                        if week_path not in result.files_written:
                            result.files_written.append(week_path)
                    else:
                        result.skipped_duplicate += 1
                except Exception as exc:
                    result.errors.append(f"theme: {exc}")

    # Phase B: account-level decisions are tenant-wide; they go in
    # weekly-cp.md's handwritten Decisions list. Cross-references to
    # specific projects come via `source: account: <company>` parsed by
    # cp_engine.agenda.decisions_for_project — but the company itself
    # isn't a project code, so these don't bucket under any one project.
    # They surface in weekly-cp.md and in /cp-prep agenda's tenant-wide
    # context block as "cross-cutting decisions, last 4 weeks."
    account_decisions = plan.get("account_decisions") or []
    if account_decisions:
        weekly_cp_path = tenant_root / "weekly-cp.md"
        if not weekly_cp_path.exists():
            result.errors.append(f"weekly-cp.md missing: {weekly_cp_path}")
        else:
            for item in account_decisions:
                try:
                    written = _write_account_decision(item, weekly_cp_path)
                    if written:
                        if weekly_cp_path not in result.files_written:
                            result.files_written.append(weekly_cp_path)
                    else:
                        result.skipped_duplicate += 1
                except Exception as exc:
                    result.errors.append(f"account-decision: {exc}")

    return result


# ──────────────────────────────────────────────────────────────────────
#  Plan validation
# ──────────────────────────────────────────────────────────────────────


def _validate_plan(plan: dict) -> None:
    if not isinstance(plan, dict):
        raise IngestPlanError("plan must be a mapping at top level")

    projects = plan.get("projects")
    if projects is not None and not isinstance(projects, dict):
        raise IngestPlanError("plan.projects must be a mapping {code: {verb: [items]}}")

    if projects:
        for code, entries in projects.items():
            if not isinstance(entries, dict):
                raise IngestPlanError(
                    f"plan.projects[{code!r}] must be a mapping {{verb: [items]}}"
                )
            for verb, items in entries.items():
                normalized = _normalize_verb(verb)
                if normalized not in _SUPPORTED_VERBS:
                    raise IngestPlanError(
                        f"plan.projects[{code!r}]: unknown verb {verb!r}; "
                        f"expected one of {sorted(_SUPPORTED_VERBS)}"
                    )
                if not isinstance(items, list):
                    raise IngestPlanError(
                        f"plan.projects[{code!r}][{verb!r}] must be a list of items"
                    )
                for i, item in enumerate(items):
                    if not isinstance(item, dict):
                        raise IngestPlanError(
                            f"plan.projects[{code!r}][{verb!r}][{i}] must be a mapping"
                        )

    themes = plan.get("themes")
    if themes is not None and not isinstance(themes, list):
        raise IngestPlanError("plan.themes must be a list of {text, date}")

    # Phase B: account_decisions is tenant-wide, parallel to themes.
    account_decisions = plan.get("account_decisions")
    if account_decisions is not None:
        if not isinstance(account_decisions, list):
            raise IngestPlanError(
                "plan.account_decisions must be a list of {text, company, date}"
            )
        for i, item in enumerate(account_decisions):
            if not isinstance(item, dict):
                raise IngestPlanError(
                    f"plan.account_decisions[{i}] must be a mapping"
                )


def _normalize_verb(verb: str) -> str:
    """Map plan-shorthand verb names ('inbound' → 'record-inbound')."""
    if verb in _SUPPORTED_VERBS:
        return verb
    shorthand = {
        "inbound": "record-inbound",
        "asks": "record-ask",
        "ask": "record-ask",
        "decisions": "add-decision",
        "decision": "add-decision",
        "risks": "record-risk",
        "risk": "record-risk",
        "stakeholders": "record-stakeholder",
        "stakeholder": "record-stakeholder",
        "themes": "record-theme",
        "theme": "record-theme",
        "slack_digest": "record-slack-digest",
        "slack-digest": "record-slack-digest",
    }
    return shorthand.get(verb, verb)


# ──────────────────────────────────────────────────────────────────────
#  Step dispatch + write functions
# ──────────────────────────────────────────────────────────────────────


def _execute_step(verb: str, code: str, item: dict, sprint_path: Path) -> bool:
    """Returns True if a new bullet was appended; False if it was a dup."""
    normalized = _normalize_verb(verb)
    handler = {
        "record-inbound": _write_inbound,
        "record-ask": _write_ask,
        "close-ask": _write_close_ask,
        "add-decision": _write_decision,
        "record-risk": _write_risk,
        "record-stakeholder": _write_stakeholder,
        "record-slack-digest": _write_slack_digest,
    }.get(normalized)
    if handler is None:
        # Themes are handled separately at the top level.
        raise IngestPlanError(f"verb {verb!r} is not project-scoped")
    return handler(code, item, sprint_path)


def _content_hash(code: str, verb: str, text: str) -> str:
    """8-char hex hash for idempotency. Inputs include project+verb+text
    so identical text in different projects/contexts don't false-collide.
    """
    raw = f"{code}|{verb}|{text}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:8]


def _hash_marker(hash_hex: str) -> str:
    return f"<!-- cp:hash={hash_hex} -->"


def _already_present(body: str, hash_hex: str) -> bool:
    return _hash_marker(hash_hex) in body


def _append_bullet_to_subsection(
    body: str,
    section_heading: str,
    subsection_heading: str | None,
    bullet: str,
) -> str:
    """Append a bullet to ``## section / ### subsection``.

    If ``subsection_heading`` is None, appends directly under the
    ``## section_heading``. Inserts before the next ``## `` or ``### ``
    heading (whichever comes first), or at section end. Strips the
    template-placeholder ``- _<...>_`` line if it's the only existing
    bullet (so the first real entry replaces the placeholder).
    """
    # Locate the section's start.
    section_re = re.compile(
        rf"^## {re.escape(section_heading)}\s*$", re.MULTILINE
    )
    sec_match = section_re.search(body)
    if not sec_match:
        raise ValueError(f"section '## {section_heading}' not found")

    section_start = sec_match.end()

    if subsection_heading:
        sub_re = re.compile(
            rf"^### {re.escape(subsection_heading)}\s*$", re.MULTILINE
        )
        sub_match = sub_re.search(body, pos=section_start)
        if not sub_match:
            # Auto-create the missing subsection. Bootstrap case: sprint files
            # scaffolded before v0.8.5 (or before whichever release added the
            # subsection in question) won't have it. Inserts the heading at the
            # end of the parent section, before the next top-level `## ` (or EOF).
            next_section_re = re.compile(r"^## ", re.MULTILINE)
            next_section_match = next_section_re.search(body, pos=section_start + 1)
            insert_pos = next_section_match.start() if next_section_match else len(body)
            new_heading = f"\n### {subsection_heading}\n"
            body = body[:insert_pos] + new_heading + body[insert_pos:]
            # Re-locate after insertion.
            sub_match = sub_re.search(body, pos=section_start)
            assert sub_match, "subsection insertion failed"
        insert_zone_start = sub_match.end()
    else:
        insert_zone_start = section_start

    # Find the end of the insert zone: next ## or ### heading.
    next_heading_re = re.compile(r"^(?:##|###) ", re.MULTILINE)
    next_match = next_heading_re.search(body, pos=insert_zone_start + 1)
    insert_zone_end = next_match.start() if next_match else len(body)

    zone = body[insert_zone_start:insert_zone_end]

    # If zone contains only the template placeholder (a `- _<...>_` line),
    # replace the whole zone with the new bullet. Otherwise append after
    # existing content.
    placeholder_re = re.compile(r"^\s*-\s+_<[^>]+>_\s*$", re.MULTILINE)
    placeholders = placeholder_re.findall(zone)
    real_bullets = [
        ln for ln in zone.splitlines()
        if ln.strip().startswith("- ") and not placeholder_re.match(ln)
    ]
    if placeholders and not real_bullets:
        # Replace the zone (preserve leading/trailing newlines).
        new_zone = f"\n{bullet}\n"
        return body[:insert_zone_start] + new_zone + body[insert_zone_end:]

    # Append the bullet to the zone, preserving trailing newline structure.
    if not zone.endswith("\n"):
        zone += "\n"
    new_zone = zone + bullet + "\n"
    return body[:insert_zone_start] + new_zone + body[insert_zone_end:]


def _write_inbound(code: str, item: dict, sprint_path: Path) -> bool:
    text = (item.get("text") or "").strip()
    date_s = (item.get("date") or "").strip()
    who = (item.get("who") or "").strip()
    if not text:
        raise IngestPlanError("inbound item missing 'text'")
    if not date_s:
        raise IngestPlanError("inbound item missing 'date'")
    h = _content_hash(code, "record-inbound", text)
    body = sprint_path.read_text(encoding="utf-8")
    if _already_present(body, h):
        return False
    bullet = f"- [{date_s} · {who}] {text} {_hash_marker(h)}"
    new = _append_bullet_to_subsection(body, "Client communication", "Inbound", bullet)
    sprint_path.write_text(new)
    return True


def _write_ask(code: str, item: dict, sprint_path: Path) -> bool:
    text = (item.get("text") or "").strip()
    who = (item.get("who") or "").strip()
    by = (item.get("by") or "").strip()
    asked_date = (item.get("date") or item.get("asked_date") or _today_iso()).strip()
    status = (item.get("status") or "open").strip()
    if not text:
        raise IngestPlanError("ask item missing 'text'")
    h = _content_hash(code, "record-ask", text)
    body = sprint_path.read_text(encoding="utf-8")
    if _already_present(body, h):
        return False
    by_str = f" · by {by}" if by else ""
    bullet = f"- [{status} · {asked_date} · {who}{by_str}] {text} {_hash_marker(h)}"
    new = _append_bullet_to_subsection(body, "Client communication", "Open asks", bullet)
    sprint_path.write_text(new)
    return True


def _write_close_ask(code: str, item: dict, sprint_path: Path) -> bool:
    """Flip an existing `[open ...]` ask to `[closed ...]`.

    Match strategy: substring of `text` against existing ask bullets.
    Returns True if a flip happened; False if no match found OR the ask
    was already closed (idempotent).
    """
    match_text = (item.get("text") or item.get("match") or "").strip()
    if not match_text:
        raise IngestPlanError("close-ask item missing 'text' (or 'match')")
    body = sprint_path.read_text(encoding="utf-8")
    # Find the first bullet under Open asks containing match_text, with status open.
    open_bullet_re = re.compile(
        r"^(?P<prefix>- `?\[)open(?P<rest>[^\]]*\][^\n]*"
        + re.escape(match_text)
        + r"[^\n]*)$",
        re.MULTILINE,
    )
    new_body, n = open_bullet_re.subn(
        lambda m: m.group("prefix") + "closed" + m.group("rest"),
        body,
        count=1,
    )
    if n == 0:
        return False
    sprint_path.write_text(new_body)
    return True


def _write_decision(code: str, item: dict, sprint_path: Path) -> bool:
    text = (item.get("text") or "").strip()
    date_s = (item.get("date") or _today_iso()).strip()
    cross = bool(item.get("cross_cutting") or item.get("cross-cutting") or False)
    if not text:
        raise IngestPlanError("decision item missing 'text'")
    h = _content_hash(code, "add-decision", text)
    body = sprint_path.read_text(encoding="utf-8")
    if _already_present(body, h):
        return False
    cross_marker = "[cross-cutting]" if cross else ""
    bullet = f"- [decision · {date_s}]{cross_marker} {text} {_hash_marker(h)}"
    new = _append_bullet_to_subsection(
        body, "Meeting notes & decisions", "Decisions", bullet
    )
    sprint_path.write_text(new)
    return True


def _write_risk(code: str, item: dict, sprint_path: Path) -> bool:
    text = (item.get("text") or "").strip()
    severity = (item.get("severity") or "watching").strip()
    category = (item.get("category") or "").strip()
    raised = (item.get("date") or item.get("raised_date") or _today_iso()).strip()
    if not text:
        raise IngestPlanError("risk item missing 'text'")
    h = _content_hash(code, "record-risk", text)
    body = sprint_path.read_text(encoding="utf-8")
    if _already_present(body, h):
        return False
    bullet = f"- [{severity} · {category} · {raised}] {text} {_hash_marker(h)}"
    new = _append_bullet_to_subsection(body, "Dependencies & risks", None, bullet)
    sprint_path.write_text(new)
    return True


def _write_stakeholder(code: str, item: dict, sprint_path: Path) -> bool:
    name = (item.get("name") or "").strip()
    role = (item.get("role") or "").strip()
    context = (item.get("context") or "").strip()
    if not name:
        raise IngestPlanError("stakeholder item missing 'name'")
    h = _content_hash(code, "record-stakeholder", name)  # dedupe by name
    body = sprint_path.read_text(encoding="utf-8")
    if _already_present(body, h):
        return False
    parts = [name]
    if role:
        parts.append(role)
    if context:
        parts.append(context)
    bullet = f"- [{' · '.join(parts)}] {_hash_marker(h)}"
    new = _append_bullet_to_subsection(
        body, "Client communication", "Stakeholders", bullet
    )
    sprint_path.write_text(new)
    return True


def _write_slack_digest(code: str, item: dict, sprint_path: Path) -> bool:
    """Write one weekly Slack-digest bullet under ## Client communication / ### Slack digest.

    Hash includes the ISO week so the same digest text in two different
    weeks doesn't collide. Re-running for the same `(code, week)` is a
    no-op — the bullet is already present.
    """
    text = (item.get("text") or "").strip()
    week = (item.get("week") or "").strip()
    if not text:
        raise IngestPlanError("slack-digest item missing 'text'")
    if not week:
        raise IngestPlanError("slack-digest item missing 'week' (e.g. '2026-W19')")
    # Hash key embeds the week so two weeks with similar summaries don't
    # collide; re-running for the same week stays idempotent.
    h = _content_hash(code, "record-slack-digest", f"{week}|{text}")
    body = sprint_path.read_text(encoding="utf-8")
    if _already_present(body, h):
        return False
    bullet = f"- [{week} · Slack] {text} {_hash_marker(h)}"
    new = _append_bullet_to_subsection(
        body, "Client communication", "Slack digest", bullet
    )
    sprint_path.write_text(new)
    return True


def _write_theme(item: dict, week_path: Path) -> bool:
    text = (item.get("text") or "").strip()
    date_s = (item.get("date") or _today_iso()).strip()
    if not text:
        raise IngestPlanError("theme item missing 'text'")
    h = _content_hash("__tenant__", "record-theme", text)
    body = week_path.read_text(encoding="utf-8")
    if _already_present(body, h):
        return False
    bullet = f"- [theme · {date_s}] {text} {_hash_marker(h)}"
    new = _append_bullet_to_subsection(body, "Themes", None, bullet)
    week_path.write_text(new)
    return True


def _write_account_decision(item: dict, weekly_cp_path: Path) -> bool:
    """Phase B: append an account-level decision to weekly-cp.md.

    Format matches the existing handwritten numbered-list style:
      <N+1>. **<text>** (YYYY-MM-DD, source: account: <company-lower>) <hash-marker>

    Where <N+1> is one greater than the highest numbered decision already
    present, found via regex on lines like "19. **...** ...".

    Inserted before the first cp-engine marker (so it lands inside the
    handwritten section, not inside the engine-managed strip regions).
    The cross-reference parser in cp_engine.agenda picks up
    `source: account: <company-lower>` automatically (see Phase B Q5
    in the cascade design doc).

    Idempotency via content hash on (company, "record-account-decision",
    text). Re-running the same plan is a no-op.
    """
    text = (item.get("text") or "").strip()
    company = (item.get("company") or "").strip().lower()
    date_s = (item.get("date") or _today_iso()).strip()
    if not text:
        raise IngestPlanError("account-decision item missing 'text'")
    if not company:
        raise IngestPlanError("account-decision item missing 'company'")
    h = _content_hash(company, "record-account-decision", text)
    body = weekly_cp_path.read_text(encoding="utf-8")
    if _already_present(body, h):
        return False

    # Find highest existing decision number. Pattern matches lines like:
    #   "19. **..." OR "1. **..." (any digit count, leading whitespace optional).
    decision_nums = [
        int(m.group(1))
        for m in re.finditer(r"^\s*(\d+)\.\s+\*\*", body, re.MULTILINE)
    ]
    next_num = (max(decision_nums) + 1) if decision_nums else 1

    # New decision line. Two newlines before so it gets its own paragraph
    # (matches the spacing of existing entries in weekly-cp.md).
    new_line = (
        f"{next_num}. **{text}** "
        f"({date_s}, source: account: {company}) {_hash_marker(h)}"
    )

    # Insert location: before the first cp-engine marker (so it lands in
    # the handwritten section), or before "## Active research" as a
    # fallback, or at end of file as last resort.
    insertion_anchors = [
        "<!-- cp-engine:start themes-strip -->",
        "<!-- cp-engine:start decisions-strip -->",
        "## Active research",
    ]
    anchor_pos = -1
    for anchor in insertion_anchors:
        pos = body.find(anchor)
        if pos != -1:
            anchor_pos = pos
            break

    if anchor_pos == -1:
        # Append at end of file.
        if not body.endswith("\n"):
            body += "\n"
        new = body + "\n" + new_line + "\n"
    else:
        # Insert just before the anchor, with blank-line padding.
        new = body[:anchor_pos] + new_line + "\n\n" + body[anchor_pos:]

    weekly_cp_path.write_text(new)
    return True


# ──────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────


def _today_iso() -> str:
    return datetime.now().date().isoformat()


def _current_week_iso(today: date) -> str:
    from datetime import timedelta
    monday = today - timedelta(days=today.weekday())
    return f"{monday.year}-W{monday.strftime('%W')}"
