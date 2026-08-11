"""``cp seal-sweep`` — ask what a shipped round consumed (#175).

`absorbed_by` had fired TWICE in the tenant's history when this was written.
That is not a discipline failure: nothing ever asked the question. `wrap up`
checks word counts, spine lint, and commitments — never "this deliverable
shipped a new version; what did it consume?"

So the spine only ever accumulated. 242 live elements tenant-wide, 9 absorbed.

WHAT THIS IS NOT
----------------
Not an eligibility gate. `seal_to_deliverable` takes an explicit
`absorbed_keys` list and applies no eligibility check — any element can be
sealed today, edge or no edge. The original #174 framing ("86% of the spine
can never be absorbed") was wrong about the mechanism; see that issue's
2026-08-11 correction. What was missing was the ASKING, which is this module.

Read-only, and deliberately so. Sealing is a compression event: it moves
elements out of the default read. Getting that wrong silently hides live
work, so the sweep ranks candidates and renders the decision fields — a
human runs `seal_to_deliverable` on what survives review. Same contract as
`commitments_sweep`: make the decision cheap, never automatic.

CANDIDATE SIGNAL
----------------
Three edge kinds are consumption evidence, and the third is easy to miss:

  - `derives_from` — built from this. The strongest claim.
  - `informs`      — shaped it without generating it.
  - `responds_to`  — feedback the version answered. On ibx-5192 this is
                     4 of the 12 edges into the two live decks; a sweep
                     that ignored it would miss every feedback card the
                     round closed, which is most of what a round consumes.

Elements already absorbed anywhere are excluded — absorption is not
re-proposed. So is the deliverable itself, and anything that still feeds a
DIFFERENT deliverable with no shipped version (#164 step 2: don't close an
input another open round is still living on).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

# Edge kinds that count as "this element fed that deliverable".
FEEDS_KINDS = ("derives_from", "informs", "responds_to")

# How strongly each kind argues for absorption, highest first. Used only for
# ordering the review list — every candidate is shown regardless.
_KIND_WEIGHT = {"derives_from": 3, "informs": 2, "responds_to": 1}

# A deliverable is "recently shipped" within this window. Wide enough that a
# wrap-up a few days after delivery still catches the round.
DEFAULT_SHIPPED_WITHIN_DAYS = 14

# Layers whose elements are deliverables. Mirrors the #172 fold: `Output` was
# an alias that mig 137 collapsed into `Deliverables`, but historical rows and
# any un-migrated tenant can still carry it, so both are accepted here.
DELIVERABLE_LAYERS = ("deliverables", "output")


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def _as_date(value: Any) -> date | None:
    """Parse a date or timestamp column; None when absent or malformed."""
    if not value:
        return None
    text = str(value)
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


@dataclass
class Candidate:
    """One element a shipped deliverable plausibly consumed."""

    est_item_id: str
    framing: str
    layer: str
    kinds: list[str]

    @property
    def weight(self) -> int:
        return max((_KIND_WEIGHT.get(k, 0) for k in self.kinds), default=0)

    @property
    def evidence(self) -> str:
        return "+".join(sorted(self.kinds, key=lambda k: -_KIND_WEIGHT.get(k, 0)))


@dataclass
class Round:
    """A deliverable that shipped a version, and what it plausibly consumed."""

    est_item_id: str
    framing: str
    version_label: str
    version_date: date | None
    candidates: list[Candidate] = field(default_factory=list)
    already_absorbed: int = 0


def _feeds_index(
    relations: Iterable[dict],
) -> tuple[dict[str, dict[str, list[str]]], set[str]]:
    """(deliverable -> {element -> [kinds]}, elements already absorbed).

    One pass over the relation rows, because the sweep needs both directions
    of the same table and re-reading it per deliverable would be N queries
    for a question that is one join.
    """
    into: dict[str, dict[str, list[str]]] = {}
    absorbed: set[str] = set()
    for edge in relations:
        kind = edge.get("kind")
        src = edge.get("from_item_id")
        dst = edge.get("to_item_id")
        if not src or not dst:
            continue
        if kind == "absorbed_by":
            absorbed.add(src)
        elif kind in FEEDS_KINDS:
            into.setdefault(dst, {}).setdefault(src, []).append(kind)
    return into, absorbed


def build_rounds(
    rows: list[dict],
    relations: list[dict],
    *,
    today: date | None = None,
    within_days: int = DEFAULT_SHIPPED_WITHIN_DAYS,
    all_deliverables: bool = False,
) -> list[Round]:
    """Shipped deliverables and their unabsorbed inputs, newest first.

    `rows` are live spine_substance rows (SEAL_SWEEP_COLUMNS shape);
    `relations` are that project's active relation rows. Pure — no I/O — so
    the ranking is testable without a database.

    `all_deliverables=True` drops the recency window, for a first pass over a
    project that has never been swept.
    """
    today = today or date.today()
    by_id = {r.get("est_item_id"): r for r in rows if r.get("est_item_id")}
    into, absorbed = _feeds_index(relations)

    # An input still feeding a deliverable that has NOT shipped is live work
    # for that other round — never propose closing it (#164 step 2).
    shipped_ids = {
        eid
        for eid, r in by_id.items()
        if _norm(r.get("layer")) in DELIVERABLE_LAYERS and r.get("version_label")
    }
    feeding_unshipped: set[str] = set()
    for dst, sources in into.items():
        if dst in by_id and dst not in shipped_ids:
            feeding_unshipped.update(sources)

    rounds: list[Round] = []
    for eid, row in by_id.items():
        if _norm(row.get("layer")) not in DELIVERABLE_LAYERS:
            continue
        vdate = _as_date(row.get("version_date"))
        if not all_deliverables:
            if vdate is None or (today - vdate).days > within_days:
                continue

        sources = into.get(eid, {})
        candidates = [
            Candidate(
                est_item_id=src,
                framing=by_id[src].get("framing") or src,
                layer=by_id[src].get("layer") or "—",
                kinds=sorted(set(kinds)),
            )
            for src, kinds in sources.items()
            # Absorbed already, gone from the live set, the deliverable
            # itself, or load-bearing for another open round.
            if src not in absorbed
            and src in by_id
            and src != eid
            and src not in feeding_unshipped
        ]
        candidates.sort(key=lambda c: (-c.weight, c.framing.lower()))
        rounds.append(
            Round(
                est_item_id=eid,
                framing=row.get("framing") or eid,
                version_label=row.get("version_label") or "—",
                version_date=vdate,
                candidates=candidates,
                already_absorbed=sum(1 for s in sources if s in absorbed),
            )
        )

    rounds.sort(key=lambda r: (r.version_date or date.min), reverse=True)
    return rounds


def render_sweep(rounds: list[Round], *, code: str) -> str:
    """The review surface: one block per shipped round, seal command inline."""
    if not rounds:
        return (
            f"{code} — no deliverable shipped a version in the window. "
            "Nothing to seal.\n"
            "(Use --all to sweep every deliverable regardless of date.)"
        )

    out: list[str] = []
    total = 0
    for r in rounds:
        when = r.version_date.isoformat() if r.version_date else "undated"
        head = f"{r.framing} · {r.version_label} ({when})"
        out.append(head)
        if r.already_absorbed:
            out.append(f"  {r.already_absorbed} input(s) already absorbed.")
        if not r.candidates:
            # "Already compressed" and "nothing ever recorded what fed this"
            # are opposite states that both produce an empty candidate list.
            # Reporting the reassuring one for both is how a project with no
            # edges reads as a project that is fully swept — the exact
            # mistake this sweep exists to stop making.
            if r.already_absorbed:
                out.append(
                    "  No unabsorbed inputs — this round is already compressed."
                )
            else:
                out.append(
                    "  ⚠ No edges into this deliverable — nothing records what "
                    "fed it, so the sweep cannot tell what it consumed. Wire "
                    "informs/derives_from/responds_to (#174), then re-run."
                )
            out.append("")
            continue
        total += len(r.candidates)
        out.append(f"  {len(r.candidates)} input(s) this version plausibly consumed:")
        for c in r.candidates:
            out.append(f"    [{c.evidence}] {c.framing}  ({c.layer})")
            out.append(f"      {c.est_item_id}")
        out.append("")
        out.append("  Seal what survives review:")
        keys = ", ".join(f'"{c.est_item_id}"' for c in r.candidates)
        out.append(
            f'    seal_to_deliverable(project_code="{code}", '
            f'deliverable_key="{r.est_item_id}", absorbed_keys=[{keys}])'
        )
        out.append("")

    blind = sum(1 for r in rounds if not r.candidates and not r.already_absorbed)
    out.append(
        f"{len(rounds)} shipped round(s) · {total} candidate input(s). "
        "Absorbed is not archived: sealed elements stay one hop behind their "
        "deliverable, and `include_absorbed=true` brings them back."
    )
    if blind:
        out.append(
            f"⚠ {blind} of {len(rounds)} round(s) have NO edges recording what "
            "fed them — an empty candidate list there means the sweep is "
            "blind, not that the round is clean."
        )
    return "\n".join(out)
