"""Stakeholders live on spine cards in MC-2 (architecture plan step 4b,
decided 2026-10-01).

ONE OWNER. A person on a workstream is a live ``spine_substance`` row with
``layer='Stakeholders'``. The hand-written cp.md ``## Stakeholders`` section
and the sprint file's ``### Stakeholders`` subsection are no longer stores:
sync imports what a person typed there onto cards, quarantines the text
(``exceptions/region-edits/``) and removes the section. What stays is ONE
read-only view, the cp.md ``stakeholders-strip`` region, rendered from cards.

THE DETAILS BLOCK. Cards are prose (``framing`` + ``body``); the structured
facts the markdown used to carry — role, company, email, client vs internal,
spelling variants — ride in a small block at the TOP of the card body. No
schema change. Format (every line optional except the markers; a field that
is unknown is left out, never written as a placeholder)::

    <!-- cp:stakeholder -->
    - **Name:** Jaime Mehra
    - **Role:** Marketing leadership; AI-campaign decision-maker
    - **Company:** Infoblox
    - **Email:** jmehra@infoblox.com
    - **Side:** client
    - **Aliases:** Jamie, Jamie Mehra
    <!-- /cp:stakeholder -->

``Side`` is ``client`` (the client organisation's people), ``internal``
(First Person staff and contractors) or ``external`` (anyone else: vendors,
interviewees from other companies, partners). ``Aliases`` are other
spellings that mean THIS person — matching, attribution and ingest dedupe
read them. The list renders as a readable block in MC-2; the HTML comments
are what the parser keys on. :func:`parse_details` / :func:`render_details`
are the one definition; ingest, the strip, attribution, the migration and
the build-stakeholder skill all use it.

Readers take cards from MC-2 when they hold a client (:func:`fetch_cards`)
and from the generated view on disk otherwise (:func:`cards_from_view`) —
``spine/`` and ``_stakeholders/`` are rendered from MC-2 on every sync, so
both are the same cards.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

LAYER = "Stakeholders"
SIDES = ("client", "internal", "external")

DETAILS_START = "<!-- cp:stakeholder -->"
DETAILS_END = "<!-- /cp:stakeholder -->"
_FIELDS = (("name", "Name"), ("role", "Role"), ("company", "Company"),
           ("email", "Email"), ("side", "Side"), ("aliases", "Aliases"))
_LABEL_TO_KEY = {label.lower(): key for key, label in _FIELDS}
_DETAILS_RE = re.compile(
    r"\A\s*" + re.escape(DETAILS_START) + r"\n(?P<inner>.*?)\n?"
    + re.escape(DETAILS_END) + r"[ \t]*\n?",
    re.S,
)
_FIELD_RE = re.compile(r"^\s*[-*]\s+\*\*(?P<k>[^*:]+):\*\*\s*(?P<v>.*?)\s*$")

#: Columns a card read needs — explicit, never ``*`` (tenant rule). Carries
#: what ``build_version_rows`` reads off the base row, so a details update can
#: version a card without a second fetch.
CARD_COLUMNS = (
    "id, project_id, project_code, company_id, est_item_id, scope, layer, "
    "status, archived, origin, version_label, version_date, framing, body, "
    "serves, sources, important, note, actor, lifetime, card_kind"
)


# ──────────────────────────────────────────────────────────────────────
#  The details block
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Details:
    name: str = ""
    role: str = ""
    company: str = ""
    email: str = ""
    side: str = ""
    aliases: tuple[str, ...] = ()
    #: Unknown ``- **Key:** value`` lines, kept verbatim and re-rendered, so a
    #: field added later by a person or a newer engine survives a rewrite.
    extra: tuple[tuple[str, str], ...] = ()

    def is_empty(self) -> bool:
        return not (self.name or self.role or self.company or self.email
                    or self.side or self.aliases or self.extra)


def parse_details(body: str | None) -> Details | None:
    """The details block at the top of ``body``, or None when there is none."""
    m = _DETAILS_RE.match(body or "")
    if not m:
        return None
    vals: dict[str, Any] = {}
    extra: list[tuple[str, str]] = []
    for line in m.group("inner").splitlines():
        f = _FIELD_RE.match(line)
        if not f:
            continue
        label, value = f.group("k").strip(), f.group("v").strip()
        key = _LABEL_TO_KEY.get(label.lower())
        if key is None:
            extra.append((label, value))
        elif key == "aliases":
            vals[key] = tuple(a.strip() for a in value.split(",") if a.strip())
        elif key == "side":
            vals[key] = value.lower()
        else:
            vals[key] = value
    return Details(**vals, extra=tuple(extra))


def strip_details(body: str | None) -> str:
    """``body`` without its details block (the prose a person wrote)."""
    return _DETAILS_RE.sub("", body or "", count=1)


def render_details(d: Details) -> str:
    lines = [DETAILS_START]
    for key, label in _FIELDS:
        v = getattr(d, key)
        if key == "aliases":
            v = ", ".join(v)
        if v:
            lines.append(f"- **{label}:** {v}")
    for label, v in d.extra:
        lines.append(f"- **{label}:** {v}")
    lines.append(DETAILS_END)
    return "\n".join(lines)


def with_details(body: str | None, d: Details) -> str:
    """``body`` with its details block replaced by (or prefixed with) ``d``."""
    rest = strip_details(body).lstrip("\n")
    block = render_details(d)
    return f"{block}\n\n{rest}" if rest.strip() else f"{block}\n"


def merge_details(old: Details | None, new: Details) -> Details:
    """Fill ``old``'s blanks from ``new`` and union the aliases. Never
    overwrites a value a card already carries — the card is the owner, and a
    person may have corrected it."""
    if old is None:
        return new
    aliases = list(old.aliases)
    for a in new.aliases:
        known = {norm_name(x) for x in aliases} | {norm_name(old.name or new.name)}
        if norm_name(a) not in known:
            aliases.append(a)
    return replace(
        old,
        name=old.name or new.name,
        role=old.role or new.role,
        company=old.company or new.company,
        email=old.email or new.email,
        side=old.side or new.side,
        aliases=tuple(aliases),
    )


# ──────────────────────────────────────────────────────────────────────
#  Names
# ──────────────────────────────────────────────────────────────────────

_NAME_WORD_RE = re.compile(r"^[A-Z][A-Za-z'À-ſ.-]*$")
_NOT_A_PERSON_RE = re.compile(
    r"\b(vendor|agency|company|profile|roster|cast|list|owners|stakeholders|"
    r"team|managers|client|group|committee|leadership team)\b",
    re.IGNORECASE,
)
_NAME_WORDS_MAX = 4


def norm_name(name: str | None) -> str:
    n = re.sub(r"\(.*?\)", " ", name or "")
    n = re.sub(r"[*_`\"“”]", "", n)
    n = re.sub(r"[^A-Za-zÀ-ɏ' .-]", " ", n)
    return " ".join(n.lower().replace(".", " ").split())


def name_from_framing(framing: str | None) -> str | None:
    """``Morgan Wright — Salesloft (…)`` → ``Morgan Wright``;
    ``Participant — Ryan Person (Sovos, …)`` → ``Ryan Person``. None when the
    framing names a group or an organisation (``Triptych — sub-vendor
    profile``, ``Google EHS cast — …``, ``Key stakeholders``)."""
    v = (framing or "").strip().strip("'\"")
    v = re.sub(r"^Participant\s*[—–-]\s*", "", v)
    head = re.split(r"\s+[—–-]\s*|[—–]|\s-|,|\(|;|:|\s{2,}", v, maxsplit=1)[0].strip()
    words = head.split()
    if not words or len(words) > _NAME_WORDS_MAX:
        return None
    if not all(_NAME_WORD_RE.match(w) for w in words):
        return None
    if _NOT_A_PERSON_RE.search(v) and len(words) == 1:
        return None
    return head


def is_person_name(name: str) -> bool:
    """A mention names one person: 1–4 capitalised words, no list
    punctuation, no group noun ("Joe, Gretchen, regional managers",
    "Infoblox (IBX)", "Madrid + Ireland MarCom team" are not people)."""
    if not name or re.search(r"[,+/&]|\band\b", name):
        return False
    bare = re.sub(r"\(.*?\)", "", name).strip()
    words = bare.split()
    if not words or len(words) > _NAME_WORDS_MAX:
        return False
    if _NOT_A_PERSON_RE.search(name):
        return False
    if len(words) == 1 and len(words[0]) > 1 and words[0].isupper():
        return False  # an acronym ("CMO", "IBX"), not a name
    return all(_NAME_WORD_RE.match(w) for w in words)


def apply_aliases(text: str, aliases: Mapping[str, str] | None) -> str:
    """The tenant ``[names] aliases`` rewrite (word boundaries, longest key
    first, case-sensitive) — the same rule ``attribution.apply_aliases``
    applies to plan text."""
    if not text or not aliases:
        return text
    for wrong in sorted(aliases, key=len, reverse=True):
        right = aliases[wrong]
        if not wrong or wrong == right:
            continue
        text = re.sub(rf"(?<![\w-]){re.escape(wrong)}(?![\w-])", right, text)
    return text


# ──────────────────────────────────────────────────────────────────────
#  Cards
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Card:
    est_item_id: str
    project_code: str
    framing: str
    body: str
    project_id: str | None = None
    scope: str = "project"
    company_id: str | None = None
    row: Mapping[str, Any] | None = None   # the live MC-2 row, when read from MC-2

    @property
    def details(self) -> Details | None:
        return parse_details(self.body)

    @property
    def name(self) -> str:
        d = self.details
        if d and d.name:
            return d.name
        return name_from_framing(self.framing) or ""

    @property
    def aliases(self) -> tuple[str, ...]:
        d = self.details
        return d.aliases if d else ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(n for n in (self.name, *self.aliases) if n)

    @property
    def side(self) -> str:
        d = self.details
        return d.side if d else ""

    @property
    def role(self) -> str:
        d = self.details
        if d and d.role:
            return d.role
        return _framing_tail(self.framing, self.name)

    @property
    def is_person(self) -> bool:
        return bool(self.name)


def _framing_tail(framing: str, name: str) -> str:
    v = re.sub(r"^Participant\s*[—–-]\s*", "", (framing or "").strip())
    if name and v.startswith(name):
        v = v[len(name):]
    return v.strip(" —–-:,").strip()


def card_from_row(row: Mapping[str, Any]) -> Card:
    return Card(
        est_item_id=str(row.get("est_item_id") or ""),
        project_code=str(row.get("project_code") or ""),
        framing=str(row.get("framing") or ""),
        body=str(row.get("body") or ""),
        project_id=row.get("project_id"),
        scope=str(row.get("scope") or "project"),
        company_id=row.get("company_id"),
        row=dict(row),
    )


def _is_live_card(row: Mapping[str, Any]) -> bool:
    return (str(row.get("layer") or "").lower() in ("stakeholders", "stakeholder")
            and row.get("status") == "live" and not row.get("archived"))


def fetch_cards(client: Any, *, project_ids: Iterable[str] | None = None,
                company_id: str | None = None) -> list[Card]:
    """Live, non-archived Stakeholders cards from MC-2: those of
    ``project_ids`` plus (with ``company_id``) every account-scope card of
    that company, wherever it was authored — or tenant-wide when neither is
    given. Paginated; explicit columns. RAISES on a read failure: an empty
    answer must never read as "nobody is known"."""
    from cp_engine.mc2_db import Tables

    def _pages(**filters) -> list[dict]:
        rows: list[dict] = []
        start = 0
        while True:
            q = (client.table(Tables.SPINE_SUBSTANCE).select(CARD_COLUMNS)
                 .eq("layer", LAYER).eq("status", "live"))
            for col, val in filters.items():
                q = q.in_(col, val) if isinstance(val, list) else q.eq(col, val)
            page = q.range(start, start + 999).execute().data or []
            rows.extend(page)
            if len(page) < 1000:
                return rows
            start += 1000

    if project_ids is None and company_id is None:
        rows = _pages()
    else:
        rows = _pages(project_id=sorted(set(project_ids))) if project_ids else []
        if company_id:
            rows += _pages(company_id=company_id, scope="account")
    out, seen = [], set()
    for r in rows:
        if _is_live_card(r) and r.get("id") not in seen:
            seen.add(r.get("id"))
            out.append(card_from_row(r))
    return out


def project_companies(client: Any) -> dict[str, str | None]:
    """``{project id: company id}`` for every MC-2 project (explicit columns),
    so a workstream's account-scope cards are found by COMPANY — they may be
    authored on a workstream that is no longer active (ibx-5192's)."""
    from cp_engine.mc2_db import Tables

    out: dict[str, str | None] = {}
    start = 0
    while True:
        page = (client.table(Tables.PROJECTS).select("id, company_id")
                .range(start, start + 999).execute().data) or []
        out.update({str(r["id"]): r.get("company_id") for r in page if r.get("id")})
        if len(page) < 1000:
            return out
        start += 1000


_VERSION_HEAD_RE = re.compile(r"^## (?P<label>v\d+) — (?P<date>\S+) · (?P<status>\w+)[ \t]*$", re.M)


def parse_card_file(text: str) -> tuple[str, str, str] | None:
    """``(est_item_id, framing, body)`` of a rendered card file's LIVE
    version, or None when it is not a live, unarchived Stakeholders card.
    Tolerant on purpose (a hand-made fixture, a pre-#216 file): it needs only
    the frontmatter's ``layer`` and a ``## vN — date · live`` block."""
    if not text.startswith("---"):
        return None
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        return None
    fm, rest = parts[0], parts[1].split("\n", 1)[1] if "\n" in parts[1] else ""
    if not re.search(r"^layer:\s*Stakeholders?\s*$", fm, re.M):
        return None
    if re.search(r"^archived:\s*true\s*$", fm, re.M | re.I):
        return None
    eid_m = re.search(r"^est_item_id:\s*(\S+)\s*$", fm, re.M)
    heads = list(_VERSION_HEAD_RE.finditer(rest))
    for i, h in enumerate(heads):
        if h.group("status") != "live":
            continue
        block = rest[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(rest)]
        lines = block.split("\n")[1:] if block.startswith("\n") else block.split("\n")
        framing, j = "", 0
        while j < len(lines):
            ln = lines[j]
            if ln.startswith("framing:"):
                framing = ln[len("framing:"):].strip().strip("'\"")
            elif ln.startswith("sources:") or (ln.startswith(("- ", "  ")) and j > 0):
                pass
            elif not ln.strip():
                j += 1
                break
            else:
                break
            j += 1
        body = "\n".join(lines[j:]).strip("\n")
        return (eid_m.group(1) if eid_m else ""), framing, body
    return None


def cards_from_view(tenant_root: Path | None, project_dir: Path | None) -> list[Card]:
    """Cards from the generated view on disk: the workstream's and each
    ancestor's ``spine/_authored/*.md`` plus each ancestor's
    ``_stakeholders/*.md`` (account scope), live version only."""
    out: list[Card] = []
    if project_dir is None:
        return out
    root = tenant_root.resolve() if tenant_root else None
    d = project_dir.resolve()
    seen: set[Path] = set()
    for _ in range(8):
        if root is not None and d == root:
            break
        for folder, scope in ((d / "spine" / "_authored", "project"),
                              (d / "_stakeholders", "account")):
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.md")):
                if path in seen:
                    continue
                seen.add(path)
                try:
                    parsed = parse_card_file(path.read_text(encoding="utf-8"))
                except OSError:
                    continue
                if parsed is None:
                    continue
                eid, framing, body = parsed
                out.append(Card(
                    est_item_id=eid or f"_authored/{path.stem}",
                    project_code=d.name if scope == "project" else "",
                    framing=framing, body=body, scope=scope,
                ))
        if root is None or d.parent == d:
            break
        d = d.parent
    return out


def workstream_chain(index: Mapping[str, Any], code: str) -> list[str]:
    """The MC-2 ids whose PROJECT-scope cards apply to ``code``: itself, then
    each ancestor (paths index entries or ProjectState objects — anything
    with ``mc2_id`` and ``parent``/``parent_code``)."""
    chain: list[str] = []
    cur, hops = index.get(code), 0
    while cur is not None and hops < 10:
        mc2 = getattr(cur, "mc2_id", None)
        if mc2:
            chain.append(mc2)
        parent = getattr(cur, "parent", None) or getattr(cur, "parent_code", None)
        cur = index.get(parent) if parent else None
        hops += 1
    return chain


def select_for_workstream(cards: Iterable[Card], *, chain_ids: list[str],
                          company_id: str | None = None) -> list[Card]:
    """The cards that apply to one workstream: project-scope cards of itself
    and its ancestors (nearest first), then its company's account-scope
    cards. One card per element."""
    rank = {pid: i for i, pid in enumerate(chain_ids)}
    picked: dict[tuple[str, str], tuple[int, Card]] = {}
    for c in cards:
        if c.scope == "account":
            if not company_id or c.company_id != company_id:
                continue
            r = len(rank) + 1
        elif c.project_id in rank:
            r = rank[c.project_id]
        else:
            continue
        key = (str(c.project_id), c.est_item_id)
        if key not in picked or r < picked[key][0]:
            picked[key] = (r, c)
    return [c for _, c in sorted(picked.values(), key=lambda rc: (rc[0], rc[1].name.lower()))]


def cards_for(tenant_root: Path, code: str, *, client: Any = None,
              project_dir: Path | None = None) -> list[Card]:
    """Every card that applies to ``code`` — MC-2 when ``client`` is given,
    the generated view on disk otherwise."""
    from cp_engine.state import load_paths_index

    if client is not None:
        chain = workstream_chain(load_paths_index(tenant_root), code)
        if chain:
            company_id, _ = company_of(client, chain[0])
            cards = fetch_cards(client, project_ids=chain, company_id=company_id)
            return select_for_workstream(cards, chain_ids=chain, company_id=company_id)
    return cards_from_view(tenant_root, project_dir)


def match_card(cards: Iterable[Card], name: str, *,
               aliases: Mapping[str, str] | None = None) -> Card | None:
    """The one card that IS ``name``, or None. Exact on the card's name or
    any of its aliases (after the tenant alias rewrite); then a first name
    alone that belongs to exactly one card, or a full name whose first name
    is a first-name-only card. Never a surname-only or fuzzy match — "Nate
    Johnson" is not "Tyler Johnson"."""
    cards = [c for c in cards if c.is_person]
    n = norm_name(apply_aliases(name, aliases))
    if not n:
        return None
    for c in cards:
        if n in {norm_name(x) for x in c.names}:
            return c
    words = n.split()
    if len(words) == 1:
        hits = {(c.project_id, c.est_item_id): c for c in cards
                if any(norm_name(x).split()[:1] == words for x in c.names)}
    else:
        hits = {(c.project_id, c.est_item_id): c for c in cards
                if any(norm_name(x) == words[0] for x in c.names)}
    return next(iter(hits.values())) if len(hits) == 1 else None


# ──────────────────────────────────────────────────────────────────────
#  Markdown mentions (the stores being retired)
# ──────────────────────────────────────────────────────────────────────


@dataclass
class Mention:
    """One person as the markdown recorded them."""

    name: str
    role: str = ""
    context: str = ""
    email: str = ""
    group: str = ""
    aliases: tuple[str, ...] = ()
    source: str = ""          # "cp.md" | "sprint 2026-W39"
    code: str = ""
    raw: str = ""

    @property
    def text(self) -> str:
        return " · ".join(p for p in (self.role, self.context) if p)


CPMD_HEADING_RE = re.compile(r"^## Stakeholders[ \t]*$", re.M)
_NEXT_H2_RE = re.compile(r"^## ", re.M)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# `- **Name** — rest` (separator optional after a bold name), `- Name — rest`
# (separator REQUIRED, or a lazy name would stop after one letter), `- Name`.
_BULLET_RE = re.compile(
    r"^\s*[-*]\s+(?:\*\*(?P<bn>[^*]+)\*\*\s*(?:[—–:]|\s-\s)?"
    r"|(?P<pn>[^—–:|]+?)\s*(?:[—–:]|\s-\s)"
    r"|(?P<only>[^—–:|]+?)\s*$)\s*(?P<rest>.*)$"
)
_TABLE_RE = re.compile(r"^\s*\|(?P<cells>.*)\|\s*$")
_GROUP_RE = re.compile(r"^\s*\*\*(?P<g>[^*]+)\*\*")
_PAREN_ALIAS_RE = re.compile(r"\(([A-Z][a-z]+)\)")


def cpmd_section_span(body: str) -> tuple[int, int] | None:
    """``(start, end)`` of the hand-written ``## Stakeholders`` section (the
    exact heading — never the ``(auto-aggregated…)``/``(from spine cards)``
    strip, which carries a suffix), heading line included."""
    m = CPMD_HEADING_RE.search(body)
    if not m:
        return None
    nxt = _NEXT_H2_RE.search(body, m.end())
    # A region marker that opens before the next heading ends the section too.
    marker = body.find("<!-- cp-engine:start ", m.end())
    end = nxt.start() if nxt else len(body)
    if 0 <= marker < end:
        end = marker
    return m.start(), end


def _split_name(raw: str) -> tuple[str, tuple[str, ...]]:
    aliases = tuple(_PAREN_ALIAS_RE.findall(raw))
    name = " ".join(re.sub(r"\(.*?\)", " ", raw).replace("*", "").split())
    return name, aliases


def parse_cpmd_section(body: str, *, code: str = "") -> list[Mention]:
    """Every entry of the hand-written cp.md section: bullets and tables,
    under ``**Group**`` headers. Placeholders and prose are skipped."""
    span = cpmd_section_span(body)
    if span is None:
        return []
    text = body[span[0]:span[1]].split("\n", 1)[1] if "\n" in body[span[0]:span[1]] else ""
    out: list[Mention] = []
    group = ""
    header: list[str] | None = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("_<") or s.startswith(">") or s.startswith("<!--"):
            continue
        t = _TABLE_RE.match(line)
        if t:
            cells = [c.strip() for c in t.group("cells").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if header is None or cells[0].lower() == "name":
                header = [c.lower() for c in cells]
                if cells[0].lower() == "name":
                    continue
            name, aliases = _split_name(cells[0])
            cols = dict(zip(header, cells, strict=False))
            email = next((c for c in cells if _EMAIL_RE.fullmatch(c)), "")
            role = cols.get("role") or cols.get("notes") or ""
            if not role:
                role = " · ".join(c for c in cells[1:] if c and c != email)
            out.append(Mention(name=name, role=role, email=email, group=group,
                               aliases=aliases, source="cp.md", code=code, raw=line))
            continue
        header = None
        g = _GROUP_RE.match(line)
        if g and not s.startswith(("-", "*  ", "* ")):
            group = g.group("g").strip()
            continue
        b = _BULLET_RE.match(line)
        if b:
            raw_name = (b.group("bn") or b.group("pn") or b.group("only") or "").strip()
            rest = (b.group("rest") or "").strip()
            if not raw_name or len(raw_name) > 60:
                continue
            name, aliases = _split_name(raw_name)
            email = (_EMAIL_RE.search(rest).group(0) if _EMAIL_RE.search(rest) else "")
            out.append(Mention(name=name, role=rest, email=email, group=group,
                               aliases=aliases, source="cp.md", code=code, raw=line))
    return out


_SUB_RE = re.compile(r"^### Stakeholders[ \t]*$", re.M)
_NEXT_SUB_RE = re.compile(r"^#{2,3} ", re.M)
_COMM_RE = re.compile(r"^## (?:Client|Team) communication\b.*$", re.M)


def sprint_subsection_span(body: str) -> tuple[int, int] | None:
    """``(start, end)`` of ``### Stakeholders`` under the communication
    section, heading included, up to the next ``##``/``###`` heading."""
    comm = _COMM_RE.search(body)
    if not comm:
        return None
    m = _SUB_RE.search(body, comm.end())
    if not m:
        return None
    nxt_h2 = _NEXT_H2_RE.search(body, comm.end())
    if nxt_h2 and nxt_h2.start() < m.start():
        return None  # the subsection belongs to a later section
    nxt = _NEXT_SUB_RE.search(body, m.end())
    return m.start(), (nxt.start() if nxt else len(body))


def parse_sprint_subsection(body: str, *, code: str = "", week: str = "") -> list[Mention]:
    """Every ``[name · role · context]`` bullet of ``### Stakeholders``,
    continuation lines joined into the context."""
    from cp_engine.sprints import bullets, parse_bracketed_bullet

    span = sprint_subsection_span(body)
    if span is None:
        return []
    out: list[Mention] = []
    chunk = body[span[0]:span[1]]
    for first, cont in bullets(chunk):
        parsed = parse_bracketed_bullet(first)
        if not parsed or not parsed[0] or not parsed[0][0].strip():
            continue
        parts, tail = parsed
        name, aliases = _split_name(parts[0])
        role = parts[1] if len(parts) > 1 else ""
        context = " · ".join(p for p in parts[2:] if p)
        tail = re.sub(r"\s*<!--.*?-->", "", tail).strip()
        extra = " ".join(x for x in (tail, " ".join(cont.split())) if x)
        context = " ".join(x for x in (context, extra) if x)
        email = (_EMAIL_RE.search(first + cont).group(0)
                 if _EMAIL_RE.search(first + cont) else "")
        out.append(Mention(name=name, role=role, context=context, email=email,
                           aliases=aliases, source=f"sprint {week}".strip(),
                           code=code, raw=first + ("\n" + cont if cont else "")))
    return out


def has_content(span_text: str) -> bool:
    """Anything in a section beyond its heading, placeholders and comments."""
    lines = span_text.splitlines()[1:]
    for ln in lines:
        s = ln.strip()
        if s and not s.startswith("_<") and not (s.startswith("<!--") and s.endswith("-->")):
            return True
    return False


def remove_span(body: str, span: tuple[int, int]) -> str:
    """``body`` without ``span``, leaving exactly one blank line where it was."""
    head, tail = body[: span[0]].rstrip("\n"), body[span[1]:].lstrip("\n")
    if not head:
        return tail
    return f"{head}\n\n{tail}" if tail else f"{head}\n"


# ──────────────────────────────────────────────────────────────────────
#  Who is a client stakeholder
# ──────────────────────────────────────────────────────────────────────

_INTERNAL_GROUP_RE = re.compile(r"first person|\binternal\b|\bFP\b|\bteam\b", re.I)
_EXTERNAL_GROUP_RE = re.compile(r"vendor|agency|partner|supplier", re.I)
_EXTERNAL_ROLE_RE = re.compile(r"\b(interviewee|participant|vendor|agency|sub-vendor)\b", re.I)


@dataclass(frozen=True)
class Roster:
    """Who is NOT a client stakeholder. ``team`` are first names from the
    tenant ``[team] members``; ``internal_names`` full names (MC-2 staff and
    freelancer entities, plus anyone the tenant aliases map onto one)."""

    team: frozenset[str] = frozenset()
    internal_names: frozenset[str] = frozenset()

    @classmethod
    def build(cls, team: Iterable[str] = (), internal_names: Iterable[str] = ()) -> Roster:
        return cls(
            team=frozenset(t.strip().split()[0].lower() for t in team if t.strip()),
            internal_names=frozenset(norm_name(n) for n in internal_names if n),
        )

    def is_internal(self, name: str, group: str = "") -> bool:
        """A known internal full name (MC-2 staff/freelancer entity); a bare
        first name on the tenant ``[team]`` roster ("Maria", "Tony"); or
        anyone listed under an internal group header ("**First Person
        (internal)**"). A FULL name that merely shares a team member's first
        name is not internal — slt-5196's participant Drew Moldenhauer is not
        Drew Fiero — and an entity's bare first name is not either (the
        client's "Scott" is not freelancer Scott Bartholomew)."""
        n = norm_name(name)
        if not n:
            return False
        if n in self.internal_names:
            return True
        words = n.split()
        if len(words) == 1 and words[0] in self.team:
            return True
        if len(words) > 1:
            # A mis-spelt surname of an internal person ("Stefan Muma" for
            # freelancer Stefan Mumaw, "Geoff Ahman"): same first name, a
            # surname within a couple of letters. "Drew Moldenhauer" is not
            # "Drew Fiero".
            import difflib

            last = " ".join(words[1:])
            for x in self.internal_names:
                xw = x.split()
                if len(xw) > 1 and xw[0] == words[0] and difflib.SequenceMatcher(
                        None, last, " ".join(xw[1:])).ratio() >= 0.8:
                    return True
        return bool(group) and bool(_INTERNAL_GROUP_RE.search(group)) and not re.search(
            r"client", group, re.I)

    def is_internal_first_name(self, name: str) -> bool:
        """A bare first name that an internal full name starts with. Asked
        only AFTER no card matched (the client's "Scott" card wins over
        freelancer Scott Bartholomew)."""
        words = norm_name(name).split()
        return len(words) == 1 and any(
            x.split()[0] == words[0] for x in self.internal_names if x)


def internal_names_from_mc2(client: Any) -> list[str]:
    """MC-2's staff and freelancer entity names (explicit columns)."""
    from cp_engine.mc2_db import Tables

    rows = (client.table(Tables.ENTITIES).select("name, kind, archived_at")
            .in_("kind", ["staff", "freelancer"]).execute().data) or []
    return [r["name"] for r in rows if r.get("name")]


def company_of(client: Any, project_id: str) -> tuple[str | None, str]:
    """``(company_id, company name)`` of one workstream, from MC-2."""
    from cp_engine.mc2_db import Tables

    proj = (client.table(Tables.PROJECTS).select("company_id").eq("id", project_id)
            .limit(1).execute().data) or []
    cid = proj[0].get("company_id") if proj else None
    if not cid:
        return None, ""
    co = (client.table(Tables.COMPANIES).select("name").eq("id", cid)
          .limit(1).execute().data) or []
    return cid, (co[0].get("name") or "") if co else ""


def company_name_for(client: Any, project_id: str) -> str:
    return company_of(client, project_id)[1]


def side_for(m: Mention, *, default: str = "client") -> str:
    if m.group and _EXTERNAL_GROUP_RE.search(m.group):
        return "external"
    if _EXTERNAL_ROLE_RE.search(m.role or "") or _EXTERNAL_ROLE_RE.search(m.context or ""):
        return "external"
    return default


# ──────────────────────────────────────────────────────────────────────
#  Writing cards
# ──────────────────────────────────────────────────────────────────────


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= n:
        return text
    cut = text[:n]
    for sep in ("; ", ". ", ", ", " — ", " "):
        i = cut.rfind(sep)
        if i > n // 2:
            return cut[:i].rstrip(" ;,.—")
    return cut


def framing_for(name: str, role: str) -> str:
    role = _clip(re.sub(r"\s*<!--.*?-->", "", role or ""), 80)
    return f"{name} — {role}" if role else name


def card_body(details: Details, mentions: Iterable[Mention], *, today: date,
              provenance: str) -> str:
    lines = [f"_Stakeholder card, v1 — {provenance}, {today.isoformat()}._", ""]
    seen: set[str] = set()
    ctx: list[str] = []
    for m in mentions:
        t = " ".join(m.text.split())
        if not t or t in seen:
            continue
        seen.add(t)
        where = f"`{m.code}` {m.source}".strip() if m.code else m.source
        ctx.append(f"- {where}: {t}" if where else f"- {t}")
    if ctx:
        lines += ["**What the tenant recorded:**", "", *ctx]
    return with_details("\n".join(lines).rstrip() + "\n", details)


@dataclass
class CardWrite:
    action: str                      # "created" | "updated" | "matched" | "skipped"
    name: str
    est_item_id: str = ""
    project_code: str = ""
    scope: str = "project"
    reason: str = ""
    rows: list[dict] = field(default_factory=list)


def _unique_eid(base: str, taken: set[str]) -> str:
    eid, n = base, 2
    while eid in taken:
        eid, n = f"{base}-{n}", n + 1
    return eid


def create_card(client: Any, *, project_id: str, project_code: str,
                details: Details, body: str, today: date,
                scope: str = "project", company_id: str | None = None,
                taken_ids: set[str] | None = None, dry_run: bool = False,
                step_title: str | None = None) -> CardWrite:
    """Insert v1 of a Stakeholders card (+ its "Created" step) through the
    engine's element-create builder. Account scope sets the ``scope`` /
    ``company_id`` pair together (they are read as one, #198)."""
    from cp_engine.authored_element import build_create_rows, slugify
    from cp_engine.mc2_db import Tables

    framing = framing_for(details.name, details.role)
    (row,) = build_create_rows(
        project_id=project_id, project_code=project_code, label=framing,
        type_=LAYER, body=body, serves=[], now_iso=today.isoformat(),
    )
    slug = slugify(framing_for(details.name, _clip(details.role, 40)))[:80].rstrip("-")
    if taken_ids is None and not dry_run:
        held = (client.table(Tables.SPINE_SUBSTANCE).select("est_item_id")
                .eq("project_id", project_id).execute().data) or []
        taken_ids = {str(r.get("est_item_id")) for r in held}
    eid = _unique_eid(f"_authored/{slug}", taken_ids or set())
    row["est_item_id"] = eid
    row["id"] = f"{project_code}/{eid}/v1"
    if scope == "account":
        row["scope"], row["company_id"] = "account", company_id
    if taken_ids is not None:
        taken_ids.add(eid)
    write = CardWrite(action="created", name=details.name, est_item_id=eid,
                      project_code=project_code, scope=scope, rows=[row])
    if dry_run:
        return write
    client.table(Tables.SPINE_SUBSTANCE).insert([row]).execute()
    try:
        client.table(Tables.SPINE_STEPS).insert([{
            "project_id": project_id, "est_item_id": eid, "position": 1,
            "title": step_title or f"Created {framing}", "status": "done",
            "step_date": today.isoformat(), "note": None,
        }]).execute()
    except Exception as exc:  # noqa: BLE001 — the card landed; the trail did not
        log.warning("stakeholder card %s created without its step: %s", eid, exc)
    return write


def update_card_details(client: Any, card: Card, details: Details, *, today: date,
                        note: str, dry_run: bool = False) -> CardWrite:
    """Add a version whose body carries ``details`` merged into the card's
    block (blanks filled, aliases unioned); a no-op when nothing changes."""
    from cp_engine.authored_element import build_version_rows
    from cp_engine.mc2_db import Tables

    merged = merge_details(card.details, details)
    if card.details == merged:
        return CardWrite(action="matched", name=card.name, est_item_id=card.est_item_id,
                         project_code=card.project_code, scope=card.scope)
    base = dict(card.row or {})
    if not base:
        raise ValueError(f"card {card.est_item_id} was not read from MC-2; cannot version it")
    (row,) = build_version_rows(
        project_id=base["project_id"], project_code=base["project_code"],
        est_item_id=card.est_item_id, prior_versions=[base],
        body=with_details(card.body, merged), version_note=note,
        now_iso=today.isoformat(),
    )
    row["scope"], row["company_id"] = base.get("scope") or "project", base.get("company_id")
    write = CardWrite(action="updated", name=merged.name or card.name,
                      est_item_id=card.est_item_id, project_code=card.project_code,
                      scope=card.scope, rows=[row], reason=note)
    if dry_run:
        return write
    client.table(Tables.SPINE_SUBSTANCE).insert([row]).execute()
    (client.table(Tables.SPINE_SUBSTANCE).update({"status": "superseded"})
     .eq("id", base["id"]).execute())
    return write


def promote_card(client: Any, card: Card, *, company_id: str, dry_run: bool = False) -> None:
    """Every version of ``card`` to account scope (the ``_promote_stakeholder``
    element-level move)."""
    from cp_engine.mc2_db import Tables

    if dry_run or card.scope == "account":
        return
    (client.table(Tables.SPINE_SUBSTANCE)
     .update({"scope": "account", "company_id": company_id})
     .eq("project_id", card.project_id).eq("est_item_id", card.est_item_id).execute())


def ensure_card(client: Any, mention: Mention, *, cards: list[Card], project_id: str,
                project_code: str, roster: Roster, today: date, company: str = "",
                aliases: Mapping[str, str] | None = None, provenance: str,
                dry_run: bool = False) -> CardWrite:
    """The one writer behind ingest and the sync import: ``mention`` becomes
    a card, or fills a matched card's details — never a second card for a
    person already known. Internal people and non-people are skipped.
    ``cards`` is updated in place with anything created."""
    name = apply_aliases(mention.name, aliases)
    if not is_person_name(name) or (company and norm_name(name) == norm_name(company)):
        return CardWrite(action="skipped", name=mention.name, reason="not a person")
    if roster.is_internal(name, mention.group):
        return CardWrite(action="skipped", name=name, reason="internal")
    found = match_card(cards, name, aliases=aliases)
    if found is not None and found.side == "internal":
        return CardWrite(action="skipped", name=name, reason="internal")
    if found is None and roster.is_internal_first_name(name):
        # A bare first name no card claims, that an internal person carries
        # ("Geoff", "Louise") — the team, not a new client stakeholder.
        return CardWrite(action="skipped", name=name, reason="internal (first name)")
    extra_aliases = tuple(a for a in (mention.name, *mention.aliases)
                          if norm_name(a) != norm_name(found.name if found else name))
    details = Details(name=name, role=_clip(mention.role, 160), company=company,
                      email=mention.email, side=side_for(mention),
                      aliases=extra_aliases)
    if found is not None:
        if found.row is None:  # read from the disk view: nothing to version
            return CardWrite(action="matched", name=found.name, est_item_id=found.est_item_id,
                             project_code=found.project_code, scope=found.scope)
        # Fill the card's blanks (never overwrite) and record the spelling
        # this mention used, so the variant resolves to this card from now on.
        return update_card_details(client, found, replace(details, name=found.name),
                                   today=today, note=f"Details from {provenance}",
                                   dry_run=dry_run)
    body = card_body(details, [mention], today=today, provenance=provenance)
    write = create_card(client, project_id=project_id, project_code=project_code,
                        details=details, body=body, today=today, dry_run=dry_run)
    cards.append(Card(est_item_id=write.est_item_id, project_code=project_code,
                      framing=framing_for(details.name, details.role), body=body,
                      project_id=project_id, row=write.rows[0] if write.rows else None))
    return write


# ──────────────────────────────────────────────────────────────────────
#  The strip
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StripEntry:
    name: str
    role: str = ""
    company: str = ""
    email: str = ""
    via: str = ""        # "" (this workstream), "account", or an ancestor code


def strip_entries(cards: Iterable[Card], *, own_project_id: str | None,
                  chain_codes: Mapping[str, str] | None = None) -> tuple[StripEntry, ...]:
    """The stakeholders-strip rows: every person card that applies, internal
    people left out (they are the team, not stakeholders — the old strip
    never listed them either). One row per person."""
    out: list[StripEntry] = []
    seen: set[str] = set()
    for c in cards:
        if not c.is_person or c.side == "internal":
            continue
        key = norm_name(c.name)
        if key in seen:
            continue
        seen.add(key)
        d = c.details or Details()
        if c.scope == "account":
            via = "account"
        elif own_project_id is not None and c.project_id != own_project_id:
            via = (chain_codes or {}).get(str(c.project_id), c.project_code)
        else:
            via = ""
        out.append(StripEntry(name=c.name, role=_clip(c.role, 120), company=d.company,
                              email=d.email, via=via))
    return tuple(out)


# ──────────────────────────────────────────────────────────────────────
#  Retiring the markdown stores (sync)
# ──────────────────────────────────────────────────────────────────────

#: Quarantine region labels (``exceptions/region-edits/*--<label>--*.md``).
CPMD_QUARANTINE = "stakeholders-section"
SPRINT_QUARANTINE = "sprint-stakeholders"
_RETIRED_NOTE = (
    "(section retired — stakeholders live on spine Stakeholders cards in "
    "MC-2; the cp.md `stakeholders-strip` is the generated view)"
)


@dataclass
class RetireResult:
    path: Path
    writes: list[CardWrite] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    removed: bool = False
    quarantined: str | None = None
    non_person_text: bool = False


def retire_markdown_store(
    client: Any,
    *,
    path: Path,
    kind: str,                       # "cpmd" | "sprint"
    code: str,
    project_id: str,
    cards: list[Card],
    roster: Roster,
    today: date,
    company: str = "",
    aliases: Mapping[str, str] | None = None,
    writer: str = "cxp sync",
    week: str = "",
    tenant_root: Path | None = None,
) -> RetireResult:
    """Import the people a person typed into a retired store, then remove it.

    1. Each entry becomes a card or fills a matched card (``ensure_card``).
    2. Only when EVERY entry landed (or was skipped as internal / not a
       person) is the section's text quarantined to
       ``exceptions/region-edits/`` and the section removed — a failed
       import keeps the section where it is, so nothing is ever lost.
    A section holding only its placeholder is removed without a quarantine
    file (there is nothing to preserve)."""
    from cp_engine import region_guard

    res = RetireResult(path=path)
    body = path.read_text(encoding="utf-8")
    span = cpmd_section_span(body) if kind == "cpmd" else sprint_subsection_span(body)
    if span is None:
        return res
    section = body[span[0]:span[1]]
    mentions = (parse_cpmd_section(body, code=code) if kind == "cpmd"
                else parse_sprint_subsection(body, code=code, week=week))
    provenance = ("imported from cp.md `## Stakeholders`" if kind == "cpmd"
                  else f"imported from sprint `### Stakeholders` {week}".rstrip())
    for m in mentions:
        try:
            res.writes.append(ensure_card(
                client, m, cards=cards, project_id=project_id, project_code=code,
                roster=roster, today=today, company=company, aliases=aliases,
                provenance=provenance,
            ))
        except Exception as exc:  # noqa: BLE001 — keep the section, say why
            res.errors.append(f"{m.name}: {type(exc).__name__}: {exc}")
    if res.errors:
        log.warning(
            "stakeholders: %s kept its %s section — %d entry(ies) could not be "
            "imported to cards: %s", path, kind, len(res.errors), "; ".join(res.errors)[:300],
        )
        return res
    if has_content(section):
        root = tenant_root.resolve() if tenant_root else region_guard.find_tenant_root(path)
        if root is None:
            res.errors.append("no tenant root — cannot quarantine; section kept")
            log.warning("stakeholders: %s kept its section — no tenant root to "
                        "quarantine into", path)
            return res
        rel = str(path.resolve().relative_to(root))
        q = region_guard.quarantine(
            root=root, rel_path=rel,
            region=CPMD_QUARANTINE if kind == "cpmd" else SPRINT_QUARANTINE,
            discarded=section, replacement=_RETIRED_NOTE, writer=writer,
        )
        res.quarantined = str(q.relative_to(root))
        person_lines = {m.raw.splitlines()[0].strip() for m in mentions if m.raw}
        res.non_person_text = any(
            ln.strip() and not ln.strip().startswith(("_<", "<!--", "|", "**"))
            and ln.strip() not in person_lines and not ln.startswith("  ")
            for ln in section.splitlines()[1:]
        )
        created = sum(1 for w in res.writes if w.action in ("created", "updated"))
        log.warning(
            "stakeholders: retired %s's %s section (%d entr%s, %d card write%s); "
            "its text is preserved in %s.%s", rel, kind, len(mentions),
            "y" if len(mentions) == 1 else "ies", created, "" if created == 1 else "s",
            res.quarantined,
            " It held text that is not a person entry — move what still matters."
            if res.non_person_text else "",
        )
    path.write_text(remove_span(body, span), encoding="utf-8")
    res.removed = True
    return res
