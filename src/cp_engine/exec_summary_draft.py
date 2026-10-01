"""Machine-drafted Exec Summaries for stale workstreams (#251).

WHY THIS EXISTS. The Exec Summary is the most-read surface in the tenant and
the least-written: measured over 90 days, one person authored 156 of 183
rewrites. The 2026-06-30 cutover (`docs/plans/2026-06-30-exec-summary.md`)
gave the model all prose and assumed the model would be invoked at wrap-up,
per project, reliably. That held for one operator. Every other workstream's
summary drifts until someone notices, while the sprint files beside it fill
with current, ingested fact.

Approved 2026-09-30 as #251's resolution: the engine drafts the four STATE
fields (Status, Where it stands, Next up, Blockers) for every active
workstream whose summary is stale or partially refreshed, and writes them
directly, visibly marked. Objective stays human-owned and is never written.

THE SEAM, RESTATED. The cutover's rule was "the engine does not author
prose". This module does not relax it quietly; it relaxes it under three
constraints that make the draft checkable:

1. GROUNDED. The draft is made only from THIS workstream's own recent
   material: its last four sprint files (hand-written sections and
   carry-forward — never the engine's derived regions), dated `## Decisions`
   in its cp.md, its open MC-2 commitments, and its open questions. No other
   workstream's content and no general knowledge reach the prompt.
2. SCORED. Every drafted field is scored with `distill_fidelity.assess`
   against the concatenated sources, and checked for dates and names that do
   not occur in them. One failing field means NOTHING is written for that
   workstream — a partially trusted summary is worse than a stale one,
   because it reads as current.
3. MARKED. The written summary's `· updated` stamp carries
   `· drafted by cp (from …)`, naming its sources. Any human write through
   `exec_summary_merge` (capture_project_state, the webhook) rewrites the
   whole stamp line, so the marker is cleared by the first human refresh —
   see `exec_summary_merge._stamp_line`.

The write goes through `merge_exec_summary_fields`, the same function the
`capture_project_state` webhook uses. Nothing here splices text by hand.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from cp_engine import distill_fidelity
from cp_engine.exec_summary_lint import lint_exec_summary
from cp_engine.render import slice_exec_summary_region

logger = logging.getLogger(__name__)

# The current Claude family. `CP_DRAFT_MODEL` or `--model` overrides it.
DEFAULT_MODEL = "claude-opus-5-5"

# The four fields this module writes. Objective is human-owned; Updates and
# Last session are logs.
DRAFTED_FIELDS: tuple[str, ...] = ("Status", "Where it stands", "Next up", "Blockers")

# A summary whose stamp is this old is in scope. Matches the planning bundle's
# STALE threshold and the hosted capture guard (`_EXEC_STALE_GUARD_DAYS`).
STALE_AFTER_DAYS = 14

# How many sprint weeks of material ground a draft.
SOURCE_WEEKS = 4

# The marker. Rendered on the stamp heading; detected by `is_drafted`.
DRAFTED_MARKER = "drafted by cp"
_DRAFTED_RE = re.compile(r"·\s*drafted by cp\b")

_STAMP_RE = re.compile(
    r"^##\s+Exec Summary\s*·\s*updated\s+(?P<date>\d{4}-\d{2}-\d{2})",
    re.MULTILINE,
)
_REGION_RE = re.compile(
    r"<!-- cp-engine:start (?P<name>[\w-]+) -->\n?(?P<body>.*?)<!-- cp-engine:end (?P=name) -->\n?",
    re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s")
_EMPTY_LABEL_RE = re.compile(r"^\*\*[^*]+:\*\*\s*$")
_PLACEHOLDER_BULLET_RE = re.compile(r"^-\s+_(?:No|Nothing|None)\b[^_]*_\s*$")
# carry-forward's rollup of items too old to carry individually. It is the
# engine's own hygiene prompt, not project state, and a draft that repeats it
# ("triage the 3 stale risks") says nothing about the work.
_STALE_ROLLUP_RE = re.compile(r"^-\s+\d+\s+stale\b.*\btriage in\b")
_DECISION_DATE_RE = re.compile(r"\((\d{4}-\d{2}-\d{2})")
_MEETING_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-.+\.md$")

# The only derived sprint-file region whose content is material: it carries
# the open items of the prior week forward. Every other region
# (`sprint-facts`, `where-it-stands`, `deliverable-cards`) is a render of MC-2
# or git state, not something that happened in the work.
_KEPT_REGIONS = frozenset({"carry-forward"})


# ──────────────────────────────────────────────────────────────────────
#  Marker
# ──────────────────────────────────────────────────────────────────────


def is_drafted(cp_md_text: str) -> bool:
    """True when the Exec Summary's stamp carries the drafted-by-cp marker."""
    m = _STAMP_RE.search(cp_md_text or "")
    if m is None:
        return False
    line_end = cp_md_text.find("\n", m.start())
    line = cp_md_text[m.start(): line_end if line_end != -1 else None]
    return bool(_DRAFTED_RE.search(line))


