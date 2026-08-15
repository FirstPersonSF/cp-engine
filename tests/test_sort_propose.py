"""The pre-pass proposes; it never decides, and never invents.

The load-bearing properties are all negative: a malformed response, an
unknown id, an unrecognised lifetime, or a failed batch must leave the item
UNPROPOSED and reach the human blank. Filling a gap with a plausible guess is
the one outcome that would make the whole surface untrustworthy — a human
confirming a fabricated proposal is worse than a human facing a blank field.
"""

from __future__ import annotations

from cp_engine.sort_propose import (
    Proposed,
    build_prompt,
    parse_response,
    propose,
)
from cp_engine.weekly_sort import Proposal, SortQueue, attach_proposals


def _item(row_id="p/a/v1", **kw) -> dict:
    base = {
        "id": row_id,
        "project_code": "ibx-5153-ai-campaign",
        "layer": "Source material",
        "framing": "Carol's 'Our AI Story' narrative prose",
        "body": "Our AI memo (board-approved).",
        "version_date": "2026-06-20",
    }
    base.update(kw)
    return base


# ── the prompt ────────────────────────────────────────────────────────


def test_prompt_carries_the_signal_a_thin_card_has():
    """82 of 129 rows are pointer cards whose filename IS the content."""
    p = build_prompt([_item(framing="260807-Email-thread-Costco.docx", body=None)])
    assert "260807-Email-thread-Costco.docx" in p
    assert "body: (empty)" in p


def test_long_bodies_are_clipped_and_marked():
    p = build_prompt([_item(body="x" * 5000)])
    assert "[…]" in p
    assert len(p) < 3000


def test_prompt_states_the_output_contract_and_the_escape_hatch():
    p = build_prompt([_item()])
    assert "unsure" in p
    assert "JSON array" in p


def test_prompt_does_not_define_canon():
    """Judgment priors come from the master prompt via system=, not from here.

    This module supplies the TASK. If it started defining whose voice
    outranks whose, the priors would stop being the single editable home for
    that and we would be back to prompts nobody can see.
    """
    p = build_prompt([_item()])
    assert "partner" not in p.lower()
    assert "Drew" not in p


# ── parsing: every failure drops the item ─────────────────────────────


def test_parses_a_clean_response():
    batch = [_item("a"), _item("b")]
    out = parse_response(
        '[{"id":"a","lifetime":"canon","why":"board-approved position"},'
        '{"id":"b","lifetime":"background","why":"reference"}]',
        batch,
    )
    assert [(p.row_id, p.lifetime) for p in out] == [
        ("a", "canon"),
        ("b", "background"),
    ]


def test_tolerates_a_fence_even_though_the_contract_forbids_one():
    out = parse_response(
        '```json\n[{"id":"a","lifetime":"feedback","why":"x"}]\n```', [_item("a")]
    )
    assert len(out) == 1


def test_unknown_id_is_dropped_not_mapped():
    """A hallucinated id must not be attached to a real row."""
    assert parse_response('[{"id":"ghost","lifetime":"canon","why":"x"}]',
                          [_item("a")]) == []


def test_unrecognised_lifetime_is_dropped():
    assert parse_response('[{"id":"a","lifetime":"important","why":"x"}]',
                          [_item("a")]) == []


def test_unparseable_response_yields_nothing():
    for junk in ("", "I could not classify these.", "[{broken", "null"):
        assert parse_response(junk, [_item("a")]) == []


# ── batching ──────────────────────────────────────────────────────────


def test_batches_never_span_projects():
    """One call per project so a batch shares one set of priors."""
    seen: list[str] = []

    def llm(prompt: str) -> str:
        seen.append(prompt)
        return "[]"

    propose(
        [_item("a", project_code="p1"), _item("b", project_code="p2")],
        llm=llm,
    )
    assert len(seen) == 2
    assert not (("p1" in seen[0]) and ("p2" in seen[0]))


def test_a_failing_batch_does_not_sink_the_run():
    calls = {"n": 0}

    def llm(prompt: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("API down")
        return '[{"id":"b","lifetime":"feedback","why":"ok"}]'

    out = propose(
        [_item("a", project_code="p1"), _item("b", project_code="p2")], llm=llm
    )
    assert [p.row_id for p in out] == ["b"]


# ── attachment to the queue ───────────────────────────────────────────


def _queue_with(row_id: str) -> SortQueue:
    q = SortQueue()
    q.needs_judgement.append(
        Proposal(row_id, "p", "framing", "Source material", None, "needs judgement")
    )
    return q


def test_unsure_leaves_the_item_blank_for_the_human():
    q = _queue_with("a")
    attach_proposals(
        q, [_item("a")],
        llm=lambda _p: '[{"id":"a","lifetime":"unsure","why":"cannot tell"}]',
    )
    assert q.needs_judgement[0].proposed is None


def test_a_proposal_is_marked_as_proposed_not_decided():
    q = _queue_with("a")
    filled = attach_proposals(
        q, [_item("a")],
        llm=lambda _p: '[{"id":"a","lifetime":"canon","why":"board-approved"}]',
    )
    assert filled == 1
    p = q.needs_judgement[0]
    assert p.proposed == "canon"
    assert p.why.startswith("proposed:")


def test_proposals_never_move_an_item_into_the_structural_list():
    """Structural means the DB will be written. A proposal must never get there."""
    q = _queue_with("a")
    attach_proposals(
        q, [_item("a")],
        llm=lambda _p: '[{"id":"a","lifetime":"canon","why":"x"}]',
    )
    assert q.structural == []
