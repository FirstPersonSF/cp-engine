"""Open asks for a document that may already have arrived (#415).

WHY. Tara Haney attached "Creative Brief V2.docx" to her 10-09 recap email
for sap-5198. The ask "Written brief document … (from Tara Haney)" stayed
open — and slipped 15 days — while the brief sat in an inbox. Nothing
compared what people had SENT us with what we were still WAITING for.

WHAT cp CAN SEE. cp has no mailbox integration. Mail reaches it only when
someone sends or forwards it to ``cp+<code>@`` (or the bare ``cp@``):

* ``cp+<code>@`` that resolves — the attachments are filed in the project's
  Dropbox and ingested (#376), so they arrive as ``rag_assets`` rows: the
  SOURCE STORE half below.
* bare ``cp@`` / an unknown ``+code`` — the email lands in MC-2's
  ``unrouted_emails`` holding pen, text only. Its attachment NAMES are kept
  in ``unrouted_emails.attachments`` (mc-2 migration 207); the files are
  not. That is the UNROUTED half below: an attachment that never reached
  the store at all.

An email nobody sends to cp is invisible to it. The 10-09 brief was one of
those, so this check would not have seen it; had the email been forwarded
to cp+sap-5198@, the brief would have been in the store on 10-09 and this
check would have named it beside the ask the first sweep after 10-16.

THE MATCH. An ask NAMES a document when its text carries a document word
(brief, deck, report, SOW …). A file answers it when its name shares that
word with the ask — "Creative Brief V2.docx" and "Written brief document".
Only real files count (a name with a document extension): a Fathom summary
titled "Video Ad Brief Walkthrough" is the meeting where the ask was made,
not the thing asked for. Only files that arrived from ``LOOKBACK_DAYS``
before the ask was logged count: an ask is often logged at the meeting
AFTER the email (the brief arrived 10-09, the ask was logged 10-16).

An unrouted email's attachment is matched only to asks that name its
sender (the ask's text or owner carries the sender's name or address) or
to the project its ``+code`` tried to reach — it has no project of its own.

It FLAGS. Closing the ask (or forwarding the email to ``cp+<code>@`` so the
file is filed) stays a person's call. Read-only.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from cp_engine.commitments_sweep import SweepRow, _content_words, _stem
from cp_engine.mc2_db import Tables

log = logging.getLogger(__name__)

# A document that arrived up to this long BEFORE the ask was logged still
# counts — meeting ingest logs the ask at the next call, after the email.
LOOKBACK_DAYS = 14

# Words that name a document. Stemmed the way the sweep stems, so
# "briefing" / "briefs" / "brief" are one word.
_DOC_WORDS = frozenset(
    _stem(w)
    for w in (
        "brief briefing deck doc document report sow contract proposal "
        "findings slides presentation spreadsheet sheet pdf file guidelines "
        "guide script storyboard outline invoice estimate spec specs memo "
        "agenda playbook library treatment treatments transcript"
    ).split()
)

# What a filed attachment looks like: a name ending in a document or media
# extension. A title with none (a meeting summary, a link card) is not a file.
_FILE_EXT = re.compile(
    r"\.(docx?|pdf|pptx?|key|xlsx?|csv|pages|numbers|rtf|txt|odt|ods|odp)$",
    re.IGNORECASE,
)
# Link cards carry a ".md" name and a "Link · " prefix; neither is a file sent in.
_LINK_PREFIX = re.compile(r"^\s*link\s*·", re.IGNORECASE)
_SPLIT = re.compile(r"[^a-z0-9]+")
_ADDR = re.compile(r"<?([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+)>?")


def is_document_file(name: str) -> bool:
    """True for a name that looks like a file someone sent: it has a document
    extension and is not a link card."""
    name = (name or "").strip()
    return bool(_FILE_EXT.search(name)) and not _LINK_PREFIX.match(name)


def filename_words(name: str) -> frozenset[str]:
    """``SAP Concur_2027 Video Ads_Creative Brief V2.docx`` →
    {sap, concur, video, ad, creative, brief}: stemmed, no extension, no
    token that carries a digit (a version or a year names no document)."""
    stem = _FILE_EXT.sub("", (name or "").strip()).lower()
    return frozenset(
        _stem(t) for t in _SPLIT.split(stem)
        if len(t) > 1 and not any(ch.isdigit() for ch in t)
    )


def document_words(text: str) -> frozenset[str]:
    """The document words an ask names (empty when it names none)."""
    return _content_words(text) & _DOC_WORDS


def match(ask_text: str, filename: str) -> tuple[str, ...] | None:
    """The words a file shares with an ask that names a document, or None.

    At least one shared word must be a document word: "Creative Brief
    V2.docx" answers "Written brief document" (brief); "Brand guidelines.pdf"
    does not answer "Send the brief"."""
    if not is_document_file(filename):
        return None
    ask = _content_words(ask_text)
    shared = ask & filename_words(filename)
    if not shared & _DOC_WORDS:
        return None
    return tuple(sorted(shared))


@dataclass
class Arrival:
    """One file cp holds a record of, and where."""

    name: str
    arrived: date
    where: str  # "store" | "unrouted"
    ref: str  # rag_assets.id | unrouted_emails.id
    project_id: str | None = None  # store rows only
    sender: str | None = None  # unrouted rows only
    attempted_code: str | None = None  # unrouted rows only


def _day(raw: Any) -> date | None:
    try:
        return datetime.fromisoformat(str(raw or "").replace("Z", "+00:00")).date()
    except ValueError:
        return None


# ── readers (explicit columns; `meta` and `raw_text` are never read) ────

STORE_COLUMNS = "id, title, created_at, status, project_id"
UNROUTED_COLUMNS = "id, from_addr, subject, received_at, attempted_code, status, attachments"


def read_store(client: Any, project_ids: list[str]) -> list[Arrival]:
    """The projects' source-store files (``rag_assets``), archived dropped."""
    if not project_ids:
        return []
    rows = (
        client.table(Tables.RAG_ASSETS)
        .select(STORE_COLUMNS)
        .in_("project_id", sorted(set(project_ids)))
        .execute()
        .data
    ) or []
    out: list[Arrival] = []
    for r in rows:
        day = _day(r.get("created_at"))
        name = r.get("title") or ""
        if r.get("status") == "archived" or day is None or not is_document_file(name):
            continue
        out.append(Arrival(name=name, arrived=day, where="store", ref=str(r.get("id")),
                           project_id=r.get("project_id")))
    return out


