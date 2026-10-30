"""Person-name checks on an auto-ingest plan (#312).

THE REPORT. Auto-ingest names client people in risk and decision bullets
using Fathom's speaker labels, and those labels are wrong often enough to
matter. On slt-5196 meeting `c3864ea3` (2026-09-28) the transcript carries
exactly two speaker labels — Drew Fiero and Marcello Grande — because the
client side shared Marcello's room. Every client line (Leah's, Morgan's,
the interviewee's) is labelled "Marcello", and the ingest confidently wrote
"Morgan admitted she was 'not at all prepared'" plus a client-risk bullet
built on it. The line was Leah's. Around it, names were mistranscribed and
minted: "Ryan Pearson" for the card's Ryan Person, "Morgan McCauley" for
Morgan Wright (a project with 112 correct occurrences), "Jeff Allman" for
Geoff Ahmann.

WHAT THIS MODULE DOES — all deterministic, run on the plan after the model
returns it:

1. **Aliases.** A tenant map (`[names] aliases` in `.cp-engine.toml`)
   rewrites known mis-hearings everywhere in the plan. Mis-heard product
   and brand terms have their own map (`[terms] aliases`, #371,
   ``apply_terms``): plain whole-word rewrites, run first.
2. **Resolution against the cards.** A "First Last" whose first name
   belongs to exactly ONE known person, with a different surname, is that
   person: the surname is corrected. A `stakeholders` entry that resolves
   to a known person is dropped instead of minted.
3. **Hedging.** A person is *heard* in this meeting when their first name
   is one of the transcript's speaker labels (and the label does not
   address itself by name — "Morgan, can you show it to me?" under the
   label Morgan is someone else talking). A risk or decision that names a
   known external person who was NOT heard is attribution by inference,
   and gets ``[attribution unverified]``. Inbound items get the same tag
   only when they quote a person who was not heard — risk and decision
   bullets carry authority downstream, so they are held to the stricter
   rule (issue ask 3).

"Known people" are the stakeholder cards that apply to the project (spine
Stakeholders layer — its own and its ancestors' cards plus the company's
account-scope cards; see ``cp_engine.stakeholders``): each card's name (the
details block's ``Name``, else the name leading ``framing``) and its
``Aliases``, plus the speaker labels themselves. A card's alias is also a
rewrite ("Rena Lanham" → "Rina Lanham"; one-word "Elin" → "Aoileann Banks",
#355), like a tenant alias. A ONE-word alias — tenant or card — is a bare
first name: it rewrites only where it stands alone (``apply_aliases``), and
not at all on a workstream that knows a different person by that first name.
Speaker labels are read through the aliases too, so a person Fathom labels
by a mis-heard name still counts as heard. Step 4b
retired the sprint file's ``### Stakeholders`` bullets as a source: cards are
the one store. Internal team members (`[team] members`) are never hedged and
never used to correct a surname.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping

HEDGE = "[attribution unverified]"

# `00:57:55 - Marcello Grande` — the staged transcript format (webhook
# `_segments_to_text` and Fathom's own export both produce it).
_SPEAKER_RE = re.compile(r"^\s*\d{1,2}:\d{2}(?::\d{2})?\s+-\s+(?P<name>[^\n]+?)\s*$", re.MULTILINE)

_NAME_WORD_RE = re.compile(r"^[A-Z][A-Za-z'À-ſ.-]*$")
# A framing that names an organisation rather than a person.
_NOT_A_PERSON_RE = re.compile(
    r"\b(vendor|agency|company|profile|roster|cast|list|owners|stakeholders)\b",
    re.IGNORECASE,
)
_NAME_WORDS_MAX = 4

# A quoted phrase. Used for INBOUND only, where the
# `who` field is itself the attribution.
_QUOTE_RE = re.compile(r"[\"“”]|(?:^|\s)'[^']{3,}'")


@dataclass(frozen=True)
class Person:
    full: str

    @property
    def first(self) -> str:
        return self.full.split()[0]


@dataclass
class KnownPeople:
    people: list[Person] = field(default_factory=list)
    team_firsts: frozenset[str] = frozenset()
    #: Card aliases → the card's name (applied like tenant aliases).
    aliases: dict[str, str] = field(default_factory=dict)

    def add(self, name: str) -> None:
        name = " ".join(name.split())
        if not name or any(p.full.lower() == name.lower() for p in self.people):
            return
        self.people.append(Person(name))

    def by_first(self, first: str) -> list[Person]:
        f = first.lower()
        return [p for p in self.people if p.first.lower() == f]

    def is_team(self, first: str) -> bool:
        return first.lower() in self.team_firsts


# ──────────────────────────────────────────────────────────────────────
#  Inputs
# ──────────────────────────────────────────────────────────────────────


def speaker_labels(transcript: str) -> list[str]:
    """Distinct speaker labels in transcript order."""
    seen: dict[str, None] = {}
    for m in _SPEAKER_RE.finditer(transcript or ""):
        seen.setdefault(" ".join(m.group("name").split()), None)
    return list(seen)


def self_addressing_labels(transcript: str) -> set[str]:
    """Labels under which someone addresses the label's OWN first name as a
    vocative ("Morgan, can you show it to me?" labelled Morgan). The label
    is then carrying another speaker's lines, so it proves nothing about
    who was heard."""
    out: set[str] = set()
    blocks = re.split(r"\n(?=\s*\d{1,2}:\d{2}(?::\d{2})?\s+-\s+)", transcript or "")
    for block in blocks:
        m = _SPEAKER_RE.match(block)
        if not m:
            continue
        label = " ".join(m.group("name").split())
        first = label.split()[0] if label.split() else ""
        if len(first) < 2:
            continue
        body = block[m.end():]
        # Vocative: the first name followed by a comma or question mark, at
        # the start of a sentence or after one.
        if re.search(
            rf"(?:^|[.?!]\s+|\s){re.escape(first)}\s*[,?]", body
        ) and not re.search(rf"\b(?:I'm|I am|this is|it's)\s+{re.escape(first)}\b", body, re.IGNORECASE):
            out.add(label)
    return out


def name_from_framing(framing: str) -> str | None:
    """`Morgan Wright — Salesloft (…)` → `Morgan Wright`. The one definition
    lives in ``cp_engine.stakeholders``; re-exported for callers here."""
    from cp_engine.stakeholders import name_from_framing as _nff

    return _nff(framing)


def load_known_people(
    *,
    tenant_root: Path | None,
    project_dir: Path | None,
    team: Iterable[str] = (),
    transcript: str = "",
    code: str | None = None,
    client=None,
    cards=None,
) -> KnownPeople:
    """Everyone this project already knows by name: the stakeholder cards
    that apply to it (MC-2 when ``client`` is given, else the generated view
    on disk — ``spine/`` and ``_stakeholders/`` up the ancestor chain) and
    the meeting's own speaker labels. ``cards`` short-circuits the read."""
    from cp_engine import stakeholders as sh

    known = KnownPeople(
        team_firsts=frozenset(t.strip().split()[0].lower() for t in team if t.strip())
    )
    if cards is None:
        if client is not None and tenant_root is not None and code:
            cards = sh.cards_for(Path(tenant_root), code, client=client,
                                 project_dir=project_dir)
        else:
            cards = sh.cards_from_view(tenant_root, project_dir)
    for card in cards:
        if not card.is_person or card.side == "internal":
            continue
        name = card.name
        if not all(_NAME_WORD_RE.match(w) for w in name.split()):
            continue
        known.add(name)
        for alias in card.aliases:
            alias = " ".join(alias.split())
            if not alias or alias.lower() == name.lower():
                continue
            # A bare first name (#355): its own first name is not a
            # mis-hearing, and only a proper noun is a name.
            if len(alias.split()) == 1 and (
                alias.lower() == name.split()[0].lower() or not _NAME_WORD_RE.match(alias)
            ):
                continue
            known.aliases.setdefault(alias, name)
    for label in speaker_labels(transcript):
        if all(_NAME_WORD_RE.match(w) for w in label.split()):
            known.add(label)
    return known


