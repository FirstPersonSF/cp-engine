"""One-time step-4b migration: markdown-only people → spine Stakeholders cards.

Measured 2026-10-01: of 115 unique people in the tenant's markdown, 58 active
external people (105 overall) had no card — all of Teleflex, the six ibx-5217
contacts with emails. The cp.md ``## Stakeholders`` section and the sprint
``### Stakeholders`` subsection are retired as stores (sync removes them), so
before that, every person they hold gets a home:

* **plan** (:func:`plan_migration`) — read every entry (cp.md sections; every
  week's sprint subsection), canonicalize spellings (the tenant ``[names]
  aliases`` plus :data:`CONFIRMED_VARIANTS`), drop internal people and
  non-people, group one company's mentions of one person, and resolve each
  person against EVERY card of the company (any workstream, any scope —
  ibx-5192's cards sit on an inactive workstream). Each person is then
  ``create`` (no card), ``update`` (a card exists; fill its details block,
  record the variant spellings) or ``match`` (nothing to add) — plus
  ``promote`` when the card is project-scope on a workstream other than the
  ones that mention the person.
* **apply** (:func:`apply_plan`) — the same writers ingest and sync use
  (``cp_engine.stakeholders``). Idempotent: a re-run resolves every person
  to the card the first run created.

``scripts/step4b_migrate_stakeholders.py`` is the operator entry point (dry
run by default).
"""

from __future__ import annotations

import glob
import json
import re as _re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any

from cp_engine import stakeholders as sh

#: Confirmed same-person spellings (Drew, 2026-10-01) → the canonical name.
#: Each choice and its evidence is in :data:`CANONICAL_EVIDENCE`. Kept here,
#: not in the tenant config, because single-word variants ("Jamie", "Mahul")
#: are only safe inside one company's roster; the multi-word ones are
#: proposed for the tenant ``[names] aliases`` as well.
CONFIRMED_VARIANTS: dict[str, dict[str, str]] = {
    "ggl": {"Rena Lanham": "Rina Lanham", "Rena": "Rina Lanham"},
    "ibx": {
        "Mahul": "Mehul Patel", "Mahool": "Mehul Patel", "Mehul": "Mehul Patel",
        "Jamie": "Jaime Mehra", "Jamie Mehra": "Jaime Mehra", "Jaime": "Jaime Mehra",
        "Hazmat Grover": "Hasmit Grover", "Hazmat": "Hasmit Grover",
    },
    "slt": {
        "Art Kalinski": "Art Kilinski",
        "Ryan Pearson": "Ryan Person",
        "Charlie Herzog": "Charles Herzog",
    },
    "sap": {"Michelle Homes Craig": "Michelle Craig"},
    "*": {
        # Geoff Ahmann, First Person's contract Art Director (an MC-2
        # freelancer entity → internal, no card). The tenant aliases carry
        # the confirmed mis-hearings; "Jeff Amon" is the one sprint listing.
        "Jeff Amon": "Geoff Ahmann", "Jeff Almond": "Geoff Ahmann",
        "Geoff Ahman": "Geoff Ahmann",
    },
}

#: Names that are NOT people, found in real entries — skipped and reported.
#: "Rena Ramos" is the old sprint template's example (`[Rena Ramos · Director
#: · primary client decision-maker]`) copied into two ggl-5168 bullets whose
#: content describes Rina Lanham; it is not merged without confirmation.
SUSPECT_NAMES = {
    "rena ramos": "the old sprint template's example name, copied into real bullets "
                  "(content reads as Rina Lanham) — confirm before merging",
    "promote": "a word read as a name ('Promote — APAC regional updater'); likely a "
               "mis-heard name — confirm",
}

#: Possibly-one-person pairs left SEPARATE (a card each) until a human
#: confirms — some are known different people (Nate vs Tyler Johnson).
CANDIDATE_PAIRS = [
    ("ggl", "CBins", "Chris Bins"),
    ("ggl", "Nate Johnson", "Nathan Johnson"),
    ("ggl", "Ealing", "Elin"),
    ("ggl", "Luis", "Louise (internal: Louise Dreier)"),
    ("ibx", "Jared", "Jarrod Kelsey"),
    ("slt", "Adele", "Adelle Bonavire (card)"),
]

