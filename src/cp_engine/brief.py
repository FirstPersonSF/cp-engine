"""`cp brief` — the composed Mode-2 CONTEXT PACK (arch-review 2026-07-26 §3).

One deterministic markdown page that orients a session on a project without
loading the whole cp.md corpus: Facts + a TRIMMED Exec Summary + the standing
"Inputs & Briefing" spine element's live body + open commitments + the
last-session pointer. Target ~900 words on a heavy project (the un-distilled
Exec Summary fields are quoted verbatim, so a bloated Status blows the target
— that's the Exec-Summary field-budget lint's problem, not this composer's).

Composition is PURE: text/rows in, markdown out, no clock reads and no
network. The MC-2 fetchers (`fetch_briefing_body`, `fetch_open_commitments`)
are best-effort and return `(payload, note)` — a project without a spine or a
commitments store degrades to a one-line absence note per section, never a
crash. Reuses the engine's existing read paths throughout: the cp.md region
slicers (render), the exec-summary field splitter (exec_summary_lint), the
live-row discipline (`project_sources._one_live_per_element`), and the
commitments owner resolver + list (commitments).
"""

from __future__ import annotations

import re
from pathlib import Path

from cp_engine.exec_summary_lint import _split_fields
from cp_engine.render import (
    exec_summary_is_authored,
    slice_exec_summary_region,
)

# The standing working-brief element (see the standing-element contracts in
# the session protocol): est_item_id `_authored/inputs-briefing`, framed
# "Inputs & Briefing". Matched id-first (exact machine path), title second.
BRIEF_ELEMENT_ID_FRAGMENT = "inputs-briefing"
BRIEF_ELEMENT_TITLE_FRAGMENT = "inputs & briefing"

# Where-it-stands keeps at most this many bullets in the pack (§3: trim, the
# full field stays in cp.md).
WHERE_IT_STANDS_MAX_BULLETS = 5

# Scaffold placeholder (`_<...>_`) — an unauthored value, not content.
_PLACEHOLDER_RE = re.compile(r"_<[^>]+>_")

_LAST_SESSION_LINE_RE = re.compile(r"^\*\*Last session:\*\*.*$", re.MULTILINE)

_REGION_START = "<!-- cp-engine:start {name} -->"
_REGION_END = "<!-- cp-engine:end {name} -->"


# ──────────────────────────────────────────────────────────────────────
#  Pure section builders
# ──────────────────────────────────────────────────────────────────────


def _slice_region(text: str, name: str) -> str | None:
    """Inner text of an engine-managed region (markers excluded), or None.

    Same marker grammar as render's exec-summary slicer, generalized by
    region name (render only exports the exec-summary-specific pair).
    """
    start_marker = _REGION_START.format(name=name)
    end_marker = _REGION_END.format(name=name)
    start = text.find(start_marker)
    if start == -1:
        return None
    end = text.find(end_marker, start)
    if end == -1:
        return None
    return text[start + len(start_marker):end].strip("\n")


def facts_section(cp_md_text: str | None) -> str:
    """The cp.md `project-facts` engine region, verbatim (sans its heading)."""
    if cp_md_text is None:
        return "_No working dir / cp.md found for this code — no Facts._"
    region = _slice_region(cp_md_text, "project-facts")
    if region is None:
        return "_cp.md has no project-facts region (pre-v0.2 scaffold?)._"
    # Drop the region's own `## Facts` heading — the pack supplies its own.
    lines = [l for l in region.splitlines() if not l.startswith("## ")]
    body = "\n".join(lines).strip("\n")
    return body or "_project-facts region is empty._"


def _trim_bullets(body: str, max_bullets: int) -> tuple[str, int]:
    """Keep `body` up to its Nth top-level `- ` bullet (sub-bullets ride
    along with their parent). Returns (trimmed_body, dropped_count)."""
    kept: list[str] = []
    seen = 0
    dropped = 0
    for raw in body.splitlines():
        is_bullet = raw.startswith("- ")
        if is_bullet:
            seen += 1
        if is_bullet and seen > max_bullets:
            dropped += 1
            continue
        if seen > max_bullets:
            continue  # sub-lines of a dropped bullet ride along with it
        kept.append(raw)
    return "\n".join(kept).rstrip(), dropped