def read_unrouted(client: Any) -> list[Arrival]:
    """Attachment names on emails still waiting in ``unrouted_emails``.

    Raises when the read fails — including before mc-2 migration 207 adds
    the ``attachments`` column; the caller reports that, it never reads as
    "nothing waiting"."""
    rows = (
        client.table(Tables.UNROUTED_EMAILS)
        .select(UNROUTED_COLUMNS)
        .eq("status", "unrouted")
        .execute()
        .data
    ) or []
    out: list[Arrival] = []
    for r in rows:
        day = _day(r.get("received_at"))
        if day is None:
            continue
        for a in r.get("attachments") or []:
            name = (a or {}).get("filename") if isinstance(a, dict) else None
            if name and is_document_file(name):
                out.append(Arrival(name=name, arrived=day, where="unrouted",
                                   ref=str(r.get("id")), sender=r.get("from_addr"),
                                   attempted_code=r.get("attempted_code")))
    return out


# ── the match over a sweep ────────────────────────────────────────────


def _sender_keys(sender: str | None) -> tuple[str | None, frozenset[str]]:
    """(address, name words) from ``"Haney, Tara" <tara.haney@sap.com>``."""
    s = sender or ""
    m = _ADDR.search(s)
    addr = m.group(1).lower() if m else None
    display = _ADDR.sub(" ", s).lower()
    words = {t for t in _SPLIT.split(display) if len(t) > 2}
    if not words and addr:
        words = {t for t in _SPLIT.split(addr.split("@")[0]) if len(t) > 2}
    return addr, frozenset(words)


def names_sender(row: SweepRow, sender: str | None) -> bool:
    """The ask's text or owner names the sender: their address, or two of
    their name words ("Tara" and "Haney")."""
    addr, words = _sender_keys(sender)
    hay = f"{row.description} {row.owner}".lower()
    if addr and addr in hay:
        return True
    hay_words = set(_SPLIT.split(hay))
    return len(words) >= 2 and len(words & hay_words) >= 2


def arrivals(
    groups: dict[str, list[SweepRow]],
    *,
    today: date,
    store: list[Arrival],
    unrouted: list[Arrival],
    code_ids: dict[str, str],
    same_project: Any = None,
) -> list[dict]:
    """One entry per open ask that names a document a file may answer:
    ``{project_code, key, description, document_words, files: [{name,
    arrived, where, ref, shared, sender?}]}``.

    ``code_ids`` maps a group key (project code) → its MC-2 id, so store
    files land on their own project only. ``same_project(a, b)`` decides
    whether an unrouted email's attempted ``+code`` is this project
    (defaults to a case-insensitive prefix match on the short code)."""
    same = same_project or _same_code
    out: list[dict] = []
    for code, rows in groups.items():
        pid = code_ids.get(code)
        mine = [a for a in store if pid and a.project_id == pid]
        for r in rows:
            if not document_words(r.description):
                continue
            logged = today - timedelta(days=r.age_days)
            since = logged - timedelta(days=LOOKBACK_DAYS)
            files: list[dict] = []
            for a in mine:
                shared = match(r.description, a.name) if a.arrived >= since else None
                if shared:
                    files.append(_file(a, shared))
            for a in unrouted:
                if a.arrived < since:
                    continue
                if not (names_sender(r, a.sender)
                        or (a.attempted_code and same(a.attempted_code, code))):
                    continue
                shared = match(r.description, a.name)
                if shared:
                    files.append(_file(a, shared))
            if files:
                files.sort(key=lambda f: (f["where"] != "unrouted", f["arrived"]))
                out.append({
                    "project_code": code,
                    "key": r.id,
                    "description": r.description,
                    "document_words": sorted(document_words(r.description)),
                    "files": files,
                })
    return out