# ──────────────────────────────────────────────────────────────────────
#  Text transforms
# ──────────────────────────────────────────────────────────────────────


def apply_aliases(text: str, aliases: Mapping[str, str]) -> str:
    """Rewrite each alias key to its value, longest key first, on word
    boundaries (case-sensitive: `Jeff` → `Geoff` must not touch `jeffrey`).
    A one-word key rewrites only a name standing alone (#355). The one
    definition lives in ``cp_engine.stakeholders``."""
    from cp_engine.stakeholders import apply_aliases as _apply

    return _apply(text, aliases)


def apply_terms(text: str, terms: Mapping[str, str] | None) -> str:
    """The tenant ``[terms] aliases`` rewrite (#371): mis-heard product and
    brand terms ("Juul", "Jool" → "Joule"). Longest key first, whole word,
    case-sensitive. Unlike a person alias a term has no first-name rules,
    and a hyphen is a boundary ("Juul-based" → "Joule-based"); a key inside
    a longer word ("Juuls", "Joolie") is left alone."""
    if not text or not terms:
        return text
    for wrong in sorted(terms, key=len, reverse=True):
        right = terms[wrong]
        if not wrong or wrong == right:
            continue
        text = re.sub(rf"(?<!\w){re.escape(wrong)}(?!\w)", lambda _m, r=right: r, text)
    return text