CANONICAL_EVIDENCE = {
    "Rina Lanham": (
        "spine card ggl-5185/_authored/rina-lanham; 1,838 tenant hits vs 13 "
        "'Rena Lanham' (5 cp.md files); no email on record"),
    "Mehul Patel": (
        "email mpatel1@infoblox.com + Janet's 6/16 workshop-invite roster "
        "('Mehul Patel'); card 'Mehul' (ibx-5192); 'Mahul' 590 / 'Mahool' 73 "
        "hits are transcription"),
    "Jaime Mehra": (
        "Google Docs mention '[@Jaime Mehra](mailto:jmehra@infoblox.com)' "
        "(account display name) + account card; 'Jamie Mehra' 13 hits"),
    "Hasmit Grover": (
        "email hgrover@infoblox.com; Janet's invite roster spells 'Hasmit' "
        "and notes prior 'Hazmat' notes"),
    "Art Kilinski": "email header 'Art Kilinski <art.kilinski@salesloft.com>' + card",
    "Ryan Person": "email ryan.person@sovos.com + LinkedIn /in/ryanperson + card",
    "Charles Herzog": (
        "email c.herzog@tricentis.com + LinkedIn /in/charles-herzog-… + card; "
        "'Charlie' (157 transcript hits) kept as an alias — the name he goes by "
        "in the room"),
    "Michelle Craig": (
        "work email michelle.craig@sap.com + 268 tenant hits; LinkedIn "
        "/in/michellehomescraig (card framing) kept as alias 'Michelle Homes Craig'"),
    "Geoff Ahmann": (
        "MC-2 freelancer entity 'Geoff Ahmann' (2,404 tenant hits); tenant "
        "[names] aliases"),
}


@dataclass
class Person:
    company: str                         # company code prefix (ggl, ibx, …)
    name: str                            # canonical
    mentions: list[sh.Mention] = field(default_factory=list)
    variants: set[str] = field(default_factory=set)
    card: sh.Card | None = None
    action: str = ""                     # create | update | match
    promote: bool = False
    home_code: str = ""
    home_id: str = ""
    scope: str = "project"
    merged_from: list[str] = field(default_factory=list)   # first-name merges
    flags: list[str] = field(default_factory=list)
    details: sh.Details | None = None

    @property
    def codes(self) -> list[str]:
        return sorted({m.code for m in self.mentions})


@dataclass
class Skip:
    code: str
    name: str
    reason: str
    source: str


@dataclass
class Plan:
    people: list[Person] = field(default_factory=list)
    skipped: list[Skip] = field(default_factory=list)
    # (code, from, to, source) per rewrite applied
    canonicalized: list[tuple[str, str, str, str]] = field(default_factory=list)
    unresolved_codes: set[str] = field(default_factory=set)


#: Words that put a bare first name on OUR side of the table (the contractor
#: bench), outranking a client-company word in the same entry.
_OUR_SIDE_RE = _re.compile(
    r"\b(first ?person|1p|our team|production partner|animator|contractor|"
    r"freelancer?|consultant|subcontractor)\b")


def _co(code: str) -> str:
    return code.split("-", 1)[0].lower()


def canonical(name: str, company: str, tenant_aliases: Mapping[str, str]) -> str:
    n = sh.apply_aliases(name, tenant_aliases)
    for table in (CONFIRMED_VARIANTS.get(company, {}), CONFIRMED_VARIANTS["*"]):
        if n in table:
            return table[n]
    return n


def resolve_stem(stem: str, index: Mapping[str, Any]) -> str | None:
    """A sprint file's stem → its indexed workstream code: exact, else the
    one indexed code with the same ``<co>-<number>`` (older weeks carry the
    short or pre-rename stem: ``tel-5149``, ``slt-5175-brand``)."""
    if stem in index:
        return stem
    from cp_engine.codes import parse_code

    parsed = parse_code(stem)
    if parsed is None:
        return None
    hits = [c for c in index
            if (pc := parse_code(c)) is not None and pc.number == parsed.number
            and c.split("-", 1)[0] == stem.split("-", 1)[0]]
    return hits[0] if len(hits) == 1 else None


