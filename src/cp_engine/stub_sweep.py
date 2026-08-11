"""``cp stub-sweep`` — empty Source-material cards, and where their provenance belongs (#178).

82 of the tenant's live elements are Source-material cards under 200 chars.
Their bodies are boilerplate the ingest wrote:

    Ingested document: **Marcello Grande** (doc)

    rag_asset: 1fb5e23e-0cfe-4d85-87e8-903d46c48a33

and the SAME rag_asset is already in the card's `sources` array. The card is a
wrapper around a pointer it duplicates. Provenance has a home — the `sources`
array, surfaced as "Documents & sources" — and a document that has been read
should be a SOURCE on the card that used it, not a peer card beside it.

WHAT THE ISSUE GOT WRONG, AND WHY THIS IS NOT A DELETE
------------------------------------------------------
#178 says "attach where they informed something, retire where they informed
nothing". But **79 of the 82 carry a `serves` binding** — the routing
judgement naming which activity or slot the document belongs to. That is real
human input, and it is what `seal_sweep`'s chain walk reads to find what fed a
deliverable. Collapsing the cards naively would throw all of it away.

So the move is a TRANSFER, not a deletion: attach the stub's rag_asset to the
element it serves, then retire the stub. The provenance ends up on the card
that actually used the document, which is what the issue wanted.

That only works when the `serves` target IS a spine element. Measured
2026-08-11: 65 stubs serve a real element (attachable), 14 serve a bare
estimate slot with nothing to attach to. Those are REPORTED, never guessed at
— inventing a target would be the same mistake as manufacturing a feeds edge.

Read-only, like every sweep here. Retiring a card and moving its provenance is
a real mutation; this makes the decision cheap and leaves it to a human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Bodies this short on Source material are the ingest's boilerplate, not a
# thought. The observed range is 93–~200 chars; the threshold matches #178's
# measurement so the two agree on what "stub" means.
STUB_BODY_MAX = 200

# The exact shape ingest writes. A card whose body is ONLY this (plus its
# rag_asset line) carries nothing a human added — if someone has written into
# it, the extra prose pushes it over STUB_BODY_MAX and it is not a stub.
_BOILERPLATE_RE = re.compile(
    r"^\s*ingested\s+(?:document|file|source)\s*:", re.IGNORECASE)

SOURCE_LAYERS = ("source material", "sourcematerial", "source")


def _norm(v: Any) -> str:
    return str(v or "").strip().lower()


@dataclass
class Stub:
    """An empty Source-material card and the element its provenance belongs on."""

    est_item_id: str
    framing: str
    body_len: int
    sources: list[dict] = field(default_factory=list)
    # Resolved `serves` targets that ARE live spine elements:
    # (id, framing, target_is_sound).
    targets: list[tuple[str, str, bool]] = field(default_factory=list)
    # `serves` entries that resolve to nothing — bare estimate slots.
    unresolved: list[str] = field(default_factory=list)
    has_edges: bool = False

    @property
    def unsound_targets(self) -> list[tuple[str, str, bool]]:
        """Targets that spine-lint would flag — unlayered or misfiled.

        Measured 2026-08-11: 34 of 65 attachable stubs route to a card with
        `layer: null`. Moving real provenance onto a broken card buries it,
        so those are named rather than silently proposed.
        """
        return [t for t in self.targets if not t[2]]

    @property
    def attachable(self) -> bool:
        """Its provenance has somewhere to go."""
        return bool(self.targets) and bool(self.sources)

    @property
    def orphan(self) -> bool:
        """Routed nowhere resolvable — nothing to attach to."""
        return not self.targets


def find_stubs(
    rows: list[dict],
    relations: list[dict] | None = None,
    *,
    body_max: int = STUB_BODY_MAX,
) -> list[Stub]:
    """Empty Source-material cards with their resolved `serves` targets.

    `rows` are live spine_substance rows (STUB_SWEEP_COLUMNS shape). Pure —
    no I/O — so the classification is testable without a database.
    """
    relations = relations or []
    by_id = {r.get("est_item_id"): r for r in rows if r.get("est_item_id")}

    edged: set[str] = set()
    for e in relations:
        for side in ("from_item_id", "to_item_id"):
            if e.get(side):
                edged.add(e[side])

    out: list[Stub] = []
    for row in rows:
        eid = row.get("est_item_id")
        if not eid or _norm(row.get("layer")) not in SOURCE_LAYERS:
            continue
        body = row.get("body") or ""
        if len(body) > body_max:
            continue
        # Only the ingest's own boilerplate qualifies. A short hand-written
        # note is thin, but it is somebody's thought — not this sweep's call.
        if body and not _BOILERPLATE_RE.match(body):
            continue

        targets: list[tuple[str, str, bool]] = []
        unresolved: list[str] = []
        for slot in row.get("serves") or []:
            target = by_id.get(slot)
            if target is not None and slot != eid:
                # A target with no layer is one spine-lint already flags as
                # unfilable. Provenance moved onto it is provenance buried.
                sound = target.get("layer") is not None
                targets.append((slot, target.get("framing") or slot, sound))
            else:
                unresolved.append(slot)

        out.append(
            Stub(
                est_item_id=eid,
                framing=row.get("framing") or eid,
                body_len=len(body),
                sources=list(row.get("sources") or []),
                targets=targets,
                unresolved=unresolved,
                has_edges=eid in edged,
            )
        )

    out.sort(key=lambda s: (s.orphan, s.framing.lower()))
    return out


def render_sweep(stubs: list[Stub], *, code: str) -> str:
    """The review surface: what to move where, and what cannot move."""
    if not stubs:
        return f"{code} — no empty Source-material cards."

    attachable = [s for s in stubs if s.attachable]
    orphans = [s for s in stubs if s.orphan]
    edged = [s for s in stubs if s.has_edges]

    out: list[str] = []
    if attachable:
        out.append(
            f"{len(attachable)} stub(s) whose provenance has somewhere to go — "
            "attach the source to what it served, then retire the card:")
        out.append("")
        for s in attachable:
            titles = ", ".join(
                str(src.get("title") or src.get("id"))
                for src in s.sources if isinstance(src, dict)) or "(no title)"
            out.append(f"  {s.framing}  ({s.body_len} chars)")
            out.append(f"    {s.est_item_id}")
            out.append(f"    source: {titles}")
            for tid, tname, sound in s.targets:
                flag = "" if sound else "  ⚠ UNLAYERED TARGET"
                out.append(f"    → serves: {tname}{flag}")
                out.append(f"        {tid}")
            if s.has_edges:
                out.append("    ⚠ has typed edges — retiring cascades them; "
                           "check what points here first")
        out.append("")

        blocked = [s for s in attachable if s.unsound_targets]
        if blocked:
            out.append(
                f"  ⚠ {len(blocked)} of these route to an UNLAYERED card — "
                "one spine-lint already flags as unfilable. Moving real "
                "provenance onto a broken card buries it. Fix the "
                "destination's layer first (#177), then migrate.")
            out.append("")

    if orphans:
        out.append(
            f"{len(orphans)} stub(s) routed to a bare estimate slot — nothing "
            "to attach to, so nothing is proposed:")
        out.append("")
        for s in orphans:
            where = (f"serves {', '.join(s.unresolved)}" if s.unresolved
                     else "serves nothing")
            out.append(f"  {s.framing} ({where})")
            out.append(f"    {s.est_item_id}")
        out.append("")
        out.append(
            "  These need a judgement the data cannot make: either the slot "
            "should be a real element, or the document belongs on a card that "
            "already exists. Inventing a target would be a guess.")
        out.append("")

    out.append(
        f"{len(stubs)} empty Source-material card(s) · {len(attachable)} "
        f"attachable · {len(orphans)} orphaned"
        + (f" · {len(edged)} carry typed edges" if edged else ""))
    out.append(
        "Read-only. Move a source with `add_element_source` on `cp-hosted`, "
        "then retire the stub — the card is a wrapper around a pointer it "
        "already duplicates, but its `serves` routing is real, so transfer "
        "before you retire.")
    return "\n".join(out)
