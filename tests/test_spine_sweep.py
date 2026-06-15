from datetime import date
from pathlib import Path

from cp_engine.spine import SpineElement
from cp_engine.spine_sweep import build_sweep_prompt


def _el(eid, layer, **o):
    d = dict(id=eid, project="ibx-5153", layer=layer, title=eid, status="active",
             last_touched="2026-06-13", path=Path("/x.md"), body="")
    d.update(o)
    return SpineElement(**d)


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
    from cp_engine.spine_sweep import run_sweep
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


def test_run_sweep_empty_spine_skips_llm():
    from datetime import date
    from cp_engine.spine_sweep import run_sweep
    calls = []
    def fake_llm(prompt):
        calls.append(prompt); return "should not be called"
    result = run_sweep("ibx-5153", (), today=date(2026, 6, 13), llm=fake_llm)
    assert calls == []                            # LLM NOT called for empty spine
    assert "no spine elements" in result.synthesis_text.lower()


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
    from cp_engine.spine_sweep import recent_meeting_summaries
    hot = _el("ibx-5153/deliverable/foundation", "Deliverables", body="x")
    assert recent_meeting_summaries((hot,)) == []


def test_recent_meeting_summaries_extracts_newest_first():
    from cp_engine.spine_sweep import recent_meeting_summaries
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
    from cp_engine.spine_sweep import recent_meeting_summaries
    out = recent_meeting_summaries((_retro(),), limit=2)
    assert len(out) == 2
    assert "Workshop kickoff" in out[0]
    assert "Carol framework review" in out[1]


def test_recent_meeting_summaries_strips_meeting_marker():
    from cp_engine.spine_sweep import recent_meeting_summaries
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
    from cp_engine.spine_sweep import run_sweep
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


# --- Task 4.2: drift proposal ----------------------------------------------

def test_parse_drift_extracts_block_and_strips_it():
    from cp_engine.spine_sweep import parse_drift
    text = (
        "The project is progressing.\n\nCold thread: the April brief.\n\n"
        "```yaml\n"
        "drift:\n"
        "  - element_id: ibx-5153/deliverable/pos\n"
        "    field: stage\n"
        "    observation: stage says draft but client signed off.\n"
        "  - element_id: ibx-5153/research/old\n"
        "    field: status\n"
        "    observation: superseded by the June direction.\n"
        "```\n"
    )
    clean, items = parse_drift(text)
    assert "```" not in clean
    assert "drift:" not in clean
    assert clean == "The project is progressing.\n\nCold thread: the April brief."
    assert len(items) == 2
    assert items[0] == {
        "element_id": "ibx-5153/deliverable/pos",
        "field": "stage",
        "observation": "stage says draft but client signed off.",
    }
    assert items[1]["element_id"] == "ibx-5153/research/old"


def test_parse_drift_no_block_returns_original():
    from cp_engine.spine_sweep import parse_drift
    text = "Just prose, no drift here at all."
    clean, items = parse_drift(text)
    assert clean == text
    assert items == []


def test_parse_drift_malformed_yaml_returns_original():
    from cp_engine.spine_sweep import parse_drift
    text = "Prose.\n\n```yaml\ndrift:\n  - element_id: x\n   bad: : indent\n```\n"
    clean, items = parse_drift(text)
    assert clean == text
    assert items == []


def test_parse_drift_skips_items_without_element_id():
    from cp_engine.spine_sweep import parse_drift
    text = (
        "Prose.\n\n```yaml\n"
        "drift:\n"
        "  - field: stage\n"
        "    observation: no id here\n"
        "  - element_id: ibx-5153/deliverable/pos\n"
        "    observation: real one\n"
        "```\n"
    )
    clean, items = parse_drift(text)
    assert len(items) == 1
    assert items[0]["element_id"] == "ibx-5153/deliverable/pos"
    # field defaults to "thinking" when missing
    assert items[0]["field"] == "thinking"
    assert items[0]["observation"] == "real one"


def test_parse_drift_block_without_drift_key_is_ignored():
    from cp_engine.spine_sweep import parse_drift
    text = "Prose.\n\n```yaml\nsomething: else\n```\n"
    clean, items = parse_drift(text)
    assert clean == text
    assert items == []


def test_run_sweep_populates_drift_and_strips_block():
    from cp_engine.spine_sweep import run_sweep
    hot = _el("ibx-5153/deliverable/foundation", "Deliverables", title="Foundation",
              stage="revised", body="brief")
    synthesis = (
        "Readout prose here.\n\n"
        "```yaml\n"
        "drift:\n"
        "  - element_id: ibx-5153/deliverable/foundation\n"
        "    field: stage\n"
        "    observation: looks stale.\n"
        "```\n"
    )
    result = run_sweep("ibx-5153", (hot,), today=date(2026, 6, 13),
                       llm=lambda p: synthesis)
    assert result.synthesis_text == "Readout prose here."
    assert "```" not in result.synthesis_text
    assert len(result.drift_items) == 1
    assert result.drift_items[0]["element_id"] == "ibx-5153/deliverable/foundation"