def collect_mentions(root: Path, index: Mapping[str, Any], *,
                     include_inactive: bool) -> tuple[list[sh.Mention], set[str]]:
    """Every entry of every cp.md section and every week's sprint subsection,
    keyed to an indexed workstream code. Returns ``(mentions, unresolved
    sprint stems)``."""
    active = {c for c, e in index.items() if e.status in ("Deal", "Open")}
    wanted = set(index) if include_inactive else active
    out: list[sh.Mention] = []
    for code in sorted(wanted):
        cp = root / index[code].path / "cp.md"
        if cp.is_file():
            out += sh.parse_cpmd_section(cp.read_text(encoding="utf-8"), code=code)
    unresolved: set[str] = set()
    for f in sorted(glob.glob(str(root / "sprints" / "20*-W*" / "*.md"))):
        p = Path(f)
        if p.stem.startswith("_") or p.stem == "README":
            continue
        code = resolve_stem(p.stem, index)
        if code is None:
            unresolved.add(p.stem)
            continue
        if code not in wanted:
            continue
        out += sh.parse_sprint_subsection(p.read_text(encoding="utf-8"), code=code,
                                          week=p.parent.name)
    return out, unresolved


def plan_migration(
    mentions: Iterable[sh.Mention],
    *,
    cards: list[sh.Card],
    index: Mapping[str, Any],
    project_company: Mapping[str, str | None],
    roster: sh.Roster,
    tenant_aliases: Mapping[str, str] | None = None,
    company_names: Mapping[str, str] | None = None,
) -> Plan:
    """Order matters, and each step is reported:

    1. canonicalize the name; skip suspects, non-people and internal people
       (a full internal name, a team first name, an internal group header);
    2. bucket one company's mentions of one name into a :class:`Person`;
    3. resolve each person to a card (the canonical name, the spellings seen,
       every confirmed variant) against EVERY card of the company;
    4. merge persons that resolved to the same card ("Michelle" and
       "Michelle Craig" → one);
    5. fold a first-name-only person into the ONE full-name person (or card)
       of the company with that first name or alias ("Brad" → Brad Rinklin);
    6. only then drop a still-unclaimed bare first name that an internal
       person carries ("Stefan", "Louise") — it was the team.
    """
    tenant_aliases = dict(tenant_aliases or {})
    company_names = dict(company_names or {})
    company_names_norm = {sh.norm_name(n) for n in company_names.values() if n}
    plan = Plan()

    def _client_word(m: sh.Mention) -> bool:
        """The entry itself names the workstream's client company (or says
        "client") — then a bare first name is the client's person."""
        entry = index.get(m.code)
        cid = project_company.get(entry.mc2_id) if entry and entry.mc2_id else None
        name = company_names.get(cid or "", "")
        words = {w for w in sh.norm_name(name).split() if len(w) > 2}
        text = sh.norm_name(f"{m.role} {m.context} {m.group}")
        if _OUR_SIDE_RE.search(text):
            return False  # "FirstPerson (prior interviewer)", "production partner"
        return "client" in text.split() or bool(words & set(text.split()))
    company_cards: dict[str, list[sh.Card]] = defaultdict(list)
    for c in cards:
        company_cards[_co(c.project_code)].append(c)

    # 1–2
    by_key: dict[tuple[str, str], Person] = {}
    for m in mentions:
        co = _co(m.code)
        canon = canonical(m.name, co, tenant_aliases)
        if canon != m.name:
            plan.canonicalized.append((m.code, m.name, canon, m.source))
        n = sh.norm_name(canon)
        if n in SUSPECT_NAMES:
            plan.skipped.append(Skip(m.code, m.name, SUSPECT_NAMES[n], m.source))
            continue
        if not sh.is_person_name(canon) or n in company_names_norm:
            plan.skipped.append(Skip(m.code, m.name, "not a person", m.source))
            continue
        if roster.is_internal(canon, m.group):
            plan.skipped.append(Skip(m.code, m.name, "internal", m.source))
            continue
        person = by_key.setdefault((co, n), Person(company=co, name=canon))
        person.mentions.append(m)
        if sh.norm_name(m.name) != n:
            person.variants.add(m.name)
        person.variants.update(a for a in m.aliases if sh.norm_name(a) != n)

    # 3
    for (co, _n), person in by_key.items():
        for nm in _names_to_try(person):
            person.card = sh.match_card(company_cards[co], nm)
            if person.card is not None:
                break

    # 4
    by_card: dict[tuple[str, str], Person] = {}
    for key, person in sorted(by_key.items(), key=lambda kv: -len(kv[0][1])):
        if person.card is None:
            continue
        ck = (str(person.card.project_id), person.card.est_item_id)
        keep = by_card.get(ck)
        if keep is None:
            by_card[ck] = person
            continue
        keep.mentions += person.mentions
        keep.variants |= person.variants | {person.name}
        keep.merged_from.append(person.name)
        del by_key[key]

    # 5
    for (co, n), person in list(by_key.items()):
        if len(n.split()) != 1 or person.card is not None:
            continue
        targets = {
            k for k, p in by_key.items()
            if k[0] == co and len(k[1].split()) > 1
            and (k[1].split()[0] == n or n in {sh.norm_name(a) for a in p.variants})
        }
        card_targets = [c for c in company_cards[co] if c.is_person
                        and len(sh.norm_name(c.name).split()) > 1
                        and sh.norm_name(c.name).split()[0] == n]
        claimed = {(str(p.card.project_id), p.card.est_item_id)
                   for k, p in by_key.items() if k in targets and p.card}
        loose_cards = [c for c in card_targets
                       if (str(c.project_id), c.est_item_id) not in claimed]
        if len(targets) + len(loose_cards) != 1:
            continue
        if targets:
            target = by_key[next(iter(targets))]
        else:
            card = loose_cards[0]
            target = by_key.setdefault((co, sh.norm_name(card.name)),
                                       Person(company=co, name=card.name, card=card))
        target.mentions += person.mentions
        target.variants.add(person.name)
        target.merged_from.append(person.name)
        del by_key[(co, n)]

    # 6
    for (co, n), person in list(by_key.items()):
        if person.card is None and roster.is_internal_first_name(person.name):
            client_side = [m for m in person.mentions if _client_word(m)]
            for m in person.mentions:
                if m not in client_side:
                    plan.skipped.append(Skip(m.code, m.name, "internal (first name)", m.source))
            if client_side:
                person.mentions = client_side
                person.flags.append("first name shared with an internal person; "
                                    "kept because the entry names the client")
            else:
                del by_key[(co, n)]

    account_node = {(e.company or "").lower(): c
                    for c, e in index.items() if e.label == "account"}
    for (co, _n), person in sorted(by_key.items()):
        card = person.card
        if card is not None and sh.norm_name(card.name) != sh.norm_name(person.name):
            person.variants.add(card.name)
        first = person.mentions[0]
        role = next((m.role for m in person.mentions if m.source == "cp.md" and m.role),
                    "") or next((m.role for m in reversed(person.mentions) if m.role), "")
        email = next((m.email for m in person.mentions if m.email), "")
        person.details = sh.Details(
            name=person.name, role=sh._clip(role, 160), email=email,
            side=sh.side_for(first),
            aliases=tuple(sorted(v for v in person.variants
                                 if sh.norm_name(v) != sh.norm_name(person.name))),
        )
        codes = person.codes
        if card is not None:
            merged = sh.merge_details(card.details, person.details)
            person.action = "match" if card.details == merged else "update"
            person.promote = card.scope != "account" and any(
                c != card.project_code for c in codes)
            person.home_code, person.scope = card.project_code, card.scope
            continue
        person.action = "create"
        if len(codes) == 1:
            person.home_code, person.scope = codes[0], "project"
        else:
            node = account_node.get(co)
            if node is None:
                counts: dict[str, int] = defaultdict(int)
                for m in person.mentions:
                    counts[m.code] += 1
                node = max(sorted(counts), key=lambda c: counts[c])
            person.home_code, person.scope = node, "account"
        person.home_id = index[person.home_code].mc2_id if person.home_code in index else ""
    plan.people = sorted(by_key.values(), key=lambda p: (p.home_code, p.name))
    return plan


