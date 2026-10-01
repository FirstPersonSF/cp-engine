"""Asks live in MC-2 (architecture plan step 4a, decided 2026-10-01).

An ask — who owes what, to whom, by when — has ONE owner: a row in MC-2's
``public.commitments`` table. The sprint file no longer stores asks; it SHOWS
them. Its ``### Open asks`` subsection carries an engine-managed region,
``<!-- cp-engine:start open-asks -->``, rendered at sync from that
workstream's open commitments, and the ``carry-forward`` region no longer
carries asks at all.

WHY. Before this, the same ask lived in two stores that never reconciled.
Closures happened in MC-2 (hosted ``resolve_commitment``, Slack, ClickUp)
and never flowed back: on 2026-10-01, 388 asks read *open* in a sprint file
while their MC-2 row was done, dropped or routed, and 704 open sprint asks
had no MC-2 row at all. Every surface that counted "open asks" was counting
asks that were already closed.

THE ONE HASH RECIPE (:func:`ask_hash`). Identity across writers — engine
ingest, the meeting webhook, the hosted server, a hand-typed bullet — is
``sha256("<co>-<number>|ask|<normalized text>")[:8]``. Two things made the
old recipe drift: meeting-ingest hashed the SHORT code (``slt-5196``) while
sprint bullets hashed the FULL one (``slt-5196-brand-campaign-26``), and the
verb differed per writer (``record-ask`` / ``set-milestone`` /
``set-client-ask-task``). Only 114 of 431 real pairs shared a hash. Now the
key is always the SHORT form (:func:`hash_key`: any spelling of a code →
``<co>-<number>``, via ``codes.parse_code``) — rename-stable, because a
workstream's company and job number never change while its name slug does
(decided 2026-10-01) — and the text is normalized
(:func:`normalize_ask_text`) so whitespace, case, a trailing period and the
display-only annotations (``[owner unresolved: …]``) cannot split one ask
into two.

HAND-TYPED ASKS ARE IMPORTED, NEVER LOST, NEVER DUPLICATED. A person (or an
ingest plan) can still type ``- [open · date · who] text`` under
``### Open asks``. At sync :func:`sync_sprint_asks` matches it against ALL of
the workstream's commitments (every status — a dropped ask must not come back
because someone retyped it), imports it when nothing matches, and moves it
into the region. Closing an ask means resolving its commitment — hosted
``resolve_commitment``, the MC-2 UI, ``close-ask`` — and the next render drops
it everywhere.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

#: The engine-managed region inside a sprint file's ``### Open asks``.
ASKS_REGION = "open-asks"

#: ``commitments.source_kind`` for rows imported from a sprint-file bullet.
#: The column has no CHECK constraint (mig 097); existing values are
#: meeting_ingest / session / manual / clickup_migration.
SPRINT_IMPORT = "sprint_import"

#: Asks first raised more than this many weeks before today import as
#: ``expired`` rather than ``open`` — the same 6-week cap carry-forward used
#: (``sprints.CARRY_FORWARD_MAX_AGE_WEEKS``), decided 2026-10-01.
IMPORT_OPEN_MAX_AGE_DAYS = 42

#: Explicit columns — never SELECT * (tenant rule).
COMMITMENT_COLUMNS = (
    "id, description, owner_email, owner_name, direction, due_date, "
    "date_status, status, source_kind, source_meeting_id, cp_hash, "
    "project_id, created_at, updated_at"
)

OPEN = "open"
CLOSED_STATUSES = frozenset({"done", "dropped", "routed", "expired"})

# ──────────────────────────────────────────────────────────────────────
#  The one hash recipe
# ──────────────────────────────────────────────────────────────────────

# Display-only annotations writers append to a description (webhook #114,
# milestone confidence, cross-routing). They never count toward identity.
_ANNOTATION_RE = re.compile(
    r"\s*\[(?:owner unresolved|off-project\?|routed from|confidence|"
    r"cross-project\?|cross-routed from|attribution unverified)[^\]]*\]",
    re.IGNORECASE,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_QUOTES = str.maketrans({
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", " ": " ",
})


def normalize_ask_text(text: str | None) -> str:
    """The identity-bearing form of an ask's text.

    Strips HTML comments (``cp:hash``, snooze and closed-by markers) and
    display annotations, folds typographic quotes and dashes, collapses
    whitespace, casefolds, and drops trailing punctuation. Two bullets that
    differ only in those ways are the same ask."""
    t = _COMMENT_RE.sub(" ", text or "")
    t = _ANNOTATION_RE.sub(" ", t)
    t = unicodedata.normalize("NFKC", t).translate(_QUOTES)
    t = " ".join(t.split()).casefold()
    return t.rstrip(" .;:,")


def hash_key(code: str | None) -> str:
    """The rename-stable part of an ask's identity: ``<co>-<number>``.

    Any spelling resolves — ``slt-5196-brand-campaign-26``, ``slt-5196``,
    ``SLT 5196 Brand Campaign 26``, ``SLT-5196`` all give ``slt-5196`` — so
    a renamed workstream keeps every ask's hash. A string that is not a
    workstream code is lower-cased as given."""
    from cp_engine.codes import parse_code

    parsed = parse_code(code)
    return parsed.short if parsed else (code or "").strip().lower()


def ask_hash(code: str, text: str, *, occurrence: int = 0) -> str:
    """THE cp_hash recipe for an ask / commitment — every writer calls this.

    ``code`` is any spelling of the workstream code; it is reduced to the
    rename-stable short form by :func:`hash_key`. A spelling that does not
    parse (no job number) should be resolved first with
    :func:`resolve_canonical_code`. ``occurrence`` > 0 salts the hash for a
    deliberate repeat of an ask whose first life is closed (see the hosted
    ``create_commitment``): the unsalted hash keeps pointing at the closed
    row, so a re-ingest of the original meeting still cannot resurrect it.
    """
    key = f"{hash_key(code)}|ask|{normalize_ask_text(text)}"
    if occurrence:
        key += f"|#{occurrence}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def canonical_code(full_job_name: str | None) -> str:
    """``slug(projects.full_job_name)`` — the canonical workstream code."""
    from cp_engine.state import slug_full_job_name

    return slug_full_job_name(full_job_name)


def resolve_canonical_code(
    code: str,
    *,
    client: Any = None,
    project_id: str | None = None,
) -> str:
    """Any spelling of a workstream code → its canonical full code.

    Only needed for a spelling with no job number in it (a stale spine
    ``project_code``): :func:`ask_hash` reduces anything that parses to
    ``<co>-<number>`` itself. With a client, MC-2 decides
    (``projects.full_job_name`` by id, else by job number); without one —
    or when MC-2 has no row — the code is returned lower-cased as given.
    """
    given = (code or "").strip().lower()
    if client is None:
        return given
    try:
        from cp_engine.mc2_db import Tables

        q = client.table(Tables.PROJECTS).select("full_job_name")
        if project_id:
            q = q.eq("id", project_id)
        else:
            from cp_engine.codes import code_number

            number = code_number(given)
            if number is None:
                return given
            q = q.eq("number", number)
        rows = q.limit(1).execute().data or []
        slug = canonical_code(rows[0].get("full_job_name")) if rows else ""
        return slug or given
    except Exception as exc:  # noqa: BLE001 — resolution must not block a write
        log.warning("asks: canonical code lookup failed for %r: %s", code, exc)
        return given


# ──────────────────────────────────────────────────────────────────────
#  Reading MC-2
# ──────────────────────────────────────────────────────────────────────


def fetch_project_commitments(client: Any, project_id: str) -> list[dict]:
    """Every commitment of one workstream, ALL statuses (the matcher needs
    the closed ones so a retyped closed ask is not re-imported)."""
    from cp_engine.mc2_db import Tables

    rows: list[dict] = []
    start = 0
    while True:
        page = (
            client.table(Tables.COMMITMENTS)
            .select(COMMITMENT_COLUMNS)
            .eq("project_id", project_id)
            .range(start, start + 999)
            .execute()
            .data
            or []
        )
        rows.extend(page)
        if len(page) < 1000:
            return rows
        start += 1000


# ──────────────────────────────────────────────────────────────────────
#  Matching a sprint bullet to a commitment
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AskMatch:
    row: dict
    method: str  # "hash" | "bullet-hash" | "text" | "tokens"


def _tokens(text: str) -> frozenset[str]:
    from cp_engine.text_similarity import _STOP, _WORD_RE

    return frozenset(
        w for w in _WORD_RE.findall(normalize_ask_text(text)) if w not in _STOP
    )


def match_ask(
    text: str,
    *,
    code: str,
    rows: Iterable[dict],
    bullet_hash: str | None = None,
) -> AskMatch | None:
    """The commitment ``text`` already is, or None.

    Tiers, first hit wins: the new-recipe hash; the hash the bullet already
    carries (``bullet_hash``, which may be a legacy recipe the row shares);
    the row's own description under the new recipe (rows not yet re-keyed);
    equal normalized text; equal content-word sets (a reordering, or a
    stop-word edit). Within a tier an OPEN row beats a closed one — a
    deliberate repeat (salted hash, see :func:`ask_hash`) is the live one —
    then the newest row.

    Match is scoped to ``rows``, which the caller limits to ONE workstream:
    text equality across workstreams is a coincidence, not identity.
    """
    rows = list(rows)
    h = ask_hash(code, text)
    norm = normalize_ask_text(text)
    toks = _tokens(text)

    def best(cands: list[dict]) -> dict | None:
        if not cands:
            return None
        return sorted(
            cands,
            key=lambda r: (r.get("status") == OPEN, r.get("created_at") or ""),
            reverse=True,
        )[0]

    tiers: list[tuple[str, list[dict]]] = [
        ("hash", [r for r in rows if r.get("cp_hash") == h]),
        ("bullet-hash", [r for r in rows if bullet_hash and r.get("cp_hash") == bullet_hash]),
        ("hash", [r for r in rows if ask_hash(code, r.get("description") or "") == h]),
        ("text", [r for r in rows if normalize_ask_text(r.get("description")) == norm]),
    ]
    if len(toks) >= 3:
        tiers.append(
            ("tokens", [r for r in rows if _tokens(r.get("description") or "") == toks])
        )
    for method, cands in tiers:
        row = best(cands)
        if row is not None:
            return AskMatch(row=row, method=method)
    return None


# ──────────────────────────────────────────────────────────────────────
#  Building an import row from a sprint bullet
# ──────────────────────────────────────────────────────────────────────

_WHO_SPLIT_RE = re.compile(r"\s*(?:/|,|&|\+|\band\b)\s*", re.IGNORECASE)
_PAREN_RE = re.compile(r"\s*\([^)]*\)")
_INTERNAL_TAG_RE = re.compile(r"\(\s*(internal|fp|first person|1p)\s*\)", re.IGNORECASE)

# Direction from wording, conservative on purpose (decided 2026-10-01):
# infer only where unambiguous, else leave the column default (internal).
_TO_US_RE = re.compile(
    r"\b(?:send|share|provide|give|forward|return|get)s?\b[^.]*?\b(?:to|with)\s+"
    r"(?:us|fp|first ?person|1p|drew|tony|marcello|the team)\b"
    r"|\b(?:send|share|provide|give|forward)s?\s+(?:us|fp|first ?person|1p)\b",
    re.IGNORECASE,
)
_TO_THEM_VERB_RE = re.compile(
    r"^(?:send|deliver|share|provide|email|submit|present)\b", re.IGNORECASE
)


def first_owner_name(who: str | None) -> str:
    """The first person in a free-text ``who`` ("Leah Ward / Morgan Wright
    (Salesloft)" → "Leah Ward")."""
    raw = (who or "").strip()
    if not raw:
        return ""
    first = _WHO_SPLIT_RE.split(raw, maxsplit=1)[0]
    return _PAREN_RE.sub("", first).strip()


def infer_direction(
    text: str,
    *,
    who: str | None,
    owner_is_internal: bool,
    company_name: str | None = None,
) -> str | None:
    """``them_to_us`` / ``us_to_them`` when the WORDING says so, else None
    (the caller keeps the column default).

    them_to_us: an external owner, and the text names us as the recipient
    ("send the deck to Drew", "share with FP").
    us_to_them: an internal owner, the text opens with a hand-off verb, and
    names the client company or "the client" as the recipient.
    """
    t = text or ""
    tagged_internal = bool(_INTERNAL_TAG_RE.search(who or ""))
    if who and not owner_is_internal and not tagged_internal and _TO_US_RE.search(t):
        return "them_to_us"
    if (owner_is_internal or tagged_internal) and _TO_THEM_VERB_RE.search(t.strip()):
        names = ["the client", "client"]
        if company_name:
            names.append(company_name)
        if any(re.search(rf"\bto\s+(?:the\s+)?{re.escape(n)}\b", t, re.IGNORECASE)
               for n in names):
            return "us_to_them"
    return None


def asked_created_at(asked_date: str | None) -> str | None:
    """A ``created_at`` for an imported ask: its first-raised day, at noon
    UTC (so it renders as the same calendar day in every US timezone)."""
    try:
        d = date.fromisoformat((asked_date or "").strip()[:10])
    except ValueError:
        return None
    return datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc).isoformat()


