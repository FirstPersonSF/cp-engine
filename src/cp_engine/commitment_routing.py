"""Which workstream does a meeting commitment belong to? (#358)

Every Fathom action item from a meeting is filed on the workstream the
meeting was tagged to. The #114 screen (`webhook/commitments_propose.
screen_off_project`) flags an item only when it names another project's
code, its whole name, or a company with exactly ONE active workstream. Real
mis-filings name none of those: "Fix Euralift page" (a Teleflex product),
"Log into SalesLoft portal" (Salesloft has a job AND an account node),
"Email Andres (Thoropass)" (the SOC 2 vendor), "Schedule mtg w/ Healy
(Navon)" (a StoryOS prospect, filed on SOC 2).

What names a workstream, besides its code, is its VOCABULARY — the people,
products and vendors that recur in its own record. `RoutingIndex` learns it
deterministically (no model call): each workstream's documents (commitment
rows from MC-2, plus `cp.md` / `meeting-history.md` lines when the tree is
on disk) are counted per term, and a term is a MARKER of a workstream when
it recurs there and rarely anywhere else. Scoring an item is then a sum of
identity hits and marker hits, in two levels:

1. **Company.** Another company wins only on company-level evidence — its
   name or code, or its markers — and only when it out-scores the tagged
   company. A generic word in a project NAME ("website", "campaign") never
   establishes another company, so "update the website" on SOC 2 stays put.
   Inside the winning company the best-scoring workstream is the target; a
   tie goes to the company's only job, then its account node, then its only
   root; otherwise nothing is proposed.
2. **Same company.** A sibling workstream wins when it scores at least 2
   and strictly more than the tagged one (SOC 2 vs StoryOS).

The tagged workstream's own vocabulary is the defence against a passing
mention: "Schedule Canonic demo w/ Craig & Fiona" names Canonic, but Craig
and Fiona are SAP's, so the SAP job out-scores and keeps it.

Posture is propose-only, as for #88 and #114: the item still lands on the
tagged workstream with an `[off-project? → <code>]` suffix, and
`route_commitment` (hosted) is the only path that moves it.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cp_engine.ingest_routing import _distinctive, identity_tokens, same_project
from cp_engine.text_similarity import content_tokens

# The stored flag. `off-project?` is what ingest writes (#114) and what
# `route_commitment` strips; `cross-project?` is the sprint-file spelling
# (#88) that has reached a commitment row by hand at least once.
FLAG_RE = re.compile(r"\s*\[(?:off|cross)-project\?\s*→\s*([^\]\s]+)\s*\]", re.IGNORECASE)
_ANNOTATION_RE = re.compile(
    r"\[(?:owner unresolved|confidence|routed from|off-project\?|cross-project\?)[^\]]*\]"
    r"|\bspeaker\s*#?\s*\d+\b",
    re.IGNORECASE,
)

# A term marks a workstream when it appears in at least MIN_DOCS of its
# documents and at least SHARE of all documents carrying it are its own.
# STRONG terms (a product, a vendor, a client's people) count double.
# Tuned 2026-10-21 against every open commitment in MC-2 (207 rows) with
# the tree on disk: at 0.75 / 0.9 SAP's own people (Fiona 0.87, Michelle
# 0.70) lost to a passing "Canonic" and Euralift (0.78 — the mis-filed row
# echoed into ibx-5153's record) never counted.
MIN_DOCS = 2
SHARE = 0.66
STRONG_DOCS = 4
STRONG_SHARE = 0.85
# A company or project NAME word counts in full only when the record that
# owns it carries most of its mentions; "Canonic" turns up across nearly
# every workstream's record (it is the firm's own product), so a passing
# "Canonic demo" on an SAP job is not evidence of Canonic's work.
IDENT_SHARE = 0.6
# The tagged workstream is the prior: its own record is scored on looser
# bars, so an item that reads as ITS work (SAP's Fiona and Michelle) keeps
# it against a passing mention of someone else (a "Canonic trial").
OWN_SHARE = 0.5
OWN_STRONG_SHARE = 0.75
OWN_STRONG_DOCS = 2  # Latoya: three docs, all Salesloft's
# The minimum score that moves anything.
MIN_SCORE = 2

# Work verbs and nouns every workstream shares; months and weekdays. They
# rarely clear SHARE anyway — this keeps a thin corpus from minting them.
_GENERIC = frozenset(
    [
        "email",
        "send",
        "sent",
        "schedule",
        "review",
        "draft",
        "share",
        "follow",
        "call",
        "meeting",
        "deck",
        "update",
        "confirm",
        "request",
        "finalize",
        "build",
        "create",
        "check",
        "ask",
        "provide",
        "prepare",
        "week",
        "today",
        "tomorrow",
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "weekend",
        "january",
        "february",
        "march",
        "april",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "eod",
        "feedback",
        "slide",
        "slides",
        "doc",
        "document",
        "note",
        "notes",
        "link",
        "file",
        "version",
        "plan",
    ]
)


def flag_target(description: str) -> str | None:
    """The code an `[off-project? → code]` (or `[cross-project? → code]`)
    marker on a stored description points at, or None."""
    m = FLAG_RE.search(description or "")
    return m.group(1).strip().lower() if m else None


def strip_flags(description: str) -> str:
    return FLAG_RE.sub("", description or "").strip()


def _fold(text: str) -> str:
    """Accents off (`Andrés` → `Andres`): `content_tokens` is ASCII-only.
    Possessives off too (`Latoya's` → `Latoya`): a name is one term."""
    text = "".join(
        ch for ch in unicodedata.normalize("NFKD", text or "") if not unicodedata.combining(ch)
    )
    return re.sub(r"['\u2019]s\b", "", text)


def _terms(text: str) -> frozenset[str]:
    text = _ANNOTATION_RE.sub(" ", _fold(text))
    return frozenset(
        t
        for t in content_tokens(text)
        if len(t) >= 3 and t not in _GENERIC and not t.replace(".", "").isdigit()
    )


def _proper_terms(text: str) -> frozenset[str]:
    """Terms the item writes as a proper noun — capitalised, not opening a
    clause (`Euralift`, `Thoropass`, `Leah`; not the `Revise` that starts an
    item). Only these can be STRONG markers: a vendor, a product or a person
    names a workstream; a habit word (`revise`) only sounds like one."""
    text = _ANNOTATION_RE.sub(" ", _fold(text))
    out: set[str] = set()
    for m in re.finditer(r"[A-Za-z][\w'.-]*", text):
        word = m.group(0)
        if not word[0].isupper():
            continue
        before = text[: m.start()].rstrip()
        opens_clause = not before or before[-1] in ".;:!?"
        inner_caps = len(word) > 1 and word[1:].lower() != word[1:]  # SalesLoft, MCP
        if opens_clause and not inner_caps:
            continue
        out |= content_tokens(word)
    return frozenset(out)


def _number(code: str) -> str | None:
    m = re.search(r"\b[a-z0-9]{2,4}-(\d{3,5})\b", (code or "").lower())
    return m.group(1) if m else None


@dataclass(frozen=True)
class RouteSuggestion:
    target_code: str
    reason: str  # "code" | "company" | "workstream"
    score: int
    tagged_score: int
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Ws:
    code: str
    company: str
    label: str
    parent: str | None


class RoutingIndex:
    """Roster + per-workstream term counts. Build once per run; `suggest`
    is pure."""

    def __init__(self, roster: Iterable[Any]):
        self._ws: dict[str, _Ws] = {}
        names: dict[str, str] = {}
        company_words: dict[str, set[str]] = {}
        for p in roster or ():
            code = (getattr(p, "code", "") or "").strip().lower()
            if not code:
                continue
            company = (getattr(p, "company_name", "") or "").strip().lower()
            co_code = (getattr(p, "company_code", "") or code.split("-")[0]).strip().lower()
            self._ws[code] = _Ws(
                code=code,
                company=company or co_code,
                label=(getattr(p, "label", "") or "").lower(),
                parent=(getattr(p, "parent_code", None) or None),
            )
            names[code] = getattr(p, "name", "") or ""
            words = company_words.setdefault(company or co_code, set())
            words |= set(content_tokens(_fold(company))) | {co_code}
        # Company identity: its name words and code, distinctive across
        # companies (two companies sharing a word name neither).
        self._co_ident = _distinctive(
            {co: frozenset(w for w in ws if len(w) >= 3) for co, ws in company_words.items()}
        )
        # Workstream identity: name and slug words minus the company's own
        # words, the code prefix and the number (a code mention is handled
        # whole), distinctive across the roster.
        raw: dict[str, frozenset[str]] = {}
        for code, ws in self._ws.items():
            num = _number(code)
            toks = identity_tokens(code, name=_fold(names[code]))
            drop = company_words.get(ws.company, set()) | {num, code.split("-")[0]}
            raw[code] = frozenset(t for t in toks if t not in drop and len(t) >= 4)
        self._ws_ident = _distinctive(raw)
        self._counts: dict[str, Counter] = {}  # term -> Counter(code -> docs)

    # ── corpus ────────────────────────────────────────────────────────────

    def resolve(self, code: str) -> str | None:
        """The roster code for any spelling (`ibx-5153`, the full slug)."""
        c = (code or "").strip().lower()
        if c in self._ws:
            return c
        return next((k for k in self._ws if same_project(k, c)), None)

    def add_document(self, code: str, text: str) -> bool:
        owner = self.resolve(code)
        if owner is None or not text:
            return False
        for t in _terms(text):
            self._counts.setdefault(t, Counter())[owner] += 1
        return True

    @property
    def documents_indexed(self) -> int:
        return sum(sum(c.values()) for c in self._counts.values())

    # ── scoring ───────────────────────────────────────────────────────────

    def _share(self, term: str, keys: set[str], minus: str | None) -> tuple[int, float]:
        """(docs of `keys` carrying `term`, their share of all such docs),
        less the row's own document when backfilling (`minus`)."""
        counts = self._counts.get(term)
        if not counts:
            return 0, 0.0
        total = sum(counts.values()) - (1 if minus and counts.get(minus) else 0)
        mine = sum(counts.get(k, 0) for k in keys)
        if minus in keys and counts.get(minus):
            mine -= 1
        return mine, (mine / total if total > 0 else 0.0)

    def _weight(self, term: str, keys: set[str], minus: str | None, own: bool = False) -> int:
        mine, share = self._share(term, keys, minus)
        if mine < MIN_DOCS:
            return 0
        if share >= (OWN_STRONG_SHARE if own else STRONG_SHARE) and mine >= (
            OWN_STRONG_DOCS if own else STRONG_DOCS
        ):
            return 2
        return 1 if share >= (OWN_SHARE if own else SHARE) else 0

    def _score(
        self,
        keys: set[str],
        ident: frozenset[str],
        ident_w: int,
        terms,
        minus,
        own: bool = False,
        proper: frozenset[str] = frozenset(),
    ) -> tuple[int, list[str], bool]:
        """(score, evidence, strong). STRONG evidence is what may move a row
        at all: an identity word the record actually owns (a name the whole
        tenant mentions — Canonic — counts once and is not strong), or a
        strong marker. Weak markers only add to a score."""
        score, ev, strong = 0, [], False
        for t in sorted(terms & ident):
            mine, share = self._share(t, keys, minus)
            owned = not self._counts.get(t) or share >= (OWN_SHARE if own else IDENT_SHARE)
            score += ident_w if owned else 1
            strong |= owned
            ev.append(t)
        for t in sorted(terms - ident):
            w = self._weight(t, keys, minus, own)
            if w == 2 and not own and t not in proper:
                w = 1
            if w:
                score += w
                strong |= w == 2
                ev.append(t)
        return score, ev, strong

    def _company_score(
        self, co: str, terms, minus, own: bool = False, proper=frozenset()
    ) -> tuple[int, list[str], bool]:
        keys = {c for c, w in self._ws.items() if w.company == co}
        return self._score(keys, self._co_ident.get(co, frozenset()), 2, terms, minus, own, proper)

    def _ws_score(
        self,
        code: str,
        terms,
        minus,
        nums=frozenset(),
        own: bool = False,
        proper=frozenset(),
    ) -> tuple[int, list[str], bool]:
        score, ev, strong = self._score(
            {code}, self._ws_ident.get(code, frozenset()), 1, terms, minus, own, proper
        )
        num = _number(code)
        if num and num in nums:  # "Google 5173" names the job
            score, strong = score + 2, True
            ev.append(num)
        return score, ev, strong

    def _company_target(self, co: str, terms, minus, nums, proper) -> str | None:
        members = [c for c, w in self._ws.items() if w.company == co]
        scored = {c: self._ws_score(c, terms, minus, nums, proper=proper)[0] for c in members}
        top = max(scored.values(), default=0)
        leaders = [c for c in members if scored[c] == top]
        if top > 0 and len(leaders) == 1:
            return leaders[0]
        # Leaves only: an account or a parent is not where a task lives.
        jobs = [
            c
            for c in members
            if self._ws[c].label != "account" and not any(self._ws[o].parent == c for o in members)
        ]
        if len(jobs) == 1:
            return jobs[0]
        accounts = [c for c in members if self._ws[c].label == "account"]
        if len(accounts) == 1:
            return accounts[0]
        roots = [c for c in members if not self._ws[c].parent]
        return roots[0] if len(roots) == 1 else None

    def suggest(
        self,
        text: str,
        tagged: str | Sequence[str],
        *,
        in_corpus: bool = False,
    ) -> RouteSuggestion | None:
        """The workstream `text` belongs to when that is NOT the tagged one,
        else None. `tagged` may list every co-tagged code; the first is the
        one the row is filed on. `in_corpus=True` (backfill) discounts the
        row's own document from the tagged workstream's counts."""
        tags = [tagged] if isinstance(tagged, str) else list(tagged)
        own = {r for r in (self.resolve(t) for t in tags) if r}
        anchor = self.resolve(tags[0]) if tags else None
        if not text or not self._ws:
            return None
        minus = anchor if in_corpus else None

        # 1 — an explicit code (`ggl-5168`, a full slug) is decisive.
        folded = _fold(text).lower()
        for m in re.finditer(r"\b([a-z0-9]{2,4}-\d{3,5})(?:-[a-z0-9-]+)?\b", folded):
            hit = self.resolve(m.group(0)) or self.resolve(m.group(1))
            if hit and hit not in own:
                return RouteSuggestion(hit, "code", 99, 0, (m.group(0),))

        terms = _terms(text)
        if not terms:
            return None
        nums = frozenset(re.findall(r"(?<![$\d,.])\b(\d{4})\b(?![\d,.%])", folded))
        proper = _proper_terms(text) & terms
        own_cos = {self._ws[c].company for c in own}
        anchor_co = self._ws[anchor].company if anchor else None

        # 2 — another company, on company-level STRONG evidence only.
        own_best = max(
            (self._company_score(c, terms, minus, own=True)[0] for c in own_cos), default=0
        )
        co_scores = {
            co: self._company_score(co, terms, minus, proper=proper)
            for co in {w.company for w in self._ws.values()} - own_cos
        }
        foreign = sorted(
            ((sc, co) for co, (sc, _, strong) in co_scores.items() if strong),
            reverse=True,
        )
        if foreign:
            best_s, best_co = foreign[0]
            unique = len(foreign) == 1 or foreign[1][0] < best_s
            if best_s >= MIN_SCORE and best_s > own_best and unique:
                target = self._company_target(best_co, terms, minus, nums, proper)
                if target:
                    return RouteSuggestion(
                        target,
                        "company",
                        best_s,
                        own_best,
                        tuple(co_scores[best_co][1]),
                    )
                return None

        # 3 — a sibling workstream in the tagged company.
        if anchor_co is None:
            return None
        anchor_s = max((self._ws_score(c, terms, minus, nums, own=True)[0] for c in own), default=0)
        sibs = sorted(
            (
                (self._ws_score(c, terms, minus, nums, proper=proper), c)
                for c, w in self._ws.items()
                if w.company == anchor_co and c not in own
            ),
            key=lambda x: -x[0][0],
        )
        if not sibs:
            return None
        (best_s, ev, strong), best_c = sibs[0]
        unique = len(sibs) == 1 or sibs[1][0][0] < best_s
        if best_s >= MIN_SCORE and best_s > anchor_s and unique and strong:
            return RouteSuggestion(best_c, "workstream", best_s, anchor_s, tuple(ev))
        return None