def exec_summary_section(cp_md_text: str | None) -> str:
    """The Exec Summary TRIMMED per §3: Objective/Status/Next up/Blockers
    verbatim, Where it stands capped at 5 bullets, Updates + the Last-session
    line dropped (last-session gets its own section)."""
    if cp_md_text is None:
        return "_No cp.md — no Exec Summary._"
    region = slice_exec_summary_region(cp_md_text)
    if region is None:
        return "_cp.md has no exec-summary region._"
    if not exec_summary_is_authored(region):
        return "_Exec Summary not yet authored (scaffold only)._"

    fields = _split_fields(region)
    out: list[str] = []
    for label in ("Objective", "Status"):
        body = (fields.get(label) or "").strip()
        if body and not _PLACEHOLDER_RE.search(body):
            out.append(f"**{label}:** {body}")

    where = (fields.get("Where it stands") or "").strip()
    if where and not _PLACEHOLDER_RE.search(where):
        trimmed, dropped = _trim_bullets(where, WHERE_IT_STANDS_MAX_BULLETS)
        out.append("**Where it stands:**\n" + trimmed)
        if dropped:
            out.append(
                f"_( {dropped} more Where-it-stands bullet(s) trimmed — "
                "see cp.md )_")

    for label in ("Next up", "Blockers"):
        body = (fields.get(label) or "").strip()
        if body and not _PLACEHOLDER_RE.search(body):
            out.append(f"**{label}:**\n{body}")

    if not out:
        return "_Exec Summary fields are all placeholders._"
    return "\n\n".join(out)


def briefing_section(body: str | None, note: str | None) -> str:
    """The standing Inputs & Briefing element's live body, or its absence note."""
    if body:
        return body.strip()
    return f"_{note or 'No Inputs & Briefing available.'}_"


def canon_section(members: list[dict] | None, note: str | None) -> str:
    """The project canon (spec v04 §2): current-truth elements pinned to the
    brief via active `canon_of` edges. One line per member."""
    if members is None:
        return f"_{note or 'Canon unavailable.'}_"
    if not members:
        return ("_No canon members yet — `promote_to_canon` (cp-hosted) pins "
                "the current-truth set to this brief._")
    lines = [
        f"- **{(m.get('framing') or m.get('est_item_id') or '').strip()}** "
        f"(`{m.get('est_item_id')}`)"
        for m in members
    ]
    return "\n".join(lines)


def commitments_section(rows: list[dict] | None, note: str | None) -> str:
    """Open commitments as one line each: description — owner, due date."""
    if rows is None:
        return f"_{note or 'Commitments store unavailable.'}_"
    if not rows:
        return "_No open commitments._"
    lines = []
    for r in rows:
        owner = r.get("owner_name") or r.get("owner_email") or "unassigned"
        due = r.get("due_date") or "undated"
        lines.append(f"- {(r.get('description') or '').strip()} — {owner}, "
                     f"due {due}")
    return "\n".join(lines)


def last_session_section(cp_md_text: str | None,
                         newest_capture: str | None) -> str:
    """The cp.md `**Last session:**` line + the newest sessions/ capture
    filename, whichever exist."""
    out: list[str] = []
    if cp_md_text:
        m = _LAST_SESSION_LINE_RE.search(cp_md_text)
        if m:
            out.append(m.group(0))
    if newest_capture:
        out.append(f"Newest capture: `sessions/{newest_capture}`")
    if not out:
        return "_No Last-session line and no sessions/ captures._"
    return "\n".join(out)


def compose_brief(
    code: str,
    cp_md_text: str | None,
    briefing_body: str | None,
    briefing_note: str | None,
    commitments: list[dict] | None,
    commitments_note: str | None,
    newest_capture: str | None,
    canon: list[dict] | None = None,
    canon_note: str | None = None,
) -> str:
    """Assemble the six-section context pack. Pure and deterministic —
    identical inputs compose byte-identical output. `canon`/`canon_note`
    default to the absence shape so pre-v04 callers compose unchanged."""
    sections = (
        ("Facts", facts_section(cp_md_text)),
        ("Exec Summary (trimmed)", exec_summary_section(cp_md_text)),
        ("Canon — current truth", canon_section(canon, canon_note)),
        ("Inputs & Briefing", briefing_section(briefing_body, briefing_note)),
        ("Open commitments", commitments_section(commitments,
                                                 commitments_note)),
        ("Last session", last_session_section(cp_md_text, newest_capture)),
    )
    parts = [f"# Brief — {code}"]
    for heading, body in sections:
        parts.append(f"## {heading}\n\n{body}")
    return "\n\n".join(parts) + "\n"


# ──────────────────────────────────────────────────────────────────────
#  Best-effort fetchers (MC-2 + filesystem)
# ──────────────────────────────────────────────────────────────────────

# Live-brief read shape — body + just enough bookkeeping for the one-live
# discipline (`_one_live_per_element` keys/ranks on these).
_BRIEF_COLUMNS = (
    "est_item_id, framing, body, status, archived, scope, project_id, "
    "version_label, version_date"
)