#: Workstream statuses whose asks import as ``expired`` whatever their age:
#: nobody will chase an ask on a closed job. ``Holding`` is paused, not over,
#: so its recent asks import ``open`` (decided 2026-10-01).
EXPIRED_WORKSTREAM_STATUSES = frozenset({"Closed", "Archived"})


def import_status(
    asked_date: str | None, today: date, workstream_status: str | None = None
) -> str:
    """``open`` when raised within the 6-week cap, else ``expired``; always
    ``expired`` on a Closed/Archived workstream. An undated ask is treated
    as current (there is nothing to age it by)."""
    if workstream_status in EXPIRED_WORKSTREAM_STATUSES:
        return "expired"
    try:
        d = date.fromisoformat((asked_date or "").strip()[:10])
    except ValueError:
        return OPEN
    return OPEN if (today - d).days <= IMPORT_OPEN_MAX_AGE_DAYS else "expired"


#: Word overlap at which an unmatched ask probably restates an existing
#: commitment in other words. Below an exact match, so it is never merged
#: automatically — the import is skipped and the pair reported for a human
#: (decided 2026-10-01: the 7 such asks found that day were not imported).
LIKELY_DUPLICATE_JACCARD = 0.6


def likely_duplicate(text: str, rows: Iterable[dict]) -> tuple[dict, float] | None:
    """The existing commitment ``text`` most probably restates (any status,
    one workstream's ``rows``), with its overlap, or None below the bar."""
    from cp_engine.text_similarity import jaccard

    best: tuple[dict, float] | None = None
    for r in rows:
        score = jaccard(text or "", r.get("description") or "")
        if score >= LIKELY_DUPLICATE_JACCARD and (best is None or score > best[1]):
            best = (r, score)
    return best


