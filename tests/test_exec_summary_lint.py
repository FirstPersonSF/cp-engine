# tests/test_exec_summary_lint.py — warn-only Exec Summary per-field budgets
from cp_engine.exec_summary_lint import (
    BLOCKERS_MAX_BULLETS,
    NEXT_UP_MAX_BULLETS,
    STATUS_MAX_WORDS,
    WHERE_MAX_BULLETS,
    WHERE_MAX_WORDS_PER_BULLET,
    lint_exec_summary,
)
from cp_engine.render import EXEC_SUMMARY_END, EXEC_SUMMARY_START


def _cp_md(status, where=(), next_up=(), blockers=()):
    """A minimal project cp.md whose exec-summary region carries the given
    field values (bullets as `- ` lines)."""
    def bullets(items):
        return "\n".join(f"- {b}" for b in items)

    return (
        "# Test project\n\n"
        f"{EXEC_SUMMARY_START}\n"
        "## Exec Summary  ·  updated 2026-07-25\n\n"
        "**Last session:** 2026-07-23\n"
        "**Objective:** Restructure the platform pitch into a field-usable "
        "storyboard.\n"
        f"**Status:** {status}\n\n"
        f"**Where it stands:**\n{bullets(where)}\n\n"
        f"**Next up:**\n{bullets(next_up)}\n\n"
        f"**Blockers:**\n{bullets(blockers)}\n\n"
        "**Updates:**\n"
        "- 2026-07-25 — built the deck.\n"
        f"{EXEC_SUMMARY_END}\n\n"
        "## Project Notes\n\nreal notes\n"
    )


# A trimmed compliant summary (raw material: the real ibx-5192 exec summary,
# cut down to its designed densities).
_COMPLIANT = _cp_md(
    status="Two decks built as real .pptx — SRS field deck at v09 (31 slides) "
           "and Jaime's mainstage explainer at v02 (9 slides); both await "
           "Geoff's visual pass, gated on the 7/22 pillar-name ruling.",
    where=(
        "**Arc B deck — v08 → r01.** The 24-slide field telling is the base; "
        "the Deck r01 call folded four decisions on top.",
        "Kimber's asset batch ingested: Trish's capability xlsx, 9 use-case "
        "briefs, the final 90-sec pitch.",
        "Infoblox PPT template received from Jarrod/Brand Studio via Priya.",
    ),
    next_up=(
        "Hand both decks to Geoff on his Friday return for visual polish.",
        "Eyeball the cube wow slide (SRS slide 17) in PowerPoint.",
        "At 7/22: get the pillar-name ruling.",
    ),
    blockers=(
        "**Pillar-name ruling (7/22)** — the one thing gating deck design.",
        "**Mehul's 6–7 pains/persona** — owed, to sharpen Slide 2/4.",
    ),
)


def test_compliant_summary_is_silent():
    assert lint_exec_summary(_COMPLIANT) == []


def test_fat_status_warns_with_actual_vs_budget():
    # Acceptance fixture: a 700-word Status must warn.
    fat = " ".join(f"word{i}" for i in range(700))
    out = lint_exec_summary(_cp_md(status=fat))
    assert len(out) == 1
    assert "Status" in out[0]
    assert "700 words" in out[0]
    assert f"budget {STATUS_MAX_WORDS}" in out[0]


def test_status_at_budget_is_silent():
    at_cap = " ".join(f"w{i}" for i in range(STATUS_MAX_WORDS))
    assert lint_exec_summary(_cp_md(status=at_cap)) == []


def test_status_word_count_ignores_markdown_emphasis():
    # `**bold**` styling must not split or inflate the count.
    styled = "**Two** decks `built` — " + " ".join("w" for _ in range(96))
    assert lint_exec_summary(_cp_md(status=styled)) == []


def test_too_many_where_bullets_warns():
    where = tuple(f"thread {i} holds" for i in range(WHERE_MAX_BULLETS + 2))
    out = lint_exec_summary(_cp_md(status="fine", where=where))
    assert len(out) == 1
    assert "Where it stands" in out[0]
    assert f"{WHERE_MAX_BULLETS + 2} bullets" in out[0]
    assert f"budget {WHERE_MAX_BULLETS}" in out[0]


def test_over_dense_where_bullet_warns_once_with_worst():
    long_a = " ".join("w" for _ in range(50))
    long_b = " ".join("w" for _ in range(92))
    out = lint_exec_summary(
        _cp_md(status="fine", where=("short bullet", long_a, long_b)))
    assert len(out) == 1
    assert f"2 bullet(s) over {WHERE_MAX_WORDS_PER_BULLET} words" in out[0]
    assert "worst 92" in out[0]


def test_too_many_next_up_and_blockers_warn():
    out = lint_exec_summary(_cp_md(
        status="fine",
        next_up=tuple(f"do {i}" for i in range(NEXT_UP_MAX_BULLETS + 1)),
        blockers=tuple(f"gate {i}" for i in range(BLOCKERS_MAX_BULLETS + 3)),
    ))
    assert len(out) == 2
    next_w, blockers_w = out
    assert "Next up" in next_w and f"budget {NEXT_UP_MAX_BULLETS}" in next_w
    assert "Blockers" in blockers_w
    assert f"{BLOCKERS_MAX_BULLETS + 3} bullets" in blockers_w


def test_bullet_counts_at_budget_are_silent():
    assert lint_exec_summary(_cp_md(
        status="fine",
        where=tuple(f"t {i}" for i in range(WHERE_MAX_BULLETS)),
        next_up=tuple(f"n {i}" for i in range(NEXT_UP_MAX_BULLETS)),
        blockers=tuple(f"b {i}" for i in range(BLOCKERS_MAX_BULLETS)),
    )) == []


def test_no_region_is_silent():
    assert lint_exec_summary("# cp\n\n## Current Work\nstuff\n") == []
    assert lint_exec_summary("") == []


def test_unauthored_scaffold_is_silent():
    scaffold = (
        f"{EXEC_SUMMARY_START}\n"
        "## Exec Summary\n\n"
        "**Objective:** _<one line>_\n"
        "**Status:** _<current state in a phrase>_\n"
        "**Where it stands:**\n"
        "- _<bullets of current reality>_\n"
        f"{EXEC_SUMMARY_END}\n"
    )
    assert lint_exec_summary(scaffold) == []


def test_placeholder_bullets_do_not_count():
    where = tuple("_<seed bullet>_" for _ in range(WHERE_MAX_BULLETS + 3))
    assert lint_exec_summary(_cp_md(status="fine", where=where)) == []
