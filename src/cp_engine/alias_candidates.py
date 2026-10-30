"""Propose new term and name aliases from ingest, for a person to review (#387).

WHY. `[terms] aliases` (#371) and `[names] aliases` (#312) fix mis-hearings
only once someone notices one and types it into `.cp-engine.toml`. The first
entries (SAP's Joule heard as "Juul" / "Jool"; Geoff Ahmann as "Jeff Allman")
were found by reading commitments that were already wrong. This module finds
the next ones at ingest, while the meeting is fresh, and puts them in a queue.

WHAT IT DOES. Capitalised words and short capitalised phrases in the text an
ingest reads (the transcript, Fathom's summary and action items) are compared
against the tenant's KNOWN terms: project and company names, stakeholder card
names and their aliases' targets, `[team] members`, the targets of existing
aliases, and `[terms] known` in `.cp-engine.toml`. A word that is NOT known but
is a near-miss of one that is (same sound and a small spelling distance) is a
candidate: "Juul" → Joule, "Jeff Allman" → Geoff Ahmann.

WHAT IT NEVER DOES. It never edits config and never rewrites text. Candidates
are appended to `alias-candidates.md` at the tenant root; `cxp aliases` lists
them, and only `cxp aliases --accept` writes a pair into `.cp-engine.toml`.
A rejected pair is recorded in the same file and is not proposed again.

PRECISION OVER RECALL. A wrong candidate costs a person's attention every
week, so the rules are deliberately narrow:
- single words are compared only against single-word TERMS (products,
  companies, project-name words), never against people's first names: two
  real people called Tara and Sara are not a mis-hearing;
- people are matched only as whole "First Last" phrases, and the first
  names must sound alike;
- a word is skipped when it is known, is a common English word or first
  name, is all capitals, is shorter than four letters, also appears in
  lowercase in the same text (an ordinary word at a sentence start), or is a
  plural / possessive of a known term;
- a pair already accepted, rejected or present as an alias key is skipped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

FILENAME = "alias-candidates.md"

Kind = Literal["term", "name"]

_MIN_LEN = 4
_CONTEXT_CHARS = 45
_MAX_PER_MEETING = 20

_SPEAKER_LINE_RE = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s+-\s+")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’]*")

# Common English words that turn up capitalised (sentence starts, titles,
# meeting jargon). The lowercase-seen rule catches most others per text.
_COMMON_WORDS = frozenset("""
about above after again against agenda also although always among another
anyway april around august back base because been before being below best
better between both bring build call came cannot change check client close
come could course daily date days deck december demo design does doing done
down during each early either else email even ever every everyone everything
fair feel february final first follow friday from front give going good great
guess half hand happy have having hello here high hold home hope however idea
into issue itself january july june just keep kind know last later least left
less like little long look made make many march maybe mean meeting monday more
most much must need never next nice none note nothing november october okay
once only open other otherwise over part people perfect plan please point
pretty probably quick quite rather ready real really right said same saturday
says second september should show side since slide some something sometimes
sorry sounds start still such sunday sure take team tell than thank thanks
that their them then there these they thing things think this those though
three through thursday time today together tomorrow totally tuesday under
until upon very wait want week well were what whatever when where whether
which while whole will with within without wednesday work would yeah year
years over yesterday your yours absolutely actually agree alright anything
basically before brief cool correct definitely exactly hmm hold honestly
interesting literally obviously oh right so super though um uh another awesome
brand budget campaign channel content copy draft edit feedback launch market
message narrative phase product project review round scope sprint story
strategy update video videos website person
""".split())  # noqa: SIM905

# Common first names. A single capitalised first name is a person, not a
# mis-heard product ("Joel" is not "Joule"); people are matched as phrases.
_COMMON_FIRST_NAMES = frozenset("""
aaron adam alan albert alex alice amanda amy andrew andy angela anna anne
anthony ben benjamin beth bill bob brad brian bruce carl carol caroline
catherine charles charlie chris christina christine claire craig dan daniel
dave david dean deborah dennis diana diane donna doug douglas drew ed edward
elizabeth emily emma eric erin frank gary george grace greg hannah harry
heather helen henry ian jack jacob james jamie jane janet jason jeff jeffrey
jennifer jenny jeremy jerry jess jessica jill jim joe joel john jon jonathan
jordan jose joseph josh joshua jules julia julie justin karen kate katherine
kathy keith kelly kevin kim kyle laura lauren leah linda lisa liz louis lucy
luke maria mark martin mary matt matthew megan melissa michael michelle mike
morgan nancy natalie nathan neil nicholas nick nicole noah olivia pam patrick
paul peter phil rachel ray rebecca richard rick rob robert robin ron rose ryan
sam samantha sandra sara sarah scott sean sharon simon sophie stephen steve
steven sue susan tara ted thomas tim timothy tina todd tom tony tyler victoria
will william zach
""".split())  # noqa: SIM905

_SUFFIXES = ("s", "es", "'s", "’s", "d", "ed", "ing", "r", "er", "ers", "y", "ly")


# ──────────────────────────────────────────────────────────────────────
#  Similarity
# ──────────────────────────────────────────────────────────────────────


def phonetic_key(word: str) -> str:
    """A coarse sound key: first letter kept, later vowels dropped, common
    spellings of one sound folded (soft g/c, ph, ck, gh, silent h), runs
    collapsed. "Juul", "Jool", "Joule" → "JL"; "Jeff", "Geoff" → "JF";
    "Tara" → "TR", "Sara" → "SR"."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return ""
    w = re.sub(r"^kn", "n", w)
    w = re.sub(r"^wr", "r", w)
    w = re.sub(r"^wh", "w", w)
    for a, b in (("ph", "f"), ("ck", "k"), ("dg", "j"), ("gh", ""), ("q", "k"),
                 ("x", "ks"), ("z", "s"), ("v", "f")):
        w = w.replace(a, b)
    w = re.sub(r"g(?=[eiy])", "j", w)
    w = re.sub(r"c(?=[eiy])", "s", w)
    w = w.replace("c", "k")
    # h is silent unless it starts a vowel sound.
    w = w[0] + re.sub(r"h(?![aeiou])", "", w[1:]) if len(w) > 1 else w
    first = "A" if w[0] in "aeiouy" else w[0].upper()
    rest = re.sub(r"[aeiouyhw]", "", w[1:]).upper()
    key = first + rest
    return re.sub(r"(.)\1+", r"\1", key)