def build_import_row(
    *,
    text: str,
    code: str,
    project_id: str,
    who: str | None,
    by: str | None,
    asked_date: str | None,
    today: date,
    client: Any = None,
    company_name: str | None = None,
    status: str | None = None,
    workstream_status: str | None = None,
    warnings: list | None = None,
) -> dict:
    """One ``commitments`` row for a sprint-file ask that matched nothing.

    Owner: the first name in ``who``, canonicalized against the entities
    roster when a client is given (``resolve_owner_identity``); a client-side
    person keeps the name with no email. Due date: ``by YYYY-MM-DD``.
    ``created_at`` is the first-raised day, so age stays legible.
    """
    from cp_engine.commitments import _valid_due_date, resolve_owner_identity

    name = first_owner_name(who)
    owner_name, owner_email = (name or None), None
    owner_is_internal = False
    if client is not None and name:
        owner_name, owner_email = resolve_owner_identity(client, name, None, warnings)
        owner_is_internal = owner_email is not None
    direction = infer_direction(
        text, who=who, owner_is_internal=owner_is_internal,
        company_name=company_name,
    )
    clean = " ".join(_COMMENT_RE.sub(" ", text or "").split())
    row = {
        "description": clean,
        "owner_email": owner_email,
        "owner_name": owner_name,
        "due_date": _valid_due_date(by),
        "date_status": "proposed",
        "status": status or import_status(asked_date, today, workstream_status),
        "source_kind": SPRINT_IMPORT,
        "cp_hash": ask_hash(code, clean),
        "project_id": project_id,
    }
    if direction:
        row["direction"] = direction
    created = asked_created_at(asked_date)
    if created:
        row["created_at"] = created
    return row


