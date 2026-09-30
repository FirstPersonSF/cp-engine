"""``cp commitments-sweep`` — surface stale/undated commitments for review.

Read-only (#135). ``list_commitments`` exists as an MCP verb but returns
flat JSON with no age, no staleness signal, and no grouping — a sweep of
~100 rows across projects was a manual read. This module groups open rows
by project, ages them, sorts oldest-first, and renders the fields that
drive a keep/close decision. Resolution stays a deliberate human/agent
act via ``resolve_commitment`` — the sweep makes the *decision* cheap,
not automatic.

Pairs with the wrap-up ritual (#134) and the dates-loop TTL (#136): rows
the TTL will expire are marked so the sweep shows what's about to close
on its own.

#311: likely-duplicate pairs. A commitment logged by hand mid-session and the
row auto-ingest later writes for the same meeting never share text, so the
content-hash dedupe cannot see them, and successive meetings restate one
obligation in new words. `likely_duplicates` pairs same-project open rows by
description similarity and the sweep shows the pairs. It FLAGS; it never
merges or resolves — closing the redundant row stays a human act.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from cp_engine.clock import tenant_today
from cp_engine.dates_loop import _EXPIRE_AFTER_DAYS, _ttl_bucket
from cp_engine import mc2_db
from cp_engine.mc2_db import Tables

def _sweep_columns(client: Any) -> str:
    """Owner columns are schema-dependent (see `mc2_db.owner_columns`)."""
    return (
        "id, description, owner_email, owner_name, due_date, date_status, "
        f"status, source_kind, source_meeting_id, {mc2_db.owner_columns(client)}, created_at"
    )

# --stale: undated AND at least this old — the "is this still real?" bucket.
STALE_DAYS = 14


@dataclass
class SweepRow:
    id: str
    description: str
    owner: str
    source_kind: str
    due_date: date | None
    date_status: str
    age_days: int
    ttl: str | None  # 'warn' | 'expire' | None (dates-loop TTL, #136)
    source_meeting_id: str | None = None

    @property
    def undated(self) -> bool:
        return self.due_date is None

    @property
    def stale(self) -> bool:
        return self.undated and self.age_days >= STALE_DAYS


def _row(c: dict, today: date) -> SweepRow:
    created = None
    try:
        raw = (c.get("created_at") or "").replace("Z", "+00:00")
        created = datetime.fromisoformat(raw).date()
    except ValueError:
        pass
    due = None
    if c.get("due_date"):
        due = date.fromisoformat(c["due_date"])
    return SweepRow(
        id=c["id"],
        description=c.get("description") or "(no description)",
        owner=c.get("owner_name") or c.get("owner_email") or "unowned",
        source_kind=c.get("source_kind") or "?",
        due_date=due,
        date_status=c.get("date_status") or "proposed",
        age_days=(today - created).days if created else 0,
        ttl=_ttl_bucket(c, today),
        source_meeting_id=c.get("source_meeting_id"),
    )


def _owner_codes(client: Any) -> dict[str, str]:
    """id → code across every workstream (one read of `projects`)."""
    codes: dict[str, str] = {}
    for r in client.table(Tables.PROJECTS).select("id, code").execute().data or []:
        if r.get("id") and r.get("code"):
            codes[r["id"]] = r["code"]
    return codes


def sweep(
    client: Any,
    *,
    code: str | None = None,
    today: date | None = None,
    undated_only: bool = False,
    older_than: int | None = None,
    stale_only: bool = False,
) -> dict[str, list[SweepRow]]:
    """Open commitments grouped by project code, oldest-first within each.

    ``code=None`` sweeps tenant-wide. Filters compose (AND).
    """
    today = today or tenant_today()

    query = (
        client.table(Tables.COMMITMENTS)
        .select(_sweep_columns(client))
        .eq("status", "open")
    )
    if code is not None:
        from cp_engine.commitments import resolve_commitment_owner

        owner = resolve_commitment_owner(client, code)
        if owner is None:
            raise ValueError(f"no workstream resolves for code {code!r}")
        # One owner column since #301. (Its predecessor picked the column
        # off `owner["kind"]` and once tested for a kind the resolver never
        # emitted, so every engagement sweep returned zero — v0.98.0.)
        query = query.eq(mc2_db.OWNER_COLUMN, owner["id"])
    rows = query.execute().data or []

    codes = _owner_codes(client)
    groups: dict[str, list[SweepRow]] = {}
    for c in rows:
        r = _row(c, today)
        if undated_only and not r.undated:
            continue
        if older_than is not None and r.age_days < older_than:
            continue
        if stale_only and not r.stale:
            continue
        owner_id = c.get(mc2_db.OWNER_COLUMN)
        group = code or codes.get(owner_id, "(unmapped)")
        groups.setdefault(group, []).append(r)

    for rs in groups.values():
        rs.sort(key=lambda r: -r.age_days)
    return dict(sorted(groups.items()))


# --- #311 likely duplicates ------------------------------------------------
#
# Tuned 2026-09-30 against every same-project pair in MC-2 whose rows were
# both open or created within 14 days of each other (1,017 commitments, 111
# loose candidates hand-labelled). At these thresholds 18 of 20 flagged pairs
# were the same obligation (0.90 precision) across 7 projects. Recall is
# deliberately modest: a paraphrase that shares under half its words is
# missed rather than guessed at — a noisy flag teaches people to skip it.
DUP_JACCARD = 0.45      # shared / union content words
DUP_CONTAINMENT = 0.80  # shared / the shorter row's content words ...
DUP_MIN_SHARED = 5      # ... and at least this many words in common
_SHORT_ROW = 15         # rows this short let one differing number decide

_STOP = frozenset(
    "a an the and or of to for in on at by with from into onto about as is "
    "are be been being was were this that these those it its their our your "
    "we they them us you i he she his her my me who what which will would "
    "should could can may might must shall do does did done not no any all "
    "each every some more most so if then than but also just via per re up "
    "out over under after before new next".split()
)
# Annotations the ingest writers append — provenance, not the obligation.
_ANNOTATION = re.compile(
    r"\[(?:confidence|routed from|off-project\?|owner unresolved)[^\]]*\]",
    re.IGNORECASE,
)
# `(from Tara Haney (SAP Concur))` — only as the TRAILING attribution; a
# mid-sentence `(from $40k, ~4–5 wks)` is content.
_ATTRIBUTION = re.compile(r"\(from [^()]*(?:\([^()]*\)[^()]*)*\)\s*$", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9$]+(?:'[a-z]+)?")


def _stem(t: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[: -len(suf)]
    return t


def _content_words(text: str) -> frozenset[str]:
    text = _ATTRIBUTION.sub(" ", _ANNOTATION.sub(" ", text.lower()).rstrip())
    return frozenset(
        _stem(t) for t in _WORD.findall(text) if t not in _STOP and len(t) > 1
    )


def _numbers_conflict(a: frozenset[str], b: frozenset[str]) -> bool:
    """Each side carries a number the other lacks: `Deck r1`/`Deck r2`,
    `Invoice #1`/`Invoice #2`, `$40k`/`$30k`. Decisive on a short row and
    for money at any length; a long row's stray date is not."""
    da = {t for t in a if any(ch.isdigit() for ch in t)}
    db = {t for t in b if any(ch.isdigit() for ch in t)}
    if not (da - db and db - da):
        return False
    if min(len(a), len(b)) <= _SHORT_ROW:
        return True
    ma = {t for t in da if t.startswith("$")}
    mb = {t for t in db if t.startswith("$")}
    return bool(ma - mb and mb - ma)