def fetch_briefing_body(
    client, code: str, alt_code: str | None = None
) -> tuple[str | None, str | None]:
    """The standing Inputs & Briefing element's live body: `(body, note)`.

    Reads `spine_substance` by project_code — the same slug-native resolution
    `cp spine-lint` uses (no number-parsing, so it works for engagements,
    initiatives, and standalone-repo codes alike). `spine_substance` keys on
    the DIR-SLUG code (`ibx-5192-platform-sales-readiness-summit`), so a
    caller holding the short form passes the working dir's name as
    `alt_code` and the read falls back to it when `code` matches nothing.
    Exactly one of the pair is non-None. Callers still wrap this in a try,
    per the every-section-degrades contract.
    """
    from cp_engine import mc2_db
    from cp_engine.project_sources import _one_live_per_element

    if client is None:
        return None, "MC-2 unreachable — Inputs & Briefing not read."

    def _live_rows(project_code: str) -> list[dict]:
        rows = (
            client.table(mc2_db.Tables.SPINE_SUBSTANCE)
            .select(_BRIEF_COLUMNS)
            .eq("project_code", project_code)
            .eq("status", "live")
            .execute()
            .data
        ) or []
        return _one_live_per_element(
            [r for r in rows if not r.get("archived")]
        )

    rows = _live_rows(code)
    if not rows and alt_code and alt_code != code:
        rows = _live_rows(alt_code)
    if not rows:
        return None, f"No live spine for '{code}'."
    hit = next(
        (r for r in rows
         if BRIEF_ELEMENT_ID_FRAGMENT in (r.get("est_item_id") or "")),
        None,
    ) or next(
        (r for r in rows
         if BRIEF_ELEMENT_TITLE_FRAGMENT in (r.get("framing") or "").lower()),
        None,
    )
    if hit is None:
        return None, "Spine has no standing Inputs & Briefing element."
    body = (hit.get("body") or "").strip()
    if not body:
        return None, "Inputs & Briefing element exists but its body is empty."
    return body, None


def fetch_canon_members(
    client, code: str, alt_code: str | None = None
) -> tuple[list[dict] | None, str | None]:
    """The project's canon members: `([{est_item_id, framing}, …], note)`.

    Reads active `canon_of` edges by project_code (same slug-native + dir-slug
    fallback as `fetch_briefing_body`), then titles the members from their
    live `spine_substance` rows. A project with no edges returns `([], None)`
    — the section renders its promote_to_canon hint, not an absence note.
    """
    from cp_engine import mc2_db

    if client is None:
        return None, "MC-2 unreachable — canon not read."

    def _edges(project_code: str) -> list[dict]:
        return (
            client.table(mc2_db.Tables.SPINE_RELATIONS)
            .select("from_item_id, to_item_id")
            .eq("project_code", project_code)
            .eq("kind", "canon_of")
            .eq("status", "active")
            .execute()
            .data
        ) or []

    resolved = code
    edges = _edges(code)
    if not edges and alt_code and alt_code != code:
        resolved = alt_code
        edges = _edges(alt_code)
    member_ids = [e["from_item_id"] for e in edges]
    if not member_ids:
        return [], None

    titles: dict[str, str] = {}
    try:
        for r in (
            client.table(mc2_db.Tables.SPINE_SUBSTANCE)
            .select("est_item_id, framing")
            .eq("project_code", resolved)
            .eq("status", "live")
            .in_("est_item_id", member_ids)
            .execute()
            .data
            or []
        ):
            titles[r["est_item_id"]] = r.get("framing")
    except Exception:  # noqa: BLE001 — titles are a nicety, ids suffice
        pass
    return (
        [{"est_item_id": m, "framing": titles.get(m)} for m in member_ids],
        None,
    )


def fetch_open_commitments(client, code) -> tuple[list[dict] | None, str | None]:
    """Open commitments for the code's owner: `(rows, note)`.

    Standalone repos can't own commitments — their codes resolve to no
    engagement or initiative, which lands on the absence note (correct, not
    an error). Exactly one of the pair is non-None ([] means a real,
    empty answer).
    """
    from cp_engine.commitments import (
        list_commitments,
        resolve_commitment_owner,
    )

    if client is None:
        return None, "MC-2 unreachable — commitments not read."
    owner = resolve_commitment_owner(client, code)
    if owner is None:
        return None, (f"'{code}' owns no commitments store (engagements and "
                      "initiatives only).")
    return list_commitments(client, owner, status="open"), None


def newest_session_capture(working_dir: Path | None) -> str | None:
    """Filename of the newest capture under <working_dir>/sessions/, or None.

    Newest = last in lexicographic order — the same rule as
    `capture_session.derive_last_session_line` (the `YYYY-MM-DD-HHMM-` prefix
    sorts chronologically).
    """
    if working_dir is None:
        return None
    sessions_dir = working_dir / "sessions"
    if not sessions_dir.is_dir():
        return None
    files = sorted(p.name for p in sessions_dir.glob("*.md") if p.is_file())
    return files[-1] if files else None