def edit_distance(a: str, b: str) -> int:
    """Optimal-string-alignment distance (Levenshtein plus transposition)."""
    a, b = a.lower(), b.lower()
    if a == b:
        return 0
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def _inflection_of(a: str, b: str) -> bool:
    """"Activations" / "Activation", "Joule's" / "Joule": the same word."""
    a, b = a.lower(), b.lower()
    short, long_ = sorted((a, b), key=len)
    return any(long_ == short + s for s in _SUFFIXES) or (
        short.endswith("e") and long_ in (short + "d", short[:-1] + "ing")
    )


def word_sounds_like(heard: str, known: str) -> bool:
    """A single word heard for a known one: same sound key and at most two
    edits, or one edit in a long word ("Infobox" / "Infoblox")."""
    if heard.lower() == known.lower() or _inflection_of(heard, known):
        return False
    d = edit_distance(heard, known)
    if d <= 2 and phonetic_key(heard) == phonetic_key(known):
        return True
    return d == 1 and min(len(heard), len(known)) >= 6


def _word_in_phrase_close(heard: str, known: str) -> bool:
    """Looser, for the non-leading words of a whole-phrase match ("Allman"
    for "Ahmann"): same first sound and a 0.6 spelling ratio."""
    if word_sounds_like(heard, known):
        return True
    if min(len(heard), len(known)) < 3 or _inflection_of(heard, known):
        return False
    if phonetic_key(heard)[:1] != phonetic_key(known)[:1]:
        return False
    return SequenceMatcher(None, heard.lower(), known.lower()).ratio() >= 0.6