def test_run_sweep_no_drift_default():
    from cp_engine.spine_sweep import run_sweep
    hot = _el("ibx-5153/deliverable/foundation", "Deliverables", title="Foundation",
              body="brief")
    result = run_sweep("ibx-5153", (hot,), today=date(2026, 6, 13),
                       llm=lambda p: "just prose")
    assert result.synthesis_text == "just prose"
    assert result.drift_items == ()


def test_build_sweep_prompt_asks_for_drift_block():
    hot = _el("ibx-5153/deliverable/foundation", "Deliverables", title="Foundation",
              body="brief")
    prompt = build_sweep_prompt("ibx-5153", (hot,), today=date(2026, 6, 13))
    low = prompt.lower()
    assert "drift" in low
    assert "element_id" in prompt


def test_recent_meeting_summaries_keeps_whole_entry_with_embedded_h3():
    # A Fathom summary embedded WHOLE can contain its own '### ' sub-headers.
    # The entry must NOT split on those — only on real `### <date>` headers.
    from cp_engine.spine_sweep import recent_meeting_summaries
    body = (
        "# Meeting history\n\n"
        "### 2026-06-11 · Workshop framing (Janet, Carol)\n\n"
        "Summary intro.\n\n"
        "### Key decisions\n"   # an H3 INSIDE the Fathom summary
        "- two-track confirmed\n\n"
        "### Next steps\n"      # another summary H3
        "- Carol sends deck\n"
        "<!-- cp:meeting=m1 -->\n\n"
        "### 2026-06-10 · Jamie feedback (Jamie)\n\n"
        "Older meeting.\n"
        "<!-- cp:meeting=m2 -->\n"
    )
    out = recent_meeting_summaries((_retro(body),), limit=4)
    assert len(out) == 2  # two MEETINGS, not four fragments
    # The first entry retains its embedded summary H3s whole.
    assert "### Key decisions" in out[0]
    assert "### Next steps" in out[0]
    assert "2026-06-10" in out[1]


def test_recent_meeting_summaries_empty_body_returns_empty():
    # MC-2-loaded elements carry body="" — the feature degrades to a no-op.
    from cp_engine.spine_sweep import recent_meeting_summaries
    assert recent_meeting_summaries((_retro(body=""),)) == []


def test_run_sweep_hydrates_retrospective_body_from_disk(tmp_path):
    # The element comes from MC-2 (body=""), but run_sweep(tenant_root=...) must
    # read its body from disk so meeting summaries reach the prompt.
    from cp_engine.spine_sweep import run_sweep
    rel = Path("1p/acct/ibx-5153/spine/Retrospective/meeting-history.md")
    f = tmp_path / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        "---\nid: ibx-5153/retrospective/meeting-history\nproject: ibx-5153\n"
        "layer: Retrospective\ntitle: Meeting history\nstatus: active\n"
        "last_touched: 2026-06-13\n---\n\n# Meeting history\n\n"
        "### 2026-06-11 · Workshop framing (Janet)\n\nDISTINCTIVE_SUMMARY_TEXT\n"
        "<!-- cp:meeting=m1 -->\n"
    )
    retro = _el("ibx-5153/retrospective/meeting-history", "Retrospective",
                body="", path=rel)  # empty body, as MC-2 returns
    hot = _el("ibx-5153/deliverable/d", "Deliverables", body="work")
    captured = {}
    def _fake_llm(prompt):
        captured["prompt"] = prompt
        return "synthesis prose"
    run_sweep("ibx-5153", (retro, hot), today=date.today(),
              llm=_fake_llm, tenant_root=tmp_path)
    assert "DISTINCTIVE_SUMMARY_TEXT" in captured["prompt"]


def test_parse_drift_ignores_earlier_prose_code_fence():
    # A prose code fence before the drift block must not break drift capture.
    from cp_engine.spine_sweep import parse_drift
    text = (
        "Here is the readout.\n\n"
        "```python\nexample_code()\n```\n\n"
        "More prose.\n\n"
        "```yaml\ndrift:\n  - element_id: ibx-5153/deliverable/d\n"
        "    field: status\n    observation: looks stale\n```\n"
    )
    clean, items = parse_drift(text)
    assert len(items) == 1
    assert items[0]["element_id"] == "ibx-5153/deliverable/d"
    assert "example_code()" in clean       # prose fence preserved
    assert "drift:" not in clean           # only the drift block removed
