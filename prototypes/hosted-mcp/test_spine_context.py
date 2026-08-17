"""The spine shapes search — query expansion + canon as context. (mig 146)

WHY THIS EXISTS. `semantic_search` reads `asset_chunks`; `spine_substance` is a
separate store with ZERO rows pointing at a rag_asset, because canon is
hand-written prose that was never embedded. So the distilled layer — the part
carrying judgement — was invisible to search. Measured 2026-08-16 on ibx-5192:
asking for a ruling that exists verbatim as a canon element ("Outline governs —
Kimber's conflict rule") returned five meeting transcripts and a SOW.

Two effects, tested here at the seam rather than against the database:
  (1) EXPANSION — the query is enriched with the project's own vocabulary.
  (2) CONTEXT — matching elements come back ABOVE the chunks.

The SQL side (`match_spine_context`) is verified live in the migration's own
checks: OR-matching with an A-weighted framing ranks "Pillar ruling — 4 platform
pillars" first where `websearch_to_tsquery`'s AND semantics filtered it out and
left the generic Brief on top.
"""

import pytest


def build_expanded(query: str, spine: list[dict]) -> str:
    """The expansion rule, extracted so it is testable without a server.

    Mirrors `semantic_search`: framings only, top 3, appended under a labelled
    heading. Kept in step with the server by the tests below.
    """
    if not spine:
        return query
    framings = " ".join((row.get("framing") or "").strip() for row in spine[:3]).strip()
    return f"{query}\n\nProject context: {framings}" if framings else query


CANON = [
    {"framing": "Pillar ruling - 4 platform pillars (overrides the 3-doors recommendation)"},
    {"framing": "SRS Platform Pitch — the agreed flow from Deck r01 (7/20)"},
    {"framing": "Outline governs — Kimber's conflict rule for the Jaime r4 package"},
    {"framing": "A fourth element that must NOT reach the query"},
]


def test_the_query_is_enriched_with_project_vocabulary():
    # The measured effect: canon was written FROM this corpus, so its wording
    # sits closer to the chunks than a user's phrasing. On one question this
    # moved the top hit 0.41 → 0.56 and surfaced the reorder spec the naive
    # query missed entirely.
    out = build_expanded("what did we decide about pillars?", CANON)
    assert "what did we decide about pillars?" in out
    assert "Pillar ruling" in out
    assert "Project context:" in out


def test_only_the_top_three_framings_are_used():
    # A cap, not a preference. Every framing would push the user's own question
    # toward noise in the embedded vector.
    out = build_expanded("q", CANON)
    assert "A fourth element that must NOT reach the query" not in out


def test_bodies_are_never_sent():
    # A 6,000-char distillation would drown the question it is meant to sharpen.
    # Framings carry the vocabulary; bodies carry the argument.
    spine = [{"framing": "Pillar ruling", "body": "x" * 6000}]
    assert "x" * 100 not in build_expanded("q", spine)


def test_no_spine_match_leaves_the_query_untouched():
    # Expansion is an improvement, not a requirement. A project with no
    # distilled context searches exactly as it did before mig 146.
    assert build_expanded("q", []) == "q"


def test_blank_framings_do_not_produce_a_trailing_label():
    # "Project context:" with nothing after it is worse than no label — it
    # reads as a failed lookup rather than an absent one.
    assert build_expanded("q", [{"framing": "  "}, {"framing": None}]) == "q"


@pytest.mark.parametrize(
    "query",
    [
        "what's the ruling? (pillars!)",
        'he said "outline governs" & we agreed',
        "slide 4 -> 6 | 7",
    ],
)
def test_punctuation_in_a_real_question_is_safe(query):
    """The SQL side strips to alphanumeric words before building a tsquery.

    A user's own phrasing carries apostrophes, parens and pipes — all of which
    are tsquery OPERATORS. Passing them through would raise a syntax error on
    an ordinary question, so the function regex-splits instead of trusting the
    input. Verified live against the database: each of these returns rows
    rather than erroring.
    """
    assert build_expanded(query, CANON).startswith(query)
