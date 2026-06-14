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