def _names_to_try(person: Person) -> list[str]:
    """The canonical name, the spellings seen, and every confirmed variant
    that maps onto the canonical name (so "Michelle Craig" finds the card
    framed "Michelle Homes Craig")."""
    confirmed = [k for k, v in {**CONFIRMED_VARIANTS.get(person.company, {}),
                                **CONFIRMED_VARIANTS["*"]}.items() if v == person.name]
    return list(dict.fromkeys([person.name, *sorted(person.variants), *confirmed]))


def apply_plan(client: Any, plan: Plan, *, project_company: Mapping[str, str | None],
               company_names: Mapping[str, str], today: date,
               dry_run: bool = False) -> list[str]:
    """Write the plan. Re-checks each person against MC-2's cards first, so a
    second run (or a card a hosted session created meanwhile) never mints a
    twin. Returns one log line per write."""
    log: list[str] = []
    live = sh.fetch_cards(client) if not dry_run else []
    for person in plan.people:
        pool = [c for c in live if _co(c.project_code) == person.company]
        card = None
        for nm in _names_to_try(person):
            card = sh.match_card(pool, nm)
            if card is not None:
                break
        card = card or person.card
        cid = project_company.get(person.home_id or (card.project_id if card else ""), None)
        details = person.details
        if details is not None and cid and not details.company:
            details = replace(details, company=company_names.get(cid, ""))
        if card is not None and card.row is not None:
            w = sh.update_card_details(client, card, details, today=today,
                                       note="Step 4b migration: details from markdown",
                                       dry_run=dry_run)
            log.append(f"{w.action:8} {card.project_code}/{card.est_item_id}  {person.name}")
            if person.promote and card.scope != "account":
                ccid = project_company.get(str(card.project_id))
                if ccid:
                    sh.promote_card(client, card, company_id=ccid, dry_run=dry_run)
                    log.append(f"promote  {card.project_code}/{card.est_item_id} → account")
            continue
        if not person.home_id:
            log.append(f"SKIP     {person.name}: no MC-2 id for {person.home_code}")
            continue
        body = sh.card_body(details, person.mentions, today=today,
                            provenance="migrated from markdown (step 4b)")
        w = sh.create_card(client, project_id=person.home_id, project_code=person.home_code,
                           details=details, body=body, today=today, scope=person.scope,
                           company_id=cid if person.scope == "account" else None,
                           dry_run=dry_run)
        log.append(f"{w.action:8} {person.home_code}/{w.est_item_id}  "
                   f"[{person.scope}]  {person.name}")
        if not dry_run:
            live.append(sh.card_from_row(w.rows[0]))
    return log