def _file(a: Arrival, shared: tuple[str, ...]) -> dict:
    f = {"name": a.name, "arrived": a.arrived.isoformat(), "where": a.where,
         "ref": a.ref, "shared": list(shared)}
    if a.sender:
        f["sender"] = a.sender
    return f


def _same_code(attempted: str, code: str) -> bool:
    a, c = attempted.strip().lower(), code.strip().lower()
    return bool(a) and (a == c or c.startswith(a + "-") or a.startswith(c + "-"))


def code_ids_for(client: Any, groups: dict[str, list[SweepRow]],
                 code: str | None = None) -> dict[str, str]:
    """Group key → MC-2 project id. A one-project sweep keys its group by the
    code the caller typed, so that one resolves through the commitment-owner
    resolver; a tenant-wide sweep keys by ``projects.code``."""
    if code is not None:
        from cp_engine.commitments import resolve_commitment_owner

        owner = resolve_commitment_owner(client, code)
        return {code: owner["id"]} if owner else {}
    rows = client.table(Tables.PROJECTS).select("id, code").execute().data or []
    return {r["code"]: r["id"] for r in rows if r.get("code") and r.get("id")}


def find(client: Any, groups: dict[str, list[SweepRow]], *, today: date,
         code: str | None = None,
         code_ids: dict[str, str] | None = None,
         canonical: dict[str, str] | None = None) -> tuple[list[dict], list[str]]:
    """Read both halves and match. Returns ``(arrivals, warnings)``: a half
    that cannot be read is named in ``warnings`` and the other still runs —
    an unreadable source never reads as "nothing arrived". ``canonical``
    (``projects.code`` → tree slug) lets an unrouted ``+code`` typed as the
    slug or its short form find its project. ``code_ids`` defaults to
    ``code_ids_for(client, groups, code)``."""
    canonical = canonical or {}
    warnings: list[str] = []
    if not groups:
        return [], warnings
    if code_ids is None:
        try:
            code_ids = code_ids_for(client, groups, code)
        except Exception as exc:  # noqa: BLE001 — reported, never silent
            code_ids = {}
            warnings.append(f"projects unreadable: {type(exc).__name__}: {str(exc)[:160]}")
    ids = [code_ids[c] for c in groups if code_ids.get(c)]
    try:
        store = read_store(client, ids)
    except Exception as exc:  # noqa: BLE001 — reported, never silent
        log.warning("document arrivals: source store unreadable", exc_info=True)
        store = []
        warnings.append(f"source store unreadable: {type(exc).__name__}: {str(exc)[:160]}")
    try:
        unrouted = read_unrouted(client)
    except Exception as exc:  # noqa: BLE001 — e.g. mc-2 migration 207 not applied
        log.warning("document arrivals: unrouted emails unreadable", exc_info=True)
        unrouted = []
        warnings.append(
            f"unrouted email attachments unreadable: {type(exc).__name__}: {str(exc)[:160]}"
        )
    def same(attempted: str, code: str) -> bool:
        return _same_code(attempted, code) or _same_code(attempted, canonical.get(code, ""))

    return arrivals(groups, today=today, store=store, unrouted=unrouted,
                    code_ids=code_ids, same_project=same), warnings


def render(found: list[dict], warnings: list[str] | None = None) -> list[str]:
    """The sweep's lines for this check (empty when there is nothing to say)."""
    lines: list[str] = []
    if found:
        lines.append(f"📎 {len(found)} open ask(s) for a document that may already be here "
                     "— check the file, then resolve the ask:")
        for e in found:
            lines.append(f"    {e['project_code']} · {e['key'][:8]}  {e['description'][:70]}")
            for f in e["files"]:
                if f["where"] == "unrouted":
                    where = (f"attached to an unrouted email from {f.get('sender') or '?'} "
                             "— not in the source store; forward it to cp+<code>@")
                else:
                    where = "in the source store"
                lines.append(f"      ↳ {f['name']} ({f['arrived']}) — {where}")
    for w in warnings or []:
        lines.append(f"📎 document check incomplete — {w}")
    return lines