# ──────────────────────────────────────────────────────────────────────
#  Rendering the region
# ──────────────────────────────────────────────────────────────────────

_SNOOZE_MARKER_RE = re.compile(r"<!--\s*cp:snoozed-until=\d{4}-\d{2}-\d{2}\s*-->")
_HASH_IN_LINE_RE = re.compile(r"cp:hash=([0-9a-f]{8})")
EMPTY_REGION_LINE = "- _No open asks._"
UNRENDERED_REGION_LINE = "- _Not yet rendered from MC-2._"


def _local_day(created_at: str | None) -> str:
    if not created_at:
        return ""
    try:
        from cp_engine.clock import local_date

        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return local_date(dt).isoformat()
    except ValueError:
        return created_at[:10]


def row_hash(row: dict, code: str) -> str:
    """The hash a rendered bullet carries: the row's own ``cp_hash`` (so
    close/snooze buttons address the row), or the recipe over its text for
    the few rows written without one."""
    return row.get("cp_hash") or ask_hash(code, row.get("description") or "")


def render_ask_line(row: dict, *, code: str, snooze_marker: str = "") -> str:
    who = (row.get("owner_name") or row.get("owner_email") or "unassigned").strip()
    who = who.replace("]", ")").replace(" · ", " - ")
    parts = ["open", _local_day(row.get("created_at")) or "—", who]
    if row.get("due_date"):
        parts.append(f"by {str(row['due_date'])[:10]}")
    text = " ".join((row.get("description") or "").split())
    snooze = f" {snooze_marker}" if snooze_marker else ""
    return f"- [{' · '.join(parts)}] {text}{snooze} <!-- cp:hash={row_hash(row, code)} -->"


