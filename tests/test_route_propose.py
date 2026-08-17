"""The routing pre-pass proposes a DESTINATION, and must never invent one.

Companion to `test_sort_propose`. The lifetime pass answers with one of three
fixed words, so a bad answer is trivially detectable. A destination is an
`est_item_id` from one project's estimate — free-form, per-project, and a
hallucinated id looks exactly like a real one. Validating against the project's
own slot list is the safety property this module exists for.
"""

from cp_engine.route_propose import (
    propose,
    ProposedRoute,
    build_prompt,
    parse_response,
)

SLOTS = [
    {"id": "est-1", "name": "Kickoff & Materials Transfer", "phase": "Platform Story", "kind": "activity"},
    {"id": "est-2", "name": "Slide-by-Slide Talk Track", "phase": "Platform Story", "kind": "deliverable"},
]
VALID = {"est-1", "est-2"}

BATCH = [
    {
        "id": "sub-1",
        "framing": "Pillar ruling — 4 platform pillars",
        "layer": "Decisions",
        "lifetime": "canon",
        "body": "Jaime ruled that the deck leads with four pillars.",
    },
    {
        "id": "sub-2",
        "framing": "Marcello's deck feedback",
        "layer": "Client feedback",
        "lifetime": "feedback",
        "body": "Two tracks, got to solution too fast.",
    },
]


def test_prompt_carries_the_slot_list():
    # The model cannot pick from a list it was not given, and the ids must be
    # verbatim or nothing it returns will validate.
    prompt = build_prompt(BATCH, SLOTS)
    assert "`est-1`" in prompt
    assert "Slide-by-Slide Talk Track" in prompt
    assert "sub-1" in prompt


def test_prompt_says_so_when_there_is_nowhere_to_route():
    # A project with no estimate has no slots. An empty list rendered silently
    # could read as an oversight and invite an invented id.
    prompt = build_prompt(BATCH, [])
    assert "no estimate" in prompt


def test_prompt_includes_lifetime_as_context():
    # Whether something is canon or feedback is a real hint about what it can
    # belong to — a decision governs work, feedback responds to it.
    assert "lifetime: canon" in build_prompt(BATCH, SLOTS)


def test_a_valid_slot_is_accepted():
    out = parse_response(
        '[{"id": "sub-1", "slot": "est-2", "confidence": 0.8, "why": "governs the deck"}]',
        BATCH,
        VALID,
    )
    assert out == [ProposedRoute("sub-1", "est-2", "governs the deck", 0.8)]


def test_a_HALLUCINATED_slot_is_dropped():
    # THE POINT OF THIS MODULE. `est-99` is not in this project's estimate. A
    # confident wrong routing is worse than a blank field: nobody re-checks a
    # filled one.
    out = parse_response(
        '[{"id": "sub-1", "slot": "est-99", "confidence": 0.95, "why": "sure"}]',
        BATCH,
        VALID,
    )
    assert out == []


def test_a_hallucinated_slot_is_NOT_coerced_to_unsure():
    # "Named a slot that does not exist" and "declined to answer" are different
    # facts. Only the second is a judgement worth recording; turning the first
    # into the second would launder a hallucination into a considered opinion.
    out = parse_response(
        '[{"id": "sub-1", "slot": "est-99", "confidence": 0.9, "why": "x"},'
        ' {"id": "sub-2", "slot": "unsure", "confidence": 0.3, "why": "y"}]',
        BATCH,
        VALID,
    )
    assert [p.row_id for p in out] == ["sub-2"]
    assert out[0].slot_id == "unsure"


def test_unsure_survives():
    # A declared uncertainty is a real answer and tells you something about the
    # priors. Dropping it would hide which items the model declined.
    out = parse_response(
        '[{"id": "sub-1", "slot": "unsure", "confidence": 0.2, "why": "several"}]',
        BATCH,
        VALID,
    )
    assert out[0].slot_id == "unsure"


def test_an_unknown_item_id_is_dropped():
    out = parse_response(
        '[{"id": "sub-NOPE", "slot": "est-1", "confidence": 0.9, "why": "x"}]',
        BATCH,
        VALID,
    )
    assert out == []


def test_a_bad_confidence_becomes_none_not_zero():
    # None reads as "needs an eye" in the UI; 0.0 would read as maximum
    # certainty that it is wrong, which is a different claim.
    out = parse_response(
        '[{"id": "sub-1", "slot": "est-1", "confidence": "very", "why": "x"}]',
        BATCH,
        VALID,
    )
    assert out[0].confidence is None

    out = parse_response(
        '[{"id": "sub-1", "slot": "est-1", "confidence": 4.2, "why": "x"}]',
        BATCH,
        VALID,
    )
    assert out[0].confidence is None


def test_unparseable_output_yields_nothing_rather_than_raising():
    # A failed batch must leave its items blank for a human, not crash the run.
    assert parse_response("I couldn't do that.", BATCH, VALID) == []
    assert parse_response("", BATCH, VALID) == []
    assert parse_response('[{"id": "sub-1",', BATCH, VALID) == []


def test_a_markdown_fence_is_tolerated():
    # The contract forbids one; models emit them anyway, and refusing would
    # drop a whole batch of good answers over formatting.
    out = parse_response(
        '```json\n[{"id": "sub-1", "slot": "est-1", "confidence": 0.7, "why": "x"}]\n```',
        BATCH,
        VALID,
    )
    assert out[0].slot_id == "est-1"


def test_why_is_truncated_not_rejected():
    out = parse_response(
        '[{"id": "sub-1", "slot": "est-1", "confidence": 0.7, "why": "%s"}]' % ("x" * 400),
        BATCH,
        VALID,
    )
    assert len(out[0].why) == 120


def test_a_failed_batch_drops_its_items_and_does_not_raise():
    """A broken call must leave blank fields, not kill the run.

    THE BUG THIS CAUGHT, live: `_call_claude` takes `model` and `api_key` as
    REQUIRED keyword-only args. The default caller omitted both, so every batch
    raised TypeError and the loop swallowed it — the run reported "proposed 0
    of 21", which reads as a model that declined everything rather than as a
    call that never happened. It is now logged at WARNING for that reason.
    """
    from cp_engine.route_propose import propose

    def boom(_prompt: str) -> str:
        raise TypeError("missing 2 required keyword-only arguments")

    assert propose(BATCH, SLOTS, call=boom) == []


def test_propose_passes_the_slot_list_to_the_model_and_validates_the_answer():
    seen = {}

    def fake(prompt: str) -> str:
        seen["prompt"] = prompt
        # One valid id, one invented one. Only the first may survive.
        return (
            '[{"id": "sub-1", "slot": "est-1", "confidence": 0.8, "why": "a"},'
            ' {"id": "sub-2", "slot": "est-INVENTED", "confidence": 0.9, "why": "b"}]'
        )

    out = propose(BATCH, SLOTS, call=fake)
    assert "`est-1`" in seen["prompt"]
    assert [p.row_id for p in out] == ["sub-1"]