# ── building the index from MC-2 and the tree ──────────────────────────────

_MAX_ROWS = 5000
_MAX_LINES_PER_FILE = 1500
_TREE_FILES = ("cp.md", "meeting-history.md")
_MANAGED_RE = re.compile(
    r"<!-- cp-engine:start (?P<n>[\w-]+) -->.*?<!-- cp-engine:end (?P=n) -->", re.S
)


def load_commitment_documents(client: Any, limit: int = _MAX_ROWS) -> list[tuple[str, str]]:
    """`(canonical code, description)` for recent commitment rows. A row
    closed as `routed`, or still carrying a routing flag, is a known
    mis-filing and teaches nothing about where it sits. Explicit columns."""
    from cp_engine.asks import canonical_code
    from cp_engine.mc2_db import OWNER_COLUMN, Tables

    projects = client.table(Tables.PROJECTS).select("id, full_job_name").execute().data or []
    code_of = {
        p["id"]: canonical_code(p.get("full_job_name"))
        for p in projects
        if p.get("id") and p.get("full_job_name")
    }
    rows = (
        client.table(Tables.COMMITMENTS)
        .select(f"description, status, {OWNER_COLUMN}")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data
    ) or []
    out: list[tuple[str, str]] = []
    for r in rows:
        desc = r.get("description") or ""
        code = code_of.get(r.get(OWNER_COLUMN))
        if not code or not desc or r.get("status") == "routed" or flag_target(desc):
            continue
        out.append((code, desc))
    return out