def aliases_for(known: KnownPeople, aliases: Mapping[str, str] | None) -> dict[str, str]:
    """The aliases to apply on one workstream: its cards' aliases plus the
    tenant's (tenant wins), minus any one-word alias whose first name a
    DIFFERENT known person carries — there "Elin" may be Elin Smith (#355)."""
    merged = {**known.aliases, **dict(aliases or {})}
    out: dict[str, str] = {}
    for wrong, right in merged.items():
        if len(wrong.split()) == 1:
            # A bare "Elin" (a speaker label, a first-name-only card) is the
            # mis-hearing itself, not a second person.
            spare = {" ".join(right.split()).lower(), wrong.lower()}
            if any(p.first.lower() == wrong.lower() and p.full.lower() not in spare
                   for p in known.people):
                continue
        out[wrong] = right
    return out


_FULL_NAME_RE = re.compile(
    r"(?<![\w-])(?P<first>[A-Z][a-z'À-ſ-]+)\s+(?P<last>[A-Z][A-Za-z'À-ſ-]+)(?![\w-])"
)


def resolve_surnames(text: str, known: KnownPeople) -> str:
    """`Morgan McCauley` → `Morgan Wright` when Morgan Wright is the ONLY
    known Morgan and "Morgan McCauley" is not itself known. A first name
    shared by two known people, or by an internal team member, is left
    alone — resolution must never pick between people."""
    if not text:
        return text

    def _fix(m: re.Match) -> str:
        first, last = m.group("first"), m.group("last")
        full = f"{first} {last}"
        if any(p.full.lower() == full.lower() for p in known.people):
            return full
        if known.is_team(first):
            return full
        matches = known.by_first(first)
        if len(matches) != 1:
            return full
        target = matches[0]
        if len(target.full.split()) < 2:
            return full  # card knows only a first name: nothing to correct to
        return target.full

    return _FULL_NAME_RE.sub(_fix, text)


def _mentioned(text: str, known: KnownPeople) -> list[Person]:
    out: list[Person] = []
    for p in known.people:
        if re.search(rf"(?<![\w-]){re.escape(p.first)}(?![\w-])", text or ""):
            out.append(p)
    return out


def unheard_external_mentions(
    text: str, known: KnownPeople, heard_firsts: set[str]
) -> list[Person]:
    """Known EXTERNAL people named in `text` who were not heard in the
    meeting (no reliable speaker label carries their first name)."""
    return [
        p for p in _mentioned(text, known)
        if not known.is_team(p.first) and p.first.lower() not in heard_firsts
    ]


def heard_first_names(transcript: str, aliases: Mapping[str, str] | None = None) -> set[str]:
    """First names the transcript's reliable speaker labels carry — each
    label also read through ``aliases``, so "Elin" hears Aoileann (#355)."""
    unreliable = self_addressing_labels(transcript)
    out: set[str] = set()
    for label in speaker_labels(transcript):
        if not label.split() or label in unreliable:
            continue
        out.add(label.split()[0].lower())
        aliased = apply_aliases(label, aliases or {})
        if aliased.split():
            out.add(aliased.split()[0].lower())
    return out


# ──────────────────────────────────────────────────────────────────────
#  Plan pass
# ──────────────────────────────────────────────────────────────────────

_STRICT_VERBS = {
    "risks", "risk", "record-risk",
    "decisions", "decision", "add-decision",
}
_INBOUND_VERBS = {"inbound", "record-inbound"}
_STAKEHOLDER_VERBS = {"stakeholders", "stakeholder", "record-stakeholder"}
_TEXT_FIELDS = ("text", "who", "name", "context", "from_party", "owner")
# Term aliases (#371) reach every prose field a plan item carries.
_TERM_FIELDS = (*_TEXT_FIELDS, "what", "description", "deliverable", "role", "company")


def _hedge(text: str) -> str:
    if HEDGE in text:
        return text
    # Keep a trailing routing marker (`[cross-project? → code]`) last.
    m = re.search(r"\s*(\[cross-project\? → [^\]]+\])\s*$", text)
    if m:
        return f"{text[:m.start()].rstrip()} {HEDGE} {m.group(1)}"
    return f"{text.rstrip()} {HEDGE}"


