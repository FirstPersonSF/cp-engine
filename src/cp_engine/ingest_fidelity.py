"""Decision fidelity on an auto-ingest plan (#321).

Two defects put a wrong decision into `### Decisions`, which agendas and
sprint prep read as settled:

1. **A deliberation recorded as a decision.** slt-5196, 2026-08-19: "shoot
   moves to Oct 15–16 CAB week" — Leah raised the option and backed off it
   in the same breath. The extractor had no other class to put it in.
2. **A decision reversed later in the meeting, recorded at its first
   value.** ibx-5153, 2026-09-15 (`9afe70ec`): "only present negative-framed
   versions" — reversed seven minutes later, with no trace of the reversal.

The prompt now has an `open_questions` class and asks for the meeting's
FINAL position (with `earlier_position` when it moved). This module is the
deterministic half, run on the plan the model returns:

- a decision whose own wording says it is unsettled ("leaning toward",
  "still open", "debating whether", "TBD", a trailing "?") is moved to
  `open-questions` — under-claiming is the safe direction;
- two decisions in one project block that are the same topic (content-word
  Jaccard >= `SUPERSEDE_JACCARD`) are one decision stated twice: the LATER
  one is kept (plans list items in transcript order) and marked revised;
- `earlier_position` on a decision is rendered into the text, so the
  reversal is visible on the bullet rather than lost.

Thresholds were tuned against the tenant's real ingests (see the #321
commit message): the deliberation pattern flags 9 of 440 recorded
decisions, each carrying an explicit unsettled clause; no two decisions
from one real meeting reach the supersede threshold (max 0.30).
"""

from __future__ import annotations

import re

from cp_engine.text_similarity import jaccard

OPEN_QUESTION_VERB = "open-questions"
SUPERSEDE_JACCARD = 0.5
_EARLIER_MAX = 140

_DECISION_VERBS = ("decisions", "decision", "add-decision")
_OPEN_Q_VERBS = (
    "open-questions", "open_questions", "open-question", "open_question",
    "questions", "record-open-question",
)

DELIBERATION_RE = re.compile(
    r"\?\s*$|\b(?:should we|could we|shall we|maybe we|perhaps we"
    r"|leaning (?:toward|towards|to)"
    r"|(?:debating|discussing|deciding|decide later|to discuss|to decide"
    r"|weighing|exploring|undecided on) whether"
    r"|still (?:open|undecided|deciding|discussing|debating|tbd)"
    r"|not (?:yet )?(?:decided|agreed|locked|final)"
    r"|tbd|to be decided|undecided|no decision(?: yet)?)\b",
    re.IGNORECASE,
)


def is_deliberation(text: str) -> bool:
    return bool(DELIBERATION_RE.search(text or ""))


def _with_earlier(item: dict) -> dict:
    earlier = item.pop("earlier_position", None) or item.pop("reversed_from", None)
    earlier = " ".join(str(earlier or "").split())
    if earlier:
        if len(earlier) > _EARLIER_MAX:
            earlier = earlier[: _EARLIER_MAX - 1].rstrip() + "…"
        item["text"] = (
            f"{str(item.get('text') or '').rstrip()} "
            f"(revised in-meeting; earlier: \"{earlier}\")"
        )
    return item


def _collapse_restatements(decisions: list[dict]) -> tuple[list[dict], int]:
    """Keep the last of each same-topic run; the dropped earlier text rides
    along as `earlier_position` unless the model already supplied one."""
    kept: list[dict] = []
    collapsed = 0
    for item in decisions:
        text = str(item.get("text") or "")
        match = next(
            (i for i, k in enumerate(kept)
             if jaccard(str(k.get("text") or ""), text) >= SUPERSEDE_JACCARD),
            None,
        )
        if match is None:
            kept.append(item)
            continue
        earlier = kept.pop(match)
        item.setdefault("earlier_position", earlier.get("text"))
        kept.append(item)
        collapsed += 1
    return kept, collapsed


def apply_decision_fidelity(plan: dict) -> dict:
    """Mutate `plan` in place; return `{demoted, collapsed, revised}`."""
    summary = {"demoted": 0, "collapsed": 0, "revised": 0}
    projects = plan.get("projects") or {}
    for code, entries in projects.items():
        if not isinstance(entries, dict):
            continue
        # One canonical open-questions list per block.
        questions: list = []
        for verb in [v for v in entries if v in _OPEN_Q_VERBS]:
            items = entries.pop(verb)
            if isinstance(items, list):
                questions.extend(i for i in items if isinstance(i, dict))
        for verb in [v for v in entries if v in _DECISION_VERBS]:
            items = entries.get(verb)
            if not isinstance(items, list):
                continue
            decisions = [i for i in items if isinstance(i, dict)]
            settled: list[dict] = []
            for item in decisions:
                if is_deliberation(str(item.get("text") or "")):
                    item.pop("cross_cutting", None)
                    questions.append(item)
                    summary["demoted"] += 1
                else:
                    settled.append(item)
            settled, n = _collapse_restatements(settled)
            summary["collapsed"] += n
            for item in settled:
                if item.get("earlier_position") or item.get("reversed_from"):
                    summary["revised"] += 1
                _with_earlier(item)
            if settled:
                entries[verb] = settled
            else:
                del entries[verb]
        if questions:
            for q in questions:
                q.pop("earlier_position", None)
                q.pop("reversed_from", None)
            entries[OPEN_QUESTION_VERB] = questions

    # Account-level decisions land on a node's cp.md `## Decisions`. An
    # unsettled one goes to the node's sprint file as an open question; a
    # sprint-planning scope has no node, so it is dropped (with a count).
    kept_ad: list = []
    for item in plan.get("account_decisions") or []:
        if isinstance(item, dict) and is_deliberation(str(item.get("text") or "")):
            node = item.get("code")
            summary["demoted"] += 1
            if node:
                q = {k: v for k, v in item.items() if k in ("text", "date")}
                projects = plan.setdefault("projects", {})
                projects.setdefault(node, {}).setdefault(OPEN_QUESTION_VERB, []).append(q)
            continue
        if isinstance(item, dict):
            _with_earlier(item)
        kept_ad.append(item)
    if "account_decisions" in plan:
        plan["account_decisions"] = kept_ad
    return summary