def phrase_sounds_like(heard: list[str], known: list[str]) -> bool:
    """A "First Last" heard for a known one: same word count, at least one
    word different, the first words equal or sounding alike, every other
    word equal or close."""
    if len(heard) != len(known) or len(heard) < 2:
        return False
    same = [h.lower() == k.lower() for h, k in zip(heard, known, strict=False)]
    if all(same):
        return False
    if not (same[0] or word_sounds_like(heard[0], known[0])):
        return False
    return all(s or _word_in_phrase_close(h, k)
               for s, h, k in zip(same[1:], heard[1:], known[1:], strict=False))


# ──────────────────────────────────────────────────────────────────────
#  Known terms
# ──────────────────────────────────────────────────────────────────────


@dataclass
class KnownTerms:
    """What the tenant already knows by name. ``terms`` are single- or
    multi-word products / companies / projects; ``people`` are person names.
    ``skip`` are heard forms never to propose (existing alias keys)."""

    terms: set[str] = field(default_factory=set)
    people: set[str] = field(default_factory=set)
    skip: set[str] = field(default_factory=set)
    #: Terms whose single words are match targets. A project name ("Vision
    #: Update 2026") is known as a whole, but its words are ordinary English
    #: and would turn "Fusion" into a candidate for "Vision".
    word_sources: set[str] = field(default_factory=set)

    def add_term(self, value: str | None, *, words: bool = True) -> None:
        value = " ".join((value or "").split())
        if value:
            self.terms.add(value)
            if words:
                self.word_sources.add(value)

    def add_person(self, value: str | None) -> None:
        value = " ".join((value or "").split())
        if value and len(value.split()) >= 2:
            self.people.add(value)
        elif value:
            self.terms.discard(value)

    def known_words(self) -> set[str]:
        out: set[str] = set()
        for v in (*self.terms, *self.people):
            out.update(w.lower() for w in _WORD_RE.findall(v))
        return out

    def single_word_terms(self) -> set[str]:
        out: set[str] = set()
        for v in self.word_sources:
            for w in _WORD_RE.findall(v):
                if (len(w) >= _MIN_LEN and w[0].isupper()
                        and w.lower() not in _COMMON_WORDS):
                    out.add(w)
        return out


def build_known_terms(
    *,
    config,
    roster: Iterable | None = None,
    cards: Iterable | None = None,
    speakers: Iterable[str] = (),
) -> KnownTerms:
    """Assemble the known list from what the engine already holds: the
    tenant config (`[terms] known`, alias targets, `[team] members`), the
    MC-2 roster (project and company names), stakeholder cards and the
    meeting's speaker labels."""
    known = KnownTerms()
    for t in getattr(config, "known_terms", ()) or ():
        known.add_term(t)
    for k, v in (getattr(config, "term_aliases", None) or {}).items():
        known.add_term(v)
        known.skip.add(k)
    for k, v in (getattr(config, "name_aliases", None) or {}).items():
        known.add_person(v) if len(v.split()) >= 2 else known.add_term(v, words=False)
        known.skip.add(k)
    for m in getattr(config, "team", ()) or ():
        known.add_person(m) if len(m.split()) >= 2 else known.terms.add(m.strip())
    for p in roster or ():
        known.add_term(getattr(p, "company_name", None))
        known.add_term(getattr(p, "name", None), words=False)
    for card in cards or ():
        name = getattr(card, "name", "") or ""
        if getattr(card, "is_person", True):
            known.add_person(name)
        else:
            known.add_term(name)
        for alias in getattr(card, "aliases", ()) or ():
            known.skip.add(alias)
    for s in speakers:
        known.add_person(s)
    return known


# ──────────────────────────────────────────────────────────────────────
#  Detection
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    heard: str
    target: str
    kind: Kind
    count: int
    context: str