def mark_drafted(cp_md_text: str, source_note: str) -> str:
    """Append `· drafted by cp (from <source_note>)` to the stamp line.

    Called only after `merge_exec_summary_fields` has rewritten the line to
    `## Exec Summary  ·  updated <today>`, so the marker always sits on a
    stamp the draft itself set.
    """
    m = _STAMP_RE.search(cp_md_text)
    if m is None:
        return cp_md_text
    line_end = cp_md_text.find("\n", m.start())
    if line_end == -1:
        line_end = len(cp_md_text)
    note = f" (from {source_note})" if source_note else ""
    line = f"{m.group(0)}  ·  {DRAFTED_MARKER}{note}"
    return cp_md_text[: m.start()] + line + cp_md_text[line_end:]


# ──────────────────────────────────────────────────────────────────────
#  Scope
# ──────────────────────────────────────────────────────────────────────


def stamp_date(cp_md_text: str) -> date | None:
    m = _STAMP_RE.search(cp_md_text or "")
    if m is None:
        return None
    try:
        return date.fromisoformat(m.group("date"))
    except ValueError:
        return None


def scope_reason(cp_md_path: Path, *, today: date) -> str | None:
    """Why this summary needs a draft — `stale <n>d` or `partial refresh` — or
    None when it is current (or has no Exec Summary region to write into).

    Partial-refresh detection reads git blame (a full-history checkout); on a
    shallow clone it answers None and only the stamp age decides.
    """
    from cp_engine.exec_summary_freshness import partial_refresh

    try:
        text = cp_md_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if slice_exec_summary_region(text) is None:
        return None
    stamp = stamp_date(text)
    if stamp is None:
        return None
    age = (today - stamp).days
    reasons = []
    if age >= STALE_AFTER_DAYS:
        reasons.append(f"stale {age}d")
    if partial_refresh(cp_md_path) is not None:
        reasons.append("partial refresh")
    return ", ".join(reasons) or None


# ──────────────────────────────────────────────────────────────────────
#  Sources
# ──────────────────────────────────────────────────────────────────────


@dataclass
class Sources:
    """Everything a draft may be made from, and a count of each kind."""

    weeks: list[str] = field(default_factory=list)  # weeks with material
    sprint_text: list[tuple[str, str]] = field(default_factory=list)  # (week, text)
    decisions: list[str] = field(default_factory=list)
    commitments: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    meetings: int = 0
    objective: str | None = None

    def is_empty(self) -> bool:
        return not (self.sprint_text or self.decisions or self.commitments
                    or self.open_questions)

    def text(self) -> str:
        """The concatenated sources — the prompt body AND the fidelity
        reference. One string for both, so the score is over exactly what the
        model saw."""
        parts: list[str] = []
        if self.objective:
            parts.append(f"## Objective (human-owned, context only)\n{self.objective}")
        for week, body in self.sprint_text:
            parts.append(f"## Sprint file {week}\n{body}")
        if self.decisions:
            parts.append("## Decisions (cp.md)\n" + "\n".join(self.decisions))
        if self.commitments:
            parts.append("## Open commitments (MC-2)\n" + "\n".join(self.commitments))
        if self.open_questions:
            parts.append("## Open questions\n" + "\n".join(self.open_questions))
        return "\n\n".join(parts)

    def note(self) -> str:
        """The short source list the marker carries."""
        bits: list[str] = []
        if self.weeks:
            w = [x.split("-")[-1] for x in self.weeks]
            span = w[0] if len(w) == 1 else f"{w[0]}–{w[-1]}"
            bits.append(f"{span} sprint {'file' if len(w) == 1 else 'files'}")
        for n, one, many in (
            (self.meetings, "meeting", "meetings"),
            (len(self.decisions), "decision", "decisions"),
            (len(self.commitments), "commitment", "commitments"),
            (len(self.open_questions), "open question", "open questions"),
        ):
            if n:
                bits.append(f"{n} {one if n == 1 else many}")
        return ", ".join(bits)