def render_asks_region(
    rows: Iterable[dict], *, code: str, snoozes: dict[str, str] | None = None
) -> str:
    """The region body: one bullet per OPEN commitment, due date first
    (undated last), then oldest first. ``snoozes`` maps hash → the snooze
    marker found on the item's previous rendering, so a snooze survives the
    re-render (MC-2 has no snooze column; the marker is display state)."""
    snoozes = snoozes or {}
    live = [r for r in rows if r.get("status") == OPEN]
    live.sort(key=lambda r: (
        r.get("due_date") is None, str(r.get("due_date") or ""),
        r.get("created_at") or "", r.get("id") or "",
    ))
    if not live:
        return EMPTY_REGION_LINE
    return "\n".join(
        render_ask_line(r, code=code, snooze_marker=snoozes.get(row_hash(r, code), ""))
        for r in live
    )


# ──────────────────────────────────────────────────────────────────────
#  Locating `### Open asks` and its region in a sprint file
# ──────────────────────────────────────────────────────────────────────

_COMM_HEADING_RE = re.compile(r"^## (?:Client|Team) communication\b.*$", re.MULTILINE)
_OPEN_ASKS_HEADING_RE = re.compile(r"^### Open asks[ \t]*$", re.MULTILINE)
_NEXT_HEADING_RE = re.compile(r"^#{2,3} ", re.MULTILINE)
_REGION_START = f"<!-- cp-engine:start {ASKS_REGION} -->"
_REGION_END = f"<!-- cp-engine:end {ASKS_REGION} -->"


def _open_asks_span(body: str) -> tuple[int, int] | None:
    """(start, end) of the ``### Open asks`` subsection body — after its
    heading line, up to the next ``##``/``###`` heading — inside the
    communication section, or None."""
    comm = _COMM_HEADING_RE.search(body)
    if not comm:
        return None
    m = _OPEN_ASKS_HEADING_RE.search(body, comm.end())
    if not m:
        return None
    nxt = _NEXT_HEADING_RE.search(body, m.end())
    return m.end(), (nxt.start() if nxt else len(body))