def _lines(text: str) -> list[str]:
    return [ln for ln in (text or "").splitlines() if not _SPEAKER_LINE_RE.match(ln)]


def _runs(line: str) -> list[list[str]]:
    """Consecutive capitalised words separated by single spaces."""
    runs: list[list[str]] = []
    cur: list[str] = []
    last_end = -1
    for m in _WORD_RE.finditer(line):
        w = re.sub(r"['’]s?$", "", m.group())
        cap = w[:1].isupper()
        joined = cur and line[last_end:m.start()] == " "
        if cap and (not cur or joined):
            cur.append(w)
        else:
            if cur:
                runs.append(cur)
            cur = [w] if cap else []
        last_end = m.end()
    if cur:
        runs.append(cur)
    return runs


def _count(text: str, phrase: str) -> int:
    return len(re.findall(rf"(?<![\w]){re.escape(phrase)}(?![\w])", text))


def _context(text: str, phrase: str) -> str:
    m = re.search(rf"(?<![\w]){re.escape(phrase)}(?![\w])", text)
    if not m:
        return ""
    start = max(0, m.start() - _CONTEXT_CHARS)
    end = min(len(text), m.end() + _CONTEXT_CHARS)
    snippet = " ".join(text[start:end].split()).replace('"', "'").replace("·", "-")
    return ("…" if start else "") + snippet + ("…" if end < len(text) else "")


def find_candidates(text: str, known: KnownTerms) -> list[Candidate]:
    """Every near-miss of a known term or person in ``text``, most frequent
    first. Pure: reads nothing, writes nothing."""
    lines = _lines(text)
    body = "\n".join(lines)
    lower_words = {w for w in _WORD_RE.findall(body) if w[:1].islower()}
    known_words = known.known_words()
    known_lc = {t.lower() for t in (*known.terms, *known.people)}
    skip_lc = {s.lower() for s in known.skip}
    single_terms = known.single_word_terms()
    multi = [k.split() for k in (*known.people, *known.terms) if len(k.split()) in (2, 3)]
    people_lc = {p.lower() for p in known.people}

    found: dict[str, tuple[str, Kind]] = {}
    phrase_words: set[str] = set()

    runs = [r for ln in lines for r in _runs(ln)]
    # Whole phrases first ("Jeff Allman" → "Geoff Ahmann").
    for run in runs:
        for k in multi:
            w = len(k)
            for i in range(len(run) - w + 1):
                window = run[i:i + w]
                phrase = " ".join(window)
                if phrase.lower() in known_lc or phrase.lower() in skip_lc:
                    continue
                if phrase in found:
                    continue
                if phrase_sounds_like(window, k):
                    target = " ".join(k)
                    found[phrase] = (target, "name" if target.lower() in people_lc else "term")
                    phrase_words.update(x.lower() for x in window)

    # Then single words against single-word terms ("Juul" → "Joule").
    for run in runs:
        for word in run:
            lw = word.lower()
            if (word in found or len(word) < _MIN_LEN or word.isupper()
                    or word.rstrip("s").isupper()
                    or lw in known_words or lw in phrase_words or lw in skip_lc
                    or lw in _COMMON_WORDS or lw in _COMMON_FIRST_NAMES
                    or lw in lower_words):
                continue
            matches = sorted(
                (t for t in single_terms if word_sounds_like(word, t)),
                key=lambda t: (edit_distance(word, t), t),
            )
            if matches:
                found[word] = (matches[0], "term")

    out = [
        Candidate(heard=h, target=t, kind=kind, count=max(1, _count(body, h)),
                  context=_context(body, h))
        for h, (t, kind) in found.items()
    ]
    out.sort(key=lambda c: (-c.count, c.heard))
    return out[:_MAX_PER_MEETING]


# ──────────────────────────────────────────────────────────────────────
#  The review file
# ──────────────────────────────────────────────────────────────────────

