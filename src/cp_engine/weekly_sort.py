"""The weekly sort — give every unclassified thing a lifetime, or a decision.

WHAT THIS IS FOR
----------------
Drew, 2026-08-15: information has stopped making cp smarter because everything
has one lifetime — forever. Background (always true), feedback (transient,
needs action) and canon (truths we defined) are three different things with
three different half-lives, and the system treats them as one.

This is the ritual that moves things between them. It proposes; a human
confirms. Nothing here writes a classification on its own authority.

WHAT IT OPERATES ON — measured 2026-08-15, not assumed
------------------------------------------------------
Two piles have no working disposition path:

  161 spine rows with lifetime NULL   (mig 140 seeded only the unambiguous)
   70 spine_inbox cards still proposed (never framed, never promoted)

Two piles that LOOK stuck and are not, so this tool leaves them alone:

  commitments  40 open of 523 — 483 dispositioned, only 3 open over 30 days.
               The dates-loop cron is off, but the loop is being worked by
               hand and it is working.
  sprint asks  21 -> 10 -> 4 -> 1 open across W31-W34. Closing fine.

A ritual that reports on healthy systems trains you to skip it. This one only
surfaces what is actually stuck.

CARDS DO NOT GET A LIFETIME
---------------------------
A deliverable or an activity is the WORK. It is not background, not feedback,
and not canon — those describe context ABOUT work. Forcing cards into the
taxonomy would repeat the "26 engagement cards" failure (card_class.py) one
level up: a category applied where it does not belong, producing noise that
buries the real signal. `card_kind in (activity, deliverable, engagement)`
is therefore a valid terminal state, and those 31 rows are excluded from the
queue rather than proposed on.

PROPOSALS ARE STRUCTURAL WHERE POSSIBLE, MODEL-DRIVEN WHERE NOT
---------------------------------------------------------------
Layer already decides some cases (Retrospective is background; a Synthesis
that has been promoted to canon is canon). Those are proposed deterministically
with a stated reason. The genuinely ambiguous ones — 86 Source material rows
that could be background (market data) or feedback (a client's marked-up deck)
— carry a reason of "needs judgement" and are handed to the caller, which is
where the master prompt does its work.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cp_engine.mc2_db import Tables, get_client

_SUBSTANCE_COLUMNS = (
    "id, project_code, est_item_id, layer, card_kind, lifetime, framing, "
    "version_date, serves"
)
_INBOX_COLUMNS = "id, project_code, source_ref, guessed_type, status, created_at"

# Kinds that ARE work. Work has no lifetime — see the module docstring.
_WORK_KINDS = frozenset({"activity", "deliverable", "engagement"})

# Layers whose lifetime follows from the layer alone. Deliberately short: a
# rule that fires on everything is a rule that has stopped discriminating.
_STRUCTURAL_LIFETIME: dict[str, tuple[str, str]] = {
    # layer (lowercased) -> (lifetime, why)
    "retrospective": ("background", "a retrospective is durable, not pending"),
    "research": ("background", "research is standing context"),
    "stakeholders": ("background", "who someone is changes rarely"),
    "brief": ("canon", "the standing brief is what we defined"),
    "agreement": ("canon", "the agreement is what we committed to"),
    "decisions": ("canon", "a ruling is what we defined"),
    "client feedback": ("feedback", "point-in-time reaction"),
    "email": ("feedback", "point-in-time, whatever it carries"),
    "note": ("feedback", "point-in-time working material"),
}


@dataclass
class Proposal:
    """One row, one proposed lifetime, one stated reason."""

    row_id: str
    project_code: str
    framing: str
    layer: str | None
    proposed: str | None  # None = needs human/model judgement
    why: str


@dataclass
class SortQueue:
    """Everything the sort found, grouped by what it can and cannot decide."""

    structural: list[Proposal] = field(default_factory=list)
    needs_judgement: list[Proposal] = field(default_factory=list)
    inbox: list[dict] = field(default_factory=list)
    work_excluded: int = 0

    @property
    def total(self) -> int:
        return len(self.structural) + len(self.needs_judgement) + len(self.inbox)


def propose_lifetime(row: dict) -> Proposal | None:
    """A lifetime for one row, or None if the row needs no decision.

    Pure. Returns a Proposal with `proposed=None` when the answer needs
    judgement — that is a queue entry, not a skip.
    """
    if (row.get("lifetime") or "").strip():
        return None  # already decided
    if (row.get("card_kind") or "").strip() in _WORK_KINDS:
        return None  # work has no lifetime

    layer = (row.get("layer") or "").strip().lower()
    framing = row.get("framing") or "(no framing)"

    hit = _STRUCTURAL_LIFETIME.get(layer)
    if hit:
        lifetime, why = hit
        return Proposal(row["id"], row.get("project_code", ""), framing,
                        row.get("layer"), lifetime, why)

    return Proposal(
        row["id"], row.get("project_code", ""), framing, row.get("layer"),
        None,
        f"{row.get('layer') or 'no layer'} can be background or feedback — "
        "read it to decide",
    )


def build_queue(rows: list[dict], inbox: list[dict]) -> SortQueue:
    """Split the week's unclassified material into decidable and not."""
    q = SortQueue(inbox=list(inbox))
    for row in rows:
        if (row.get("card_kind") or "").strip() in _WORK_KINDS and not (
            row.get("lifetime") or ""
        ).strip():
            q.work_excluded += 1
            continue
        p = propose_lifetime(row)
        if p is None:
            continue
        (q.structural if p.proposed else q.needs_judgement).append(p)
    return q