def check_plan_attribution(
    plan: dict,
    *,
    transcript: str,
    known_for: "callable",
    aliases: Mapping[str, str] | None = None,
    term_aliases: Mapping[str, str] | None = None,
) -> dict:
    """Apply aliases, surname resolution and hedging to `plan` in place.

    `known_for(code)` returns the `KnownPeople` for one project block (the
    caller owns disk access). Returns a counts summary for the run log:
    `{aliased, resolved, hedged, stakeholders_dropped}`, plus `terms` (the
    fields a `[terms] aliases` rewrite changed, #371) when any are set.
    Term aliases run first, over every prose field, then the name pass.
    """
    summary = {"aliased": 0, "resolved": 0, "hedged": 0, "stakeholders_dropped": 0}
    aliases = dict(aliases or {})
    terms = dict(term_aliases or {})
    if terms:
        summary["terms"] = _apply_plan_terms(plan, terms)

    def _clean(value, known, scoped):
        if not isinstance(value, str) or not value:
            return value
        aliased = apply_aliases(value, scoped)
        if aliased != value:
            summary["aliased"] += 1
        resolved = resolve_surnames(aliased, known)
        if resolved != aliased:
            summary["resolved"] += 1
        return resolved

    for code, entries in (plan.get("projects") or {}).items():
        if not isinstance(entries, dict):
            continue
        known = known_for(code)
        scoped = aliases_for(known, aliases)
        heard = heard_first_names(transcript, scoped)
        for verb, items in list(entries.items()):
            if not isinstance(items, list):
                continue
            kept: list = []
            for item in items:
                if not isinstance(item, dict):
                    kept.append(item)
                    continue
                for f in _TEXT_FIELDS:
                    if f in item:
                        item[f] = _clean(item[f], known, scoped)
                if verb in _STAKEHOLDER_VERBS:
                    name = str(item.get("name") or "").strip()
                    if name and _resolves_to_known(name, known):
                        summary["stakeholders_dropped"] += 1
                        continue
                text = item.get("text")
                if isinstance(text, str) and text:
                    if verb in _STRICT_VERBS:
                        if unheard_external_mentions(text, known, heard):
                            if HEDGE not in text:
                                summary["hedged"] += 1
                            item["text"] = _hedge(text)
                    elif verb in _INBOUND_VERBS and _QUOTE_RE.search(text):
                        who = str(item.get("who") or "")
                        subject = f"{who} {text}"
                        if unheard_external_mentions(subject, known, heard):
                            if HEDGE not in text:
                                summary["hedged"] += 1
                            item["text"] = _hedge(text)
                kept.append(item)
            entries[verb] = kept
        for verb in [v for v, items in entries.items() if isinstance(items, list) and not items]:
            del entries[verb]

    summary_block = plan.get("account_summary")
    for item in summary_block if isinstance(summary_block, list) else [summary_block]:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            item["text"] = apply_aliases(item["text"], aliases)
    for item in plan.get("account_decisions") or []:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            item["text"] = apply_aliases(item["text"], aliases)
    return summary


def _apply_plan_terms(plan: dict, terms: Mapping[str, str]) -> int:
    """Rewrite `[terms] aliases` in every prose field of `plan`, in place:
    project items, the account summary and decisions, week themes and week
    open questions. Returns how many fields changed."""
    changed = 0

    def _fix(item) -> None:
        nonlocal changed
        if not isinstance(item, dict):
            return
        for f in _TERM_FIELDS:
            value = item.get(f)
            if isinstance(value, str) and value:
                fixed = apply_terms(value, terms)
                if fixed != value:
                    item[f] = fixed
                    changed += 1

    for entries in (plan.get("projects") or {}).values():
        if isinstance(entries, dict):
            for items in entries.values():
                for item in items if isinstance(items, list) else []:
                    _fix(item)
    for key in ("account_summary", "account_decisions", "themes", "week_open_questions"):
        block = plan.get(key)
        for item in block if isinstance(block, list) else [block]:
            _fix(item)
    return changed


def _resolves_to_known(name: str, known: KnownPeople) -> bool:
    """A would-be new stakeholder is already known: the exact name, an
    internal team member, or a first name that belongs to exactly one known
    person with a full name (the 09-22 "Morgan McCauley" for Morgan Wright)."""
    if any(p.full.lower() == name.lower() for p in known.people):
        return True
    first = name.split()[0]
    if known.is_team(first):
        return True
    matches = known.by_first(first)
    return len(matches) == 1 and len(matches[0].full.split()) >= 2