def ensure_asks_region(body: str) -> str:
    """Seed the ``open-asks`` markers right under ``### Open asks`` in a file
    scaffolded before they existed. No-op when present or when the file has
    no such subsection."""
    if _REGION_START in body:
        return body
    span = _open_asks_span(body)
    if span is None:
        return body
    start, _ = span
    block = f"\n{_REGION_START}\n{UNRENDERED_REGION_LINE}\n{_REGION_END}"
    return body[:start] + block + body[start:]


@dataclass
class HandBullet:
    """A bullet typed in ``### Open asks`` outside the region."""

    lines: list[str]  # the bullet line plus its indented continuation
    status: str
    asked_date: str
    who: str | None
    by: str | None
    text: str  # joined, markers included
    hash: str | None

    @property
    def raw(self) -> str:
        return "\n".join(self.lines)


def _parse_bullet(lines: list[str]) -> HandBullet | None:
    from cp_engine.sprints import _ask_who_and_by, _join_bullet, parse_bracketed_bullet

    parsed = parse_bracketed_bullet(lines[0])
    if not parsed:
        return None
    parts, text = parsed
    status = (parts[0] if parts else "open").strip().lower()
    asked = parts[1] if len(parts) > 1 else ""
    who, by = _ask_who_and_by(parts[2:])
    full = _join_bullet(text, "\n".join(ln.strip() for ln in lines[1:]))
    m = _HASH_IN_LINE_RE.search(" ".join(lines))
    return HandBullet(lines=lines, status=status, asked_date=asked, who=who,
                      by=by, text=full, hash=m.group(1) if m else None)


def _group_bullets(text: str) -> list[list[str]]:
    groups: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith("- ") or line.startswith("-\t"):
            groups.append([line])
        elif line.startswith("  ") and groups and line.strip():
            groups[-1].append(line)
    return groups


def split_open_asks(body: str) -> tuple[str, list[HandBullet], list[HandBullet]]:
    """``(region_inner, region_bullets, hand_bullets)`` for a sprint file.

    ``hand_bullets`` are the bracketed bullets under ``### Open asks`` that
    sit OUTSIDE the region — typed by a person or appended by an ingest
    plan, waiting to be imported."""
    span = _open_asks_span(body)
    if span is None:
        return "", [], []
    sub = body[span[0]:span[1]]
    region_inner = ""
    outside = sub
    if _REGION_START in sub and _REGION_END in sub:
        a = sub.index(_REGION_START)
        b = sub.index(_REGION_END)
        region_inner = sub[a + len(_REGION_START):b]
        outside = sub[:a] + sub[b + len(_REGION_END):]
    region = [hb for g in _group_bullets(region_inner) if (hb := _parse_bullet(g))]
    hand = [hb for g in _group_bullets(outside) if (hb := _parse_bullet(g))]
    return region_inner, region, hand


def remove_hand_bullets(body: str, bullets: list[HandBullet]) -> str:
    """Drop ``bullets`` from ``### Open asks`` (outside the region only)."""
    if not bullets:
        return body
    span = _open_asks_span(body)
    if span is None:
        return body
    sub = body[span[0]:span[1]]
    if _REGION_START in sub and _REGION_END in sub:
        a = sub.index(_REGION_START)
        b = sub.index(_REGION_END) + len(_REGION_END)
        head, region, tail = sub[:a], sub[a:b], sub[b:]
    else:
        head, region, tail = sub, "", ""
    for hb in bullets:
        needle = hb.raw + "\n"
        if needle in tail:
            tail = tail.replace(needle, "", 1)
        elif hb.raw in tail:
            tail = tail.replace(hb.raw, "", 1)
        elif needle in head:
            head = head.replace(needle, "", 1)
        elif hb.raw in head:
            head = head.replace(hb.raw, "", 1)
    return body[:span[0]] + head + region + tail + body[span[1]:]