def fetch(client, *, project_code: str | None = None) -> tuple[list, list]:
    """Unclassified live substance rows + still-proposed inbox cards."""
    sq = (
        client.table(Tables.SPINE_SUBSTANCE)
        .select(_SUBSTANCE_COLUMNS)
        .eq("status", "live")
        .eq("archived", False)
        .is_("lifetime", "null")
    )
    iq = (
        client.table(Tables.SPINE_INBOX)
        .select(_INBOX_COLUMNS)
        .eq("status", "proposed")
    )
    if project_code:
        sq = sq.like("project_code", f"{project_code}%")
        iq = iq.like("project_code", f"{project_code}%")
    return (sq.execute().data or [], iq.execute().data or [])


def apply_structural(client, proposals: list[Proposal]) -> int:
    """Write only the structurally-decided lifetimes. Returns rows written.

    Targeted single-column updates: `spine_substance`'s mig-092 guard rejects
    changes to body/status/origin without a writer header, and a bulk upsert
    would need every NOT NULL column. One column, one row, no clobber risk.
    """
    written = 0
    for p in proposals:
        if not p.proposed:
            continue
        client.table(Tables.SPINE_SUBSTANCE).update(
            {"lifetime": p.proposed}
        ).eq("id", p.row_id).execute()
        written += 1
    return written


def attach_proposals(queue: SortQueue, rows: list[dict], *, llm) -> int:
    """Ask the model for a lifetime on everything structure could not decide.

    Fills `Proposal.proposed` in place for the `needs_judgement` list, moving
    nothing between lists: a model proposal is still a proposal, and the human
    pass is what turns it into a lifetime. An item the model returns `unsure`
    for keeps `proposed=None` and reaches the human blank, which is honest.

    Returns the number of items that came back with a usable lifetime.
    """
    from cp_engine.sort_propose import propose

    if not queue.needs_judgement:
        return 0

    wanted = {p.row_id for p in queue.needs_judgement}
    items = [r for r in rows if r["id"] in wanted]
    by_id = {p.row_id: p for p in queue.needs_judgement}

    filled = 0
    for prop in propose(items, llm=llm):
        target = by_id.get(prop.row_id)
        if target is None or prop.lifetime == "unsure":
            continue
        target.proposed = prop.lifetime
        target.why = f"proposed: {prop.why}" if prop.why else "proposed by model"
        filled += 1
    return filled


def run(
    *,
    config=None,
    project_code: str | None = None,
    apply: bool = False,
    llm=None,
) -> tuple[SortQueue, int, int]:
    """Fetch, queue, optionally propose, optionally write.

    Returns (queue, rows_written, proposals_filled). Only STRUCTURAL decisions
    are ever written — a model proposal reaches the database solely through a
    human confirming it, so `--apply` and `--propose` are independent.
    """
    client = get_client(config, required=True)
    rows, inbox = fetch(client, project_code=project_code)
    queue = build_queue(rows, inbox)

    proposed = attach_proposals(queue, rows, llm=llm) if llm else 0
    written = apply_structural(client, queue.structural) if apply else 0
    return queue, written, proposed