def load_tree_documents(tenant_root: Path, codes: Iterable[str]) -> list[tuple[str, str]]:
    """`(code, line)` from each workstream's `cp.md` and `meeting-history.md`
    — the newest lines, bounded per file.

    A line found under MORE than one workstream is dropped: sprint-planning
    and account meetings are copied into every workstream they touch, so
    on 2026-10-21 Google's nine records mentioned "Infoblox" more often than
    Infoblox's own. A copied line is cross-cutting and votes for nobody."""
    from cp_engine.plan_from_transcript import _find_project_dir

    seen: dict[str, set[str]] = {}
    found: list[tuple[str, str, str]] = []
    for code in codes:
        d = _find_project_dir(Path(tenant_root), code)
        if d is None:
            continue
        for name in _TREE_FILES:
            f = d / name
            if not f.is_file():
                continue
            try:
                body = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Managed regions re-render MC-2 rows (open asks): counting them
            # would count each commitment twice, mis-filings included.
            lines = _MANAGED_RE.sub("", body).splitlines()
            for line in lines[-_MAX_LINES_PER_FILE:]:
                line = re.sub(r"\(https?://[^)]*\)|<!--.*?-->", " ", line).strip()
                if len(line) < 20:
                    continue
                key = " ".join(sorted(_terms(line)))
                if not key:
                    continue
                seen.setdefault(key, set()).add(code)
                found.append((code, key, line))
    return [(code, line) for code, key, line in found if len(seen[key]) == 1]