def hand_written_sprint_text(body: str) -> str:
    """A sprint file's material with the scaffolding removed.

    Drops the frontmatter, the title and nav lines, every derived region
    except `carry-forward`, every HTML comment (the template's `<!-- … -->`
    field hints and the `cp:hash` tags), empty labels, placeholder bullets,
    and any heading with nothing beneath it. What is left is what people and
    ingest actually wrote this week.
    """
    body = _FRONTMATTER_RE.sub("", body or "", count=1)
    body = _REGION_RE.sub(
        lambda m: m.group("body") if m.group("name") in _KEPT_REGIONS else "", body
    )
    body = _COMMENT_RE.sub("", body)

    lines: list[str] = []
    for raw in body.splitlines():
        line = raw.rstrip()
        s = line.strip()
        if not s:
            continue
        if s.startswith("# ") or s.startswith("← "):
            continue  # title / nav
        if _EMPTY_LABEL_RE.match(s) or _PLACEHOLDER_BULLET_RE.match(s):
            continue
        if s.startswith("_See [sprint file]") or s.startswith("_(no "):
            continue
        if _STALE_ROLLUP_RE.match(s):
            continue  # carry-forward's "N stale asks — triage in W##" hygiene line
        lines.append(line)

    # Keep a heading only if some content sits under it before the next
    # heading of the same or a higher level.
    kept: list[str] = []
    for i, line in enumerate(lines):
        h = _HEADING_RE.match(line)
        if h is None:
            kept.append(line)
            continue
        level = len(h.group("hashes"))
        has_content = False
        for nxt in lines[i + 1:]:
            hn = _HEADING_RE.match(nxt)
            if hn is None:
                has_content = True
                break
            if len(hn.group("hashes")) <= level:
                break
        if has_content:
            kept.append(line)
    return "\n".join(kept).strip()


def recent_weeks(tenant_root: Path, current_week: str, n: int = SOURCE_WEEKS) -> list[str]:
    """The `n` newest sprint-week dirs at or before `current_week`, oldest first."""
    sprints = tenant_root / "sprints"
    if not sprints.is_dir():
        return []
    weeks = sorted(
        d.name for d in sprints.iterdir()
        if d.is_dir() and re.fullmatch(r"\d{4}-W\d{2}", d.name) and d.name <= current_week
    )
    return weeks[-n:]


def _week_monday(week_iso: str) -> date:
    year, wk = week_iso.split("-W")
    return date.fromisocalendar(int(year), int(wk), 1)


