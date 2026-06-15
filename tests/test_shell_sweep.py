from datetime import date
from pathlib import Path

from cp_engine.shell import ShellElement
from cp_engine.shell_sweep import build_sweep_prompt


def _el(eid, layer, **o):
    d = dict(id=eid, project="ibx-5153", layer=layer, title=eid, status="active",
             last_touched="2026-06-13", path=Path("/x.md"), body="")
    d.update(o)
    return ShellElement(**d)


def test_build_sweep_prompt_structure():
    hot = _el("ibx-5153/deliverable/foundation", "Deliverables", title="Foundation P&P doc",
              stage="revised", last_touched="2026-06-13", body="The internal strategic brief.")
    cold = _el("ibx-5153/research/old-iv", "Research", title="April interview",
               last_touched="2026-04-01", body="Early discovery notes.")
    prompt = build_sweep_prompt("ibx-5153", (hot, cold), today=date(2026, 6, 13))
    assert "ibx-5153" in prompt
    assert "Foundation P&P doc" in prompt
    assert "April interview" in prompt
    # instruction to synthesize + name cold threads
    low = prompt.lower()
    assert "synthesis" in low or "readout" in low
    assert "cold" in low


def test_build_sweep_prompt_caps_excerpts():
    # Many elements: only the top-N by score get body excerpts; the rest are
    # title-only. Build 40 elements, assert the prompt isn't unbounded.
    els = tuple(
        _el(f"ibx-5153/research/iv{i}", "Research", title=f"Interview {i}",
            body="x" * 500, last_touched="2026-06-13")
        for i in range(40)
    )
    prompt = build_sweep_prompt("ibx-5153", els, today=date(2026, 6, 13))
    # all titles present (title-only for the tail)
    assert "Interview 0" in prompt and "Interview 39" in prompt
    # but not all 40 full 500-char bodies (excerpt cap bounds it)
    assert prompt.count("x" * 200) <= 30  # at most ~top-30 get long excerpts


def test_build_sweep_prompt_ranks_hot_before_cold():
    hot = _el("ibx-5153/deliverable/foundation", "Deliverables", title="Hot foundation",
              stage="revised", last_touched="2026-06-13")
    cold = _el("ibx-5153/research/old", "Research", title="Cold April note",
               last_touched="2026-04-01")
    prompt = build_sweep_prompt("ibx-5153", (cold, hot), today=date(2026, 6, 13))
    # hot element's title appears before the cold one in the ranked list
    assert prompt.index("Hot foundation") < prompt.index("Cold April note")


def test_run_sweep_calls_llm_and_returns_result():
    from datetime import date
    from cp_engine.shell_sweep import run_sweep
    hot = _el("ibx-5153/deliverable/foundation", "Deliverables", title="Foundation",
              stage="revised", last_touched="2026-06-13")
    calls = []
    def fake_llm(prompt):
        calls.append(prompt)
        return "SYNTHESIS: the project is in good shape."
    result = run_sweep("ibx-5153", (hot,), today=date(2026, 6, 13), llm=fake_llm)
    assert len(calls) == 1                       # llm was called once
    assert "Foundation" in calls[0]              # got the built prompt
    assert result.synthesis_text == "SYNTHESIS: the project is in good shape."
    assert "Foundation" in result.ranked_table   # render_sweep table included


def test_run_sweep_empty_shell_skips_llm():
    from datetime import date
    from cp_engine.shell_sweep import run_sweep
    calls = []
    def fake_llm(prompt):
        calls.append(prompt); return "should not be called"
    result = run_sweep("ibx-5153", (), today=date(2026, 6, 13), llm=fake_llm)
    assert calls == []                            # LLM NOT called for empty shell
    assert "no shell elements" in result.synthesis_text.lower()


# --- meeting-summary enrichment (sweep-enrichment) ---------------------------