# ──────────────────────────────────────────────────────────────────────
#  The sync pass
# ──────────────────────────────────────────────────────────────────────

_CLOSE_WORDS = {
    "closed": "done", "done": "done", "answered": "done", "resolved": "done",
    "dropped": "dropped", "cancelled": "dropped", "canceled": "dropped",
}


@dataclass
class AsksSyncResult:
    code: str
    imported: list[str] = field(default_factory=list)     # descriptions
    matched: list[str] = field(default_factory=list)      # hand bullets already in MC-2
    resolved: list[str] = field(default_factory=list)     # commitments closed by a hand edit
    rendered: int = 0
    changed: bool = False
    errors: list[str] = field(default_factory=list)


def _insert(client: Any, row: dict) -> dict:
    from cp_engine.mc2_db import Tables

    data = client.table(Tables.COMMITMENTS).insert(row).execute().data or []
    return {**row, **(data[0] if data else {})}


def _close(client: Any, row_id: str, outcome: str) -> None:
    from cp_engine.commitments import close_commitment

    close_commitment(client, row_id, outcome)


def sync_sprint_asks(
    client: Any,
    *,
    sprint_path: Path,
    code: str,
    project_id: str,
    today: date,
    earlier_files: Iterable[Path] = (),
    company_name: str | None = None,
    writer: str = "cxp sync",
) -> AsksSyncResult:
    """Import hand-typed asks into MC-2, then render ``open-asks`` from it.

    1. A region bullet whose status a person flipped (``[done ·``) closes
       its commitment — the region is a view, but the edit is honoured
       rather than discarded.
    2. Each hand bullet in THIS file's ``### Open asks``: an open one that
       matches nothing is imported; a matched one defers to MC-2's status
       (MC-2 wins); a closed one (``[closed ·``/``[done ·``/``[answered ·``)
       closes its open commitment. Every handled bullet is removed — it now
       renders inside the region, once.
    3. ``earlier_files`` (prior weeks in the carry window): an open hand
       bullet that matches nothing is imported. Those files are history and
       are not edited; a re-run matches the import and does nothing.
    4. Render the region from the workstream's open commitments.

    A failed insert keeps its bullet where it was (never lost) and is
    reported in ``errors``. Writes the file only when it changed.
    """
    from cp_engine.render import splice_managed_region

    result = AsksSyncResult(code=code)
    rows = fetch_project_commitments(client, project_id)
    original = sprint_path.read_text(encoding="utf-8")
    body = ensure_asks_region(original)
    _, region_bullets, hand_bullets = split_open_asks(body)
    if _REGION_START not in body:
        return result  # no `### Open asks` subsection to render into
    warnings: list[str] = []

    def _match(hb: HandBullet) -> AskMatch | None:
        return match_ask(hb.text, code=code, rows=rows, bullet_hash=hb.hash)

    # Snooze markers on the previous rendering survive the re-render.
    snoozes: dict[str, str] = {}
    for hb in region_bullets + hand_bullets:
        s = _SNOOZE_MARKER_RE.search(hb.text)
        if s and hb.hash:
            snoozes[hb.hash] = s.group(0)

    # 1 — status flips inside the region.
    for hb in region_bullets:
        outcome = _CLOSE_WORDS.get(hb.status)
        if not outcome:
            continue
        m = _match(hb)
        if m and m.row.get("status") == OPEN:
            try:
                _close(client, m.row["id"], outcome)
                m.row["status"] = outcome
                result.resolved.append(m.row.get("description") or "")
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"close {m.row.get('id')}: {exc}")

    # 2 — this week's hand bullets.
    handled: list[HandBullet] = []
    for hb in hand_bullets:
        outcome = _CLOSE_WORDS.get(hb.status)
        m = _match(hb)
        if outcome:
            if m and m.row.get("status") == OPEN:
                try:
                    _close(client, m.row["id"], outcome)
                    m.row["status"] = outcome
                    result.resolved.append(m.row.get("description") or "")
                    handled.append(hb)
                except Exception as exc:  # noqa: BLE001
                    result.errors.append(f"close {m.row.get('id')}: {exc}")
            elif m:
                handled.append(hb)  # already closed in MC-2: nothing to keep
            continue  # an unmatched closed bullet is history; leave it
        if hb.status != OPEN:
            continue  # an unknown status word: not ours to interpret
        if m:
            if hb.hash and hb.hash in snoozes:
                snoozes[row_hash(m.row, code)] = snoozes[hb.hash]
            result.matched.append(hb.text)
            handled.append(hb)
            continue
        row = build_import_row(
            text=hb.text, code=code, project_id=project_id, who=hb.who,
            by=hb.by, asked_date=hb.asked_date or today.isoformat(),
            today=today, client=client, company_name=company_name,
            status=OPEN, warnings=warnings,
        )
        try:
            stored = _insert(client, row)
        except Exception as exc:  # noqa: BLE001 — keep the bullet, say why
            result.errors.append(f"import {row['description'][:60]!r}: {exc}")
            continue
        rows.append(stored)
        if hb.hash and hb.hash in snoozes:
            snoozes[row_hash(stored, code)] = snoozes[hb.hash]
        result.imported.append(row["description"])
        handled.append(hb)

    # 3 — open hand bullets in the earlier weeks of the carry window. The
    # NEWEST statement of an ask decides (the carry walk's rule): one closed
    # in this week or a later earlier-week is not imported from an older
    # open copy. `earlier_files` arrive newest first.
    def _identity(hb: HandBullet) -> str:
        return hb.hash or normalize_ask_text(hb.text)

    stated: set[str] = {_identity(hb) for hb in region_bullets + hand_bullets}
    for path in earlier_files:
        try:
            other = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for hb in split_open_asks(other)[2]:
            key = _identity(hb)
            if key in stated:
                continue
            stated.add(key)
            if hb.status != OPEN or _match(hb):
                continue
            row = build_import_row(
                text=hb.text, code=code, project_id=project_id, who=hb.who,
                by=hb.by, asked_date=hb.asked_date, today=today,
                client=client, company_name=company_name, warnings=warnings,
            )
            try:
                rows.append(_insert(client, row))
                result.imported.append(row["description"])
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"import {row['description'][:60]!r}: {exc}")

    body = remove_hand_bullets(body, handled)
    new_inner = render_asks_region(rows, code=code, snoozes=snoozes)
    result.rendered = sum(1 for r in rows if r.get("status") == OPEN)
    body = splice_managed_region(
        body, ASKS_REGION, new_inner, source=sprint_path, writer=writer,
        guard_hint=("Asks live in MC-2: close one with resolve_commitment "
                    "(or close-ask); type a new one under ### Open asks, "
                    "outside the markers."),
    )
    for w in warnings:
        log.warning("asks %s: %s", code, w)
    if body != original:
        sprint_path.write_text(body, encoding="utf-8")
        result.changed = True
    return result