def dated_decisions(cp_md_text: str, *, since: date) -> list[str]:
    """`## Decisions` entries in cp.md dated on or after `since`.

    An entry is a top-level `- ` or `N. ` item with its continuation lines.
    Its date is the first `(YYYY-MM-DD` parenthetical — the shape both the
    hand-written `(2026-05-08)` and the auto-ingest
    `(2026-06-10, source: account: ggl)` forms take. Undated entries are left
    out: the draft is of CURRENT state, and an undated decision cannot be
    placed in time.
    """
    m = re.search(r"^## Decisions\s*$", cp_md_text or "", re.MULTILINE)
    if m is None:
        return []
    rest = cp_md_text[m.end():]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    section = _COMMENT_RE.sub("", rest[: nxt.start()] if nxt else rest)

    entries: list[list[str]] = []
    for raw in section.splitlines():
        s = raw.strip()
        if re.match(r"^(?:-|\d+\.)\s+", raw):
            entries.append([s])
        elif s and entries and raw[:1].isspace():
            entries[-1].append(s)
    out: list[str] = []
    for e in entries:
        text = " ".join(e)
        d = _DECISION_DATE_RE.search(text)
        if d is None:
            continue
        try:
            when = date.fromisoformat(d.group(1))
        except ValueError:
            continue
        if when >= since:
            out.append(text if text.startswith("- ") else f"- {text}")
    return out


def _commitment_line(c: dict) -> str:
    direction = {"them_to_us": "them→us", "us_to_them": "us→them"}.get(
        c.get("direction") or "", c.get("direction") or "internal")
    bits = [direction]
    if c.get("due_date"):
        bits.append(f"due {c['due_date']} ({c.get('date_status') or 'proposed'})")
    owner = c.get("owner_name") or c.get("owner_email")
    if owner:
        bits.append(str(owner))
    return f"- [{' · '.join(bits)}] {(c.get('description') or '').strip()}"


def gather_sources(
    tenant_root: Path,
    project,
    work_dir: Path,
    *,
    current_week: str,
    commitments: list[dict] | tuple[dict, ...] = (),
) -> Sources:
    """This workstream's own recent material, and nothing else."""
    from cp_engine.exec_summary_lint import _split_fields

    src = Sources()
    # The window is the four sprint weeks ending with the current one, by
    # the calendar — not by which week dirs happen to exist.
    since = _week_monday(current_week) - timedelta(weeks=SOURCE_WEEKS - 1)
    weeks = [w for w in recent_weeks(tenant_root, current_week) if _week_monday(w) >= since]

    cp_md = work_dir / "cp.md"
    cp_text = cp_md.read_text(encoding="utf-8") if cp_md.is_file() else ""
    region = slice_exec_summary_region(cp_text) or ""
    objective = _split_fields(region).get("Objective", "").strip()
    if objective and "_<" not in objective:
        src.objective = objective

    for week in weeks:
        path = tenant_root / "sprints" / week / f"{project.code}.md"
        if not path.is_file():
            continue
        text = hand_written_sprint_text(path.read_text(encoding="utf-8"))
        # A file whose only remaining lines are headings says nothing.
        if not any(not _HEADING_RE.match(ln) for ln in text.splitlines()):
            continue
        src.weeks.append(week)
        src.sprint_text.append((week, text))

    if weeks:
        latest = tenant_root / "sprints" / weeks[-1] / f"{project.code}.md"
        if latest.is_file():
            from cp_engine.prep_planning import _sprint_open_questions

            for q in _sprint_open_questions(latest):
                raised = f" (raised {q['raised_date']})" if q.get("raised_date") else ""
                src.open_questions.append(f"- {q['text']}{raised}")

    src.decisions = dated_decisions(cp_text, since=since)
    meetings_dir = work_dir / "meetings"
    if meetings_dir.is_dir():
        for f in meetings_dir.iterdir():
            mm = _MEETING_FILE_RE.match(f.name)
            if mm and date.fromisoformat(mm.group(1)) >= since:
                src.meetings += 1

    src.commitments = [_commitment_line(c) for c in commitments if c.get("description")]
    return src


# ──────────────────────────────────────────────────────────────────────
#  Drafting
# ──────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You write the current-state fields of a project's Exec Summary for a small \
creative agency's shared project index. Partners read it on Monday morning to \
decide what to do this week.

Use ONLY the material you are given for this one project. It is the project's \
own sprint files, decisions, commitments and open questions. Do not add \
anything from general knowledge, and do not guess. Every person, company, \
date, deliverable and number you write must appear in the material. If the \
material does not say something, leave it out rather than fill it in.