def build_routing_index(
    roster: Iterable[Any] | None,
    *,
    client: Any = None,
    tenant_root: Path | None = None,
) -> RoutingIndex | None:
    """Roster + MC-2 commitment rows + (when on disk) the tree's own record.
    None without a roster. Raises on a read failure — callers that must not
    fail wrap it."""
    roster = list(roster or [])
    if not roster:
        return None
    index = RoutingIndex(roster)
    if client is not None:
        for code, text in load_commitment_documents(client):
            index.add_document(code, text)
    if tenant_root is not None:
        for code, text in load_tree_documents(tenant_root, [p.code for p in roster]):
            index.add_document(code, text)
    return index


def score_open_commitments(
    index: RoutingIndex, groups: dict[str, list], canonical: dict[str, str] | None = None
) -> list[dict]:
    """Backfill report (read-only): open rows that look filed on the wrong
    workstream and carry no flag yet. `groups` is `commitments_sweep.sweep`
    output keyed by the row's `projects.code`; `canonical` maps that to the
    roster slug (`IBX-ai-campaign` → `ibx-5153-ai-campaign`). Writes nothing."""
    canonical = canonical or {}
    out: list[dict] = []
    for code, rows in groups.items():
        tagged = canonical.get(code, code)
        if index.resolve(tagged) is None:
            continue
        for r in rows:
            if flag_target(r.description):
                continue
            s = index.suggest(r.description, tagged, in_corpus=True)
            if s is None:
                continue
            out.append(
                {
                    "project_code": code,
                    "key": r.id,
                    "target_code": s.target_code,
                    "description": r.description,
                    "reason": s.reason,
                    "evidence": list(s.evidence),
                }
            )
    return out