_RETRO_BODY = """# Meeting history

### 2026-06-11 · Workshop kickoff (Janet, Drew)

We aligned on the two-track AI story and the Engage/Execute/Extend framing.

**Decisions:** ship the foundation doc by 6/12
**Action items:** Drew to draft P&P pre-read
[Fathom recording](http://f/1)
<!-- cp:meeting=m1 -->

### 2026-06-08 · Carol framework review (Carol, Drew)

Carol walked her five-category deck collapsing into three chapters.

**Decisions:** adopt three-chapter structure
<!-- cp:meeting=m2 -->

### 2026-05-27 · Janet positioning (Janet)

Leadership positioning sharpening on the two-track thesis.
<!-- cp:meeting=m3 -->
"""


def _retro(body=_RETRO_BODY):
    return _el("ibx-5153/retrospective/meeting-history", "Retrospective",
               title="Meeting history", type="retrospective", body=body)


def test_recent_meeting_summaries_no_retro_element():
    from cp_engine.shell_sweep import recent_meeting_summaries
    hot = _el("ibx-5153/deliverable/foundation", "Deliverables", body="x")
    assert recent_meeting_summaries((hot,)) == []


def test_recent_meeting_summaries_extracts_newest_first():
    from cp_engine.shell_sweep import recent_meeting_summaries
    out = recent_meeting_summaries((_retro(),))
    assert len(out) == 3
    # newest first
    assert "Workshop kickoff" in out[0]
    assert "Carol framework review" in out[1]
    assert "Janet positioning" in out[2]
    # full entry text preserved (summary + decisions)
    assert "two-track AI story" in out[0]
    assert "ship the foundation doc by 6/12" in out[0]
    # the H1 is not an entry
    assert all("# Meeting history" not in e for e in out)


def test_recent_meeting_summaries_respects_limit():
    from cp_engine.shell_sweep import recent_meeting_summaries
    out = recent_meeting_summaries((_retro(),), limit=2)
    assert len(out) == 2
    assert "Workshop kickoff" in out[0]
    assert "Carol framework review" in out[1]


def test_recent_meeting_summaries_strips_meeting_marker():
    from cp_engine.shell_sweep import recent_meeting_summaries
    out = recent_meeting_summaries((_retro(),))
    assert all("cp:meeting" not in e for e in out)


def test_build_sweep_prompt_includes_meeting_summaries():
    hot = _el("ibx-5153/deliverable/foundation", "Deliverables", title="Foundation",
              body="brief")
    prompt = build_sweep_prompt(
        "ibx-5153", (hot,), today=date(2026, 6, 13),
        meeting_summaries=["### 2026-06-11 · X\n\nSummary text about the workshop"],
    )
    assert "## Recent meeting discussion" in prompt
    assert "Summary text about the workshop" in prompt


def test_build_sweep_prompt_no_summaries_is_unchanged():
    hot = _el("ibx-5153/deliverable/foundation", "Deliverables", title="Foundation",
              body="brief")
    none_prompt = build_sweep_prompt("ibx-5153", (hot,), today=date(2026, 6, 13))
    explicit_none = build_sweep_prompt("ibx-5153", (hot,), today=date(2026, 6, 13),
                                       meeting_summaries=None)
    empty = build_sweep_prompt("ibx-5153", (hot,), today=date(2026, 6, 13),
                               meeting_summaries=[])
    assert "## Recent meeting discussion" not in none_prompt
    assert none_prompt == explicit_none == empty


def test_run_sweep_passes_meeting_summaries_to_llm():
    from cp_engine.shell_sweep import run_sweep
    hot = _el("ibx-5153/deliverable/foundation", "Deliverables", title="Foundation",
              stage="revised", body="brief")
    calls = []
    def fake_llm(prompt):
        calls.append(prompt)
        return "synthesis"
    run_sweep("ibx-5153", (hot, _retro()), today=date(2026, 6, 13), llm=fake_llm)
    assert len(calls) == 1
    assert "## Recent meeting discussion" in calls[0]
    assert "two-track AI story" in calls[0]