Prefer the newest material: a later sprint file supersedes an earlier one, \
and an item marked done, resolved, answered or struck through is no longer \
open. The Objective is context only; do not restate it.

Write plainly: short sentences, no hype, no filler. Keep the project's own \
names and phrases where you can.
"""

_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "where_it_stands", "next_up", "blockers"],
    "properties": {
        "status": {"type": "string"},
        "where_it_stands": {"type": "array", "items": {"type": "string"}},
        "next_up": {"type": "array", "items": {"type": "string"}},
        "blockers": {"type": "array", "items": {"type": "string"}},
    },
}


def build_prompt(project_label: str, sources: Sources, *, today: date) -> str:
    return (
        f"Project: {project_label}\nToday: {today.isoformat()}\n\n"
        "Write four fields as JSON:\n"
        "- status: one or two sentences, at most 60 words — where the work is "
        "right now.\n"
        "- where_it_stands: 2 to 5 bullets, each at most 35 words — the "
        "current reality, newest first.\n"
        "- next_up: 1 to 6 bullets — the concrete next actions, with the "
        "owner and date when the material gives them.\n"
        "- blockers: 0 to 5 bullets — what is actually holding the work up. "
        "An empty list when the material records none.\n"
        "Bullets are plain text without a leading dash.\n\n"
        "<material>\n" + sources.text() + "\n</material>\n"
    )


class DraftError(Exception):
    """The model call failed or returned something unusable."""


def anthropic_llm(model: str | None = None, *, timeout: float = 300) -> Callable[[str, str], str]:
    """A `(system, prompt) -> JSON text` callable backed by the Anthropic API.

    Reads ANTHROPIC_API_KEY, as every other LLM path in the engine does.
    Structured output keeps the reply parseable; adaptive thinking is the
    model's default and is left on.
    """
    model = model or os.environ.get("CP_DRAFT_MODEL") or DEFAULT_MODEL

    def call(system: str, prompt: str) -> str:
        try:
            from anthropic import Anthropic
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise DraftError("anthropic package not installed") from exc
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise DraftError("ANTHROPIC_API_KEY not set")
        client = Anthropic(api_key=key, timeout=timeout)
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "effort": "medium",
                    "format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA},
                },
            )
        except Exception as exc:  # noqa: BLE001 — SDK raises many subclasses
            raise DraftError(f"Anthropic API call failed: {exc}") from exc
        if getattr(resp, "stop_reason", None) == "refusal":
            raise DraftError("model declined the request (stop_reason=refusal)")
        if getattr(resp, "stop_reason", None) == "max_tokens":
            raise DraftError("model reply truncated at max_tokens")
        text = "".join(
            getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
        )
        if not text.strip():
            raise DraftError("model returned no text")
        return text

    return call


def parse_draft(reply: str) -> dict[str, str | list[str]]:
    """Model JSON → `{field label: value}` in `merge_exec_summary_fields` shape."""
    text = reply.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DraftError(f"reply is not JSON: {exc}") from exc

    def bullets(key: str) -> list[str]:
        vals = data.get(key) or []
        if isinstance(vals, str):
            vals = [vals]
        return [re.sub(r"^[-*]\s+", "", str(v).strip()) for v in vals if str(v).strip()]

    status = str(data.get("status") or "").strip()
    where = bullets("where_it_stands")
    nxt = bullets("next_up")
    if not status or not where or not nxt:
        raise DraftError("reply is missing Status, Where it stands or Next up")
    return {
        "Status": status,
        "Where it stands": where,
        "Next up": nxt,
        "Blockers": bullets("blockers") or ["None recorded."],
    }


# ──────────────────────────────────────────────────────────────────────
#  Checks
# ──────────────────────────────────────────────────────────────────────

_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_SLASH_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
           "sept", "oct", "nov", "dec")
_MONTH_NAMES = frozenset("""
january february march april may june july august september october november
december
""".split())
_MONTH_DAY_RE = re.compile(
    r"\b(?P<mon>(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*)\.?\s+(?P<day>\d{1,2})\b",
    re.IGNORECASE,
)
_WEEK_RE = re.compile(r"\bW\d{2}\b")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
# A token that opens a sentence, a bullet or a clause is capitalised by
# grammar, not because it names something ("Send the deck", "Pricing is
# open"), so only an ALL-CAPS token is checked there. Everywhere else a
# capitalised token is a name and must occur in the material.
_SENTENCE_OPENERS = ".!?:;—–-(\"'“‘\n"
# Capitalised words any status line may use that name nothing.
_GENERIC_CAPS = frozenset("""
none awaiting await waiting next status blockers blocker the this that these
those once when then after before until while with without from into onto
for and but not now new still also only all any each both final first last
pending open closed done draft drafts review reviewed follow confirm confirmed
monday tuesday wednesday thursday friday saturday sunday today tomorrow week
weeks month months sprint sprints recorded work working client clients team
internal round rounds phase phases scope sow kickoff deck decks feedback
""".split())


def _month_day_in(source_lower: str, mon: str, day: str) -> bool:
    mon3 = mon.lower()[:3]
    num = _MONTHS.index(mon3) + 1 if mon3 in _MONTHS else None
    if num is not None:
        if re.search(rf"\b\d{{4}}-{num:02d}-{int(day):02d}\b", source_lower):
            return True
        if re.search(rf"\b{num}/{int(day)}\b", source_lower):
            return True
    return re.search(rf"\b{mon3}[a-z]*\.?\s+{int(day)}\b", source_lower) is not None


def unsupported_specifics(body: str, source: str) -> list[str]:
    """Dates, week labels and proper names in `body` that `source` never
    contains. The fidelity score judges phrasing; this judges the one kind of
    invention a phrase score can miss — a single wrong date or name inside an
    otherwise faithful sentence.
    """
    src_lower = (source or "").lower()
    src_tokens = {t.lower() for t in _TOKEN_RE.findall(source or "")}
    src_stems = {t[:6] for t in src_tokens}
    missing: list[str] = []

    for d in _ISO_DATE_RE.findall(body):
        if d not in source:
            missing.append(d)
    for d in _SLASH_DATE_RE.findall(body):
        if d not in source:
            # 10/15 may appear as 2026-10-15 in the material.
            parts = d.split("/")
            iso = f"-{int(parts[0]):02d}-{int(parts[1]):02d}"
            if iso not in source:
                missing.append(d)
    for m in _MONTH_DAY_RE.finditer(body):
        if not _month_day_in(src_lower, m.group("mon"), m.group("day")):
            missing.append(m.group(0))
    for w in _WEEK_RE.findall(body):
        if w not in source:
            missing.append(w)
    for m in _TOKEN_RE.finditer(body):
        w = m.group(0)
        if len(w) < 3 or not w[0].isupper():
            continue
        lw = w.lower()
        if lw in _GENERIC_CAPS or lw.rstrip("s") in _GENERIC_CAPS:
            continue
        if lw[:3] in _MONTHS and (lw in _MONTH_NAMES or len(lw) <= 4):
            continue  # a month is a date part; the date checks above judge it
        before = body[: m.start()].rstrip(" \t*_")
        opener = not before or before[-1] in _SENTENCE_OPENERS
        if opener and not w.isupper():
            continue
        if lw in src_tokens or lw[:6] in src_stems or lw.rstrip("s") in src_tokens:
            continue
        missing.append(w)
    return sorted(set(missing))


@dataclass
class FieldCheck:
    field: str
    score: float | None
    reason: str
    unsupported: list[str]
    ok: bool


def check_draft(fields: dict[str, str | list[str]], source: str) -> list[FieldCheck]:
    """Score every drafted field; `ok` is False when any check fails."""
    out: list[FieldCheck] = []
    for label in DRAFTED_FIELDS:
        value = fields.get(label, "")
        text = value if isinstance(value, str) else "\n".join(value)
        a = distill_fidelity.assess(text, source)
        unsupported = unsupported_specifics(text, source)
        ok = not a["low"] and not unsupported
        reason = a["reason"]
        if unsupported:
            reason = f"not in the material: {', '.join(unsupported)}"
        out.append(FieldCheck(label, a.get("score"), reason, unsupported, ok))
    return out


def budget_warnings(cp_md_text: str) -> list[str]:
    """Exec Summary budget warnings for the four drafted fields only."""
    return [w for w in lint_exec_summary(cp_md_text)
            if any(f"exec-summary {f}:" in w for f in DRAFTED_FIELDS)]


# ──────────────────────────────────────────────────────────────────────
#  One workstream
# ──────────────────────────────────────────────────────────────────────


@dataclass
class DraftResult:
    code: str
    reason: str  # why it was in scope
    outcome: str  # written | would-write | rejected | skipped | error
    detail: str = ""
    source_note: str = ""
    fields: dict = field(default_factory=dict)
    checks: list[FieldCheck] = field(default_factory=list)
    cp_md_path: str = ""

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "reason": self.reason,
            "outcome": self.outcome,
            "detail": self.detail,
            "source_note": self.source_note,
            "fields": self.fields,
            "checks": [c.__dict__ for c in self.checks],
            "cp_md_path": self.cp_md_path,
        }


def draft_one(
    project,
    *,
    tenant_root: Path,
    work_dir: Path,
    reason: str,
    today: date,
    current_week: str,
    llm: Callable[[str, str], str],
    commitments: list[dict] | tuple[dict, ...] = (),
    apply: bool,
) -> DraftResult:
    """Draft, check and (with `apply`) write one workstream's summary.

    Writes NOTHING unless every field passes the fidelity and specifics
    checks and the merged summary is inside the field budgets.
    """
    from cp_engine.exec_summary_merge import (
        ExecSummaryMergeError,
        merge_exec_summary_fields,
    )

    cp_md = work_dir / "cp.md"
    rel = str(cp_md.relative_to(tenant_root)) if cp_md.is_relative_to(tenant_root) else str(cp_md)
    res = DraftResult(code=project.code, reason=reason, outcome="skipped", cp_md_path=rel)

    sources = gather_sources(tenant_root, project, work_dir,
                             current_week=current_week, commitments=commitments)
    res.source_note = sources.note()
    if sources.is_empty():
        res.detail = f"no material in the last {SOURCE_WEEKS} sprint weeks"
        return res

    label = f"{project.code} — {project.name}"
    try:
        reply = llm(SYSTEM_PROMPT, build_prompt(label, sources, today=today))
        fields = parse_draft(reply)
    except DraftError as exc:
        res.outcome, res.detail = "error", str(exc)
        return res
    res.fields = fields

    source_text = sources.text()
    res.checks = check_draft(fields, source_text)
    failed = [c for c in res.checks if not c.ok]
    if failed:
        res.outcome = "rejected"
        res.detail = "; ".join(f"{c.field}: {c.reason}" for c in failed)
        return res

    text = cp_md.read_text(encoding="utf-8")
    try:
        merged, changed = merge_exec_summary_fields(text, fields, today=today)
    except ExecSummaryMergeError as exc:
        res.outcome, res.detail = "error", str(exc)
        return res
    if not changed:
        res.detail = "draft matches the current fields"
        return res
    over = budget_warnings(merged)
    if over:
        res.outcome, res.detail = "rejected", "; ".join(over)
        return res

    merged = mark_drafted(merged, sources.note())
    if apply:
        cp_md.write_text(merged, encoding="utf-8")
        res.outcome = "written"
    else:
        res.outcome = "would-write"
    res.detail = f"changed: {', '.join(changed)}"
    return res


# ──────────────────────────────────────────────────────────────────────
#  The run
# ──────────────────────────────────────────────────────────────────────


def draft_summaries(
    config,
    projects,
    *,
    today: date,
    current_week: str,
    llm: Callable[[str, str], str],
    codes: tuple[str, ...] = (),
    supabase_client=None,
    apply: bool,
) -> list[DraftResult]:
    """Draft every in-scope active workstream (or the named ones, still
    scope-checked). Returns one result per workstream considered."""
    from cp_engine.agenda import filter_active
    from cp_engine.prep_planning import _fetch_project_commitments
    from cp_engine.state import resolve_project_dir, select_codes

    active = tuple(filter_active(tuple(projects)))
    if codes:
        active = select_codes(active, codes)
    by_code = {p.code: p for p in projects}

    results: list[DraftResult] = []
    for p in sorted(active, key=lambda x: x.code):
        work_dir = resolve_project_dir(config.root, p, by_code)
        cp_md = work_dir / "cp.md"
        if not cp_md.is_file():
            continue
        reason = scope_reason(cp_md, today=today)
        if reason is None:
            if codes:
                results.append(DraftResult(p.code, "current", "skipped",
                                           "summary is current"))
            continue
        # strict: a failed commitments read must not become a draft whose
        # Next up / Blockers were written as if there were none (step 3).
        try:
            commitments = (
                _fetch_project_commitments(supabase_client, p, strict=True)
                if supabase_client else ()
            )
        except Exception as exc:  # noqa: BLE001 — one workstream never sinks the run
            results.append(DraftResult(
                p.code, reason, "error",
                f"commitments read failed, not drafting from partial sources: "
                f"{type(exc).__name__}: {exc}",
            ))
            continue
        try:
            results.append(draft_one(
                p, tenant_root=config.root, work_dir=work_dir, reason=reason,
                today=today, current_week=current_week, llm=llm,
                commitments=commitments, apply=apply,
            ))
        except Exception as exc:  # noqa: BLE001 — one workstream never sinks the run
            logger.exception("draft failed for %s", p.code)
            results.append(DraftResult(p.code, reason, "error", str(exc)))
    return results


# ──────────────────────────────────────────────────────────────────────
#  The scheduled run (`cxp draft-summaries` and the cron route share it)
# ──────────────────────────────────────────────────────────────────────

#: The scheduled run's window: Monday, before this tenant-local hour.
PLANNING_MORNING_BEFORE_HOUR = 10


def is_planning_morning(now) -> bool:
    """`--planning-morning-only`: Monday before 10:00 TENANT time. `now` is a
    tenant wall-clock datetime (naive, or aware in the tenant zone)."""
    return now.weekday() == 0 and now.hour < PLANNING_MORNING_BEFORE_HOUR


def run_drafts(config, *, now, codes: tuple[str, ...] = (), model: str | None = None,
               apply: bool, llm: Callable[[str, str], str] | None = None) -> list[DraftResult]:
    """One draft run over the tenant at `config.root` — the body of
    `cxp draft-summaries`, callable from the webhook's cron route."""
    from cp_engine.prep_planning import _make_supabase_client
    from cp_engine.sprints import current_sprint_week_iso
    from cp_engine.sync import _default_backend_factory

    backend = _default_backend_factory(config.sync.backend)
    projects = backend.read_projects(config)
    return draft_summaries(
        config,
        tuple(projects),
        today=now.date(),
        current_week=current_sprint_week_iso(now),
        llm=llm or anthropic_llm(model),
        codes=codes,
        supabase_client=_make_supabase_client(config),
        apply=apply,
    )


def all_attempts_errored(results: list[DraftResult]) -> bool:
    """The run failed (transport / credentials), not the check: drafting was
    attempted and every attempt errored. A rejection is the check working."""
    attempted = [r for r in results if r.outcome != "skipped"]
    return bool(attempted) and all(r.outcome == "error" for r in attempted)


__all__ = [
    "DEFAULT_MODEL", "DRAFTED_FIELDS", "DRAFTED_MARKER", "DraftError", "DraftResult",
    "Sources", "all_attempts_errored", "anthropic_llm", "build_prompt", "check_draft",
    "draft_one", "draft_summaries", "gather_sources", "is_planning_morning", "run_drafts", "hand_written_sprint_text", "is_drafted",
    "mark_drafted", "parse_draft", "scope_reason",
    "unsupported_specifics",
]