_HEADER = """---
Project: {tenant}
Provenance: Version 01 | {today}
Filename: alias-candidates.md
Author: cp-engine
---

# Alias candidates

Words ingest heard that look like a mis-hearing of a known term or person.
Nothing here changes any text. Review with `cxp aliases`: `--accept` writes
the pair into `.cp-engine.toml`, `--reject` keeps it from coming back.

## Open

## Accepted

## Rejected
"""

# - 2026-10-30 · term · "Juul" → Joule · ×3 · sap-5198 · meeting 1a2b3c4d · …context…
_OPEN_RE = re.compile(
    r'^- (?P<date>\d{4}-\d{2}-\d{2}) · (?P<kind>term|name) · "(?P<heard>[^"]+)" → '
    r"(?P<target>.+?) · ×(?P<count>\d+) · (?P<code>\S+) · meeting (?P<meeting>\S+)"
    r"(?: · (?P<context>.*))?$"
)
# - 2026-10-30 · term · "Juul" → Joule (…anything…)
_DONE_RE = re.compile(
    r'^- (?P<date>\d{4}-\d{2}-\d{2}) · (?P<kind>term|name) · "(?P<heard>[^"]+)" → '
    r"(?P<target>.+?)(?: · .*)?$"
)
_SECTION_RE = re.compile(r"^##\s+(?P<name>\w+)")


@dataclass(frozen=True)
class OpenEntry:
    date: str
    kind: Kind
    heard: str
    target: str
    count: int
    code: str
    meeting: str
    context: str


@dataclass
class ReviewFile:
    lines: list[str]

    @classmethod
    def read(cls, tenant_root: Path) -> ReviewFile:
        p = Path(tenant_root) / FILENAME
        return cls(p.read_text(encoding="utf-8").splitlines() if p.exists() else [])

    def _sections(self) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        current = None
        for i, ln in enumerate(self.lines):
            m = _SECTION_RE.match(ln)
            if m:
                current = m.group("name").lower()
                out.setdefault(current, [])
            elif current is not None:
                out[current].append(i)
        return out

    def open_entries(self) -> list[OpenEntry]:
        out = []
        for i in self._sections().get("open", []):
            m = _OPEN_RE.match(self.lines[i])
            if m:
                out.append(OpenEntry(
                    date=m["date"], kind=m["kind"], heard=m["heard"],
                    target=m["target"].strip(), count=int(m["count"]),
                    code=m["code"], meeting=m["meeting"],
                    context=(m["context"] or "").strip(),
                ))
        return out

    def decided(self, section: str) -> set[tuple[str, str]]:
        out = set()
        for i in self._sections().get(section, []):
            m = _DONE_RE.match(self.lines[i])
            if m:
                out.add((m["heard"], m["target"].strip()))
        return out

    def _ensure(self, tenant: str, today: date) -> None:
        if not any(_SECTION_RE.match(ln) for ln in self.lines):
            self.lines = _HEADER.format(tenant=tenant, today=today.isoformat()).splitlines()
        names = {k for k in self._sections()}
        for name in ("open", "accepted", "rejected"):
            if name not in names:
                self.lines += ["", f"## {name.capitalize()}"]

    def _insert_at_end_of(self, section: str, new_lines: list[str]) -> None:
        heading = next(i for i, ln in enumerate(self.lines)
                       if (m := _SECTION_RE.match(ln)) and m["name"].lower() == section)
        end = next((j for j in range(heading + 1, len(self.lines))
                    if _SECTION_RE.match(self.lines[j])), len(self.lines))
        # Keep one blank line before the next heading.
        while end - 1 > heading and not self.lines[end - 1].strip():
            end -= 1
        if end == heading + 1:
            new_lines = [""] + new_lines
        self.lines[end:end] = new_lines

    def write(self, tenant_root: Path) -> None:
        (Path(tenant_root) / FILENAME).write_text("\n".join(self.lines).rstrip() + "\n",
                                                   encoding="utf-8")

    def text(self) -> str:
        return "\n".join(self.lines)