def summary(plan: Plan) -> dict:
    by_ws: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for p in plan.people:
        by_ws[p.home_code][p.action] += 1
        if p.promote:
            by_ws[p.home_code]["promote"] += 1
    skipped = defaultdict(set)
    for s in plan.skipped:
        skipped[s.reason].add((_co(s.code), sh.norm_name(s.name)))
    return {
        "people": len(plan.people),
        "create": sum(1 for p in plan.people if p.action == "create"),
        "update": sum(1 for p in plan.people if p.action == "update"),
        "match": sum(1 for p in plan.people if p.action == "match"),
        "promote": sum(1 for p in plan.people if p.promote),
        "internal_unique": len(skipped["internal"]),
        "not_a_person_unique": len(skipped["not a person"]),
        "by_workstream": {k: dict(v) for k, v in sorted(by_ws.items())},
    }


def to_json(plan: Plan) -> str:
    return json.dumps({
        "summary": summary(plan),
        "people": [{
            "company": p.company, "name": p.name, "action": p.action,
            "promote": p.promote, "home": p.home_code, "scope": p.scope,
            "card": f"{p.card.project_code}/{p.card.est_item_id}" if p.card else None,
            "variants": sorted(p.variants), "merged_from": p.merged_from,
            "flags": p.flags,
            "workstreams": p.codes,
            "details": ({k: getattr(p.details, k) for k in ("role", "email", "side")}
                        if p.details else None),
            "mentions": [f"{m.code} {m.source}: {m.name} — {m.text[:120]}" for m in p.mentions],
        } for p in plan.people],
        "skipped": [vars(s) for s in plan.skipped],
        "canonicalized": plan.canonicalized,
        "unresolved_sprint_stems": sorted(plan.unresolved_codes),
    }, indent=2, ensure_ascii=False)