# ──────────────────────────────────────────────────────────────────────
#  Closing an ask (close-ask, Slack, ClickUp)
# ──────────────────────────────────────────────────────────────────────


def find_commitment(
    client: Any,
    *,
    project_id: str,
    code: str,
    cp_hash: str | None = None,
    text: str | None = None,
) -> dict | None:
    """The commitment a close-ask addresses: by ``cp_hash`` (the row's own,
    or the recipe over a row's text for rows not yet re-keyed), else by
    ``text`` — an exact match, then a unique case-insensitive substring of
    an OPEN row. Ambiguity returns None (never a guess)."""
    rows = fetch_project_commitments(client, project_id)
    if cp_hash:
        hit = [r for r in rows if r.get("cp_hash") == cp_hash] or [
            r for r in rows if ask_hash(code, r.get("description") or "") == cp_hash
        ]
        if hit:
            return sorted(hit, key=lambda r: r.get("status") == OPEN, reverse=True)[0]
    if text:
        m = match_ask(text, code=code, rows=rows)
        if m:
            return m.row
        needle = normalize_ask_text(text)
        subs = [r for r in rows if r.get("status") == OPEN
                and needle and needle in normalize_ask_text(r.get("description"))]
        if len(subs) == 1:
            return subs[0]
    return None