def record(
    tenant_root: Path,
    candidates: Iterable[Candidate],
    *,
    code: str,
    meeting_id: str,
    today: date,
    tenant: str = "cp tenant",
) -> int:
    """Append new candidates to ``## Open``. Idempotent per meeting: a pair
    already queued from this meeting, or already accepted / rejected, is
    not written again. Returns how many lines were added."""
    rf = ReviewFile.read(tenant_root)
    decided = rf.decided("accepted") | rf.decided("rejected")
    decided_heard = {h for h, _ in decided}
    meeting = (meeting_id or "-")[:8]
    queued = {(e.meeting, e.heard) for e in rf.open_entries()}
    new: list[str] = []
    for c in candidates:
        if (c.heard, c.target) in decided or c.heard in decided_heard:
            continue
        if (meeting, c.heard) in queued:
            continue
        queued.add((meeting, c.heard))
        line = (f'- {today.isoformat()} · {c.kind} · "{c.heard}" → {c.target} · '
                f"×{c.count} · {code} · meeting {meeting}")
        if c.context:
            line += f" · {c.context}"
        new.append(line)
    if not new:
        return 0
    rf._ensure(tenant, today)
    rf._insert_at_end_of("open", new)
    rf.write(tenant_root)
    return len(new)


# ──────────────────────────────────────────────────────────────────────
#  Review
# ──────────────────────────────────────────────────────────────────────


@dataclass
class Group:
    heard: str
    target: str
    kind: Kind
    count: int = 0
    meetings: set[str] = field(default_factory=set)
    codes: list[str] = field(default_factory=list)
    first: str = ""
    last: str = ""
    examples: list[str] = field(default_factory=list)


def grouped(tenant_root: Path) -> list[Group]:
    """Open candidates grouped by (heard, target), most-seen first."""
    groups: dict[tuple[str, str], Group] = {}
    for e in ReviewFile.read(tenant_root).open_entries():
        g = groups.setdefault((e.heard, e.target), Group(e.heard, e.target, e.kind))
        g.count += e.count
        g.meetings.add(e.meeting)
        if e.code not in g.codes:
            g.codes.append(e.code)
        g.first = min(g.first or e.date, e.date)
        g.last = max(g.last, e.date)
        if e.context and len(g.examples) < 2 and e.context not in g.examples:
            g.examples.append(e.context)
    return sorted(groups.values(), key=lambda g: (-len(g.meetings), -g.count, g.heard))


def render_review(groups: list[Group], *, heading: str = "## Alias candidates") -> list[str]:
    """Markdown lines for the weekly review (also the `cxp aliases` listing)."""
    if not groups:
        return []
    out = [f"{heading} ({len(groups)} open)", "",
           "_Possible mis-hearings from ingest. Accept or reject with "
           "`cxp aliases --accept \"<heard>\"` / `--reject \"<heard>\"`._", ""]
    for i, g in enumerate(groups, 1):
        meetings = len(g.meetings)
        out.append(
            f"{i}. **\"{g.heard}\" → {g.target}** ({g.kind}) · ×{g.count} in "
            f"{meetings} meeting{'s' if meetings != 1 else ''} · "
            f"{', '.join(g.codes)} · {g.first}"
            + (f" to {g.last}" if g.last != g.first else "")
        )
        for ex in g.examples:
            out.append(f"   - {ex}")
    out.append("")
    return out


class ReviewError(ValueError):
    """A review instruction that cannot be applied as asked."""


def _parse_choice(raw: str) -> tuple[str, str | None]:
    if "=" in raw:
        heard, target = raw.split("=", 1)
        return heard.strip(), target.strip() or None
    return raw.strip(), None