@dataclass
class DuplicatePair:
    a: SweepRow
    b: SweepRow
    jaccard: float
    same_meeting: bool  # both rows carry one `source_meeting_id`


def similarity(a: str, b: str) -> tuple[float, float, int] | None:
    """(jaccard, containment, shared) over content words, or None when
    either side has none. Exposed for tuning and tests."""
    wa, wb = _content_words(a), _content_words(b)
    if not wa or not wb:
        return None
    shared = len(wa & wb)
    return shared / len(wa | wb), shared / min(len(wa), len(wb)), shared


def is_likely_duplicate(a: str, b: str) -> bool:
    sim = similarity(a, b)
    if sim is None:
        return False
    jac, cont, shared = sim
    if not (jac >= DUP_JACCARD or (cont >= DUP_CONTAINMENT and shared >= DUP_MIN_SHARED)):
        return False
    return not _numbers_conflict(_content_words(a), _content_words(b))


def likely_duplicates(rows: list[SweepRow]) -> list[DuplicatePair]:
    """Pairs within ONE project's rows that read as the same obligation,
    most similar first. Callers pass a single group — pairing never
    crosses projects."""
    pairs: list[DuplicatePair] = []
    for i, a in enumerate(rows):
        for b in rows[i + 1 :]:
            if not is_likely_duplicate(a.description, b.description):
                continue
            jac = similarity(a.description, b.description)[0]  # type: ignore[index]
            pairs.append(
                DuplicatePair(
                    a=a,
                    b=b,
                    jaccard=round(jac, 2),
                    same_meeting=bool(
                        a.source_meeting_id
                        and a.source_meeting_id == b.source_meeting_id
                    ),
                )
            )
    pairs.sort(key=lambda p: -p.jaccard)
    return pairs


def _clip(text: str, n: int = 70) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def render_sweep(groups: dict[str, list[SweepRow]], *, today: date) -> str:
    """The review surface: one block per project, decision fields inline."""
    if not groups:
        return "No open commitments match."
    out: list[str] = []
    for code, rows in groups.items():
        out.append(f"{code} — {len(rows)} open")
        out.append("")
        for r in rows:
            if r.undated:
                head = f"  ⚠ UNDATED · {r.age_days}d"
            elif r.due_date < today:
                overdue = (today - r.due_date).days
                head = f"    SLIPPED · due {r.due_date.isoformat()} ({overdue}d ago)"
            else:
                head = f"    due {r.due_date.isoformat()} [{r.date_status}]"
            head += f"  [{r.source_kind}]  {r.owner}"
            if r.ttl == "expire":
                head += "  ← past TTL, expires next dates loop"
            elif r.ttl == "warn":
                head += f"  ← expires at {_EXPIRE_AFTER_DAYS}d unless dated"
            out.append(head)
            out.append(f"    {r.description}")
        dups = likely_duplicates(rows)
        if dups:
            out.append("")
            out.append(f"  ≈ {len(dups)} likely duplicate pair(s) — resolve the redundant row:")
            for p in dups:
                tag = " · same meeting" if p.same_meeting else ""
                out.append(
                    f"    {p.a.id[:8]} ↔ {p.b.id[:8]}  ({p.jaccard:.2f}{tag})"
                )
                out.append(f"      {_clip(p.a.description)}")
                out.append(f"      {_clip(p.b.description)}")
        out.append("")
    total = sum(len(rs) for rs in groups.values())
    stale = sum(1 for rs in groups.values() for r in rs if r.stale)
    ndup = sum(len(likely_duplicates(rs)) for rs in groups.values())
    out.append(
        f"{total} open across {len(groups)} project(s) · {stale} stale "
        f"(undated ≥{STALE_DAYS}d) · {ndup} likely duplicate pair(s)"
    )
    return "\n".join(out)