def review(
    tenant_root: Path,
    *,
    accept: Iterable[str] = (),
    reject: Iterable[str] = (),
    today: date,
) -> dict:
    """Apply accept / reject choices. ``accept`` entries are ``heard`` or
    ``heard=Target`` (a target other than the proposed one). Accepted pairs
    are written into `[terms.aliases]` or `[names.aliases]` in
    `.cp-engine.toml` (format-preserving); every Open line for a decided
    pair moves to ``## Accepted`` / ``## Rejected`` as one summary line.
    Validates everything before writing anything."""
    import tomlkit

    tenant_root = Path(tenant_root)
    groups = {g.heard: g for g in grouped(tenant_root)}
    plan: list[tuple[str, Group, str]] = []
    for verb, raws in (("accepted", accept), ("rejected", reject)):
        for raw in raws:
            heard, target = _parse_choice(raw)
            g = groups.get(heard)
            if g is None:
                open_list = ", ".join(f'"{h}"' for h in groups) or "none"
                raise ReviewError(f'no open candidate "{heard}" (open: {open_list})')
            if any(p[1] is g for p in plan):
                raise ReviewError(f'"{heard}" is both accepted and rejected')
            plan.append((verb, g, target or g.target))
    if not plan:
        return {"accepted": [], "rejected": []}

    cfg_path = tenant_root / ".cp-engine.toml"
    doc = tomlkit.parse(cfg_path.read_text(encoding="utf-8"))
    for verb, g, target in plan:
        if verb != "accepted":
            continue
        section = "names" if g.kind == "name" else "terms"
        if section == "names" and len(g.heard.split()) == 1:
            raise ReviewError(
                f'"{g.heard}" is one word: a one-word [names] alias is a bare first '
                "name and needs a person's judgement; add it by hand")
        table = doc.get(section)
        if table is None:
            table = tomlkit.table(is_super_table=True)
            doc[section] = table
        aliases = table.get("aliases")
        if aliases is None:
            aliases = tomlkit.table()
            table["aliases"] = aliases
        existing = aliases.get(g.heard)
        if existing is not None and str(existing) != target:
            raise ReviewError(
                f'"{g.heard}" is already an alias for {existing} in [{section}.aliases]')
        aliases[g.heard] = target

    rf = ReviewFile.read(tenant_root)
    decided_heard = {g.heard for _, g, _ in plan}
    keep = [ln for ln in rf.lines
            if not ((m := _OPEN_RE.match(ln)) and m["heard"] in decided_heard)]
    rf.lines = keep
    for verb, g, target in plan:
        n = len(g.meetings)
        rf._insert_at_end_of(verb, [
            f'- {today.isoformat()} · {g.kind} · "{g.heard}" → {target} · '
            f"×{g.count} in {n} meeting{'s' if n != 1 else ''} · {', '.join(g.codes)}"
        ])
    cfg_path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    rf.write(tenant_root)
    return {
        "accepted": [(g.heard, t, g.kind) for v, g, t in plan if v == "accepted"],
        "rejected": [(g.heard, t, g.kind) for v, g, t in plan if v == "rejected"],
    }


# ──────────────────────────────────────────────────────────────────────
#  Ingest entry point
# ──────────────────────────────────────────────────────────────────────


def collect_for_ingest(
    *,
    config,
    code: str,
    meeting_id: str,
    texts: Iterable[str],
    today: date,
    roster: Iterable | None = None,
    project_dir: Path | None = None,
) -> int:
    """Detect candidates in one meeting's texts for one workstream and queue
    them. Known people are the workstream's stakeholder cards (generated
    view on disk) plus the transcript's speaker labels. Returns lines added.
    Callers must treat any exception as non-fatal."""
    from cp_engine.attribution import speaker_labels
    from cp_engine.stakeholders import cards_from_view

    texts = [t for t in texts if t]
    if not texts:
        return 0
    root = Path(config.root)
    cards = cards_from_view(root, project_dir) if project_dir is not None else []
    speakers = [s for t in texts for s in speaker_labels(t)]
    known = build_known_terms(config=config, roster=roster, cards=cards, speakers=speakers)
    candidates = find_candidates("\n".join(texts), known)
    if not candidates:
        return 0
    return record(root, candidates, code=code, meeting_id=meeting_id, today=today,
                  tenant=getattr(config, "display", None) or root.name)
