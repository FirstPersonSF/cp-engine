from pathlib import Path

from cp_engine.spine_recover import (
    RecoveryAction,
    classify_element,
    load_legacy_elements,
    match_source_asset,
    plan_element,
    redistill_body,
    source_basename,
)


class _El:  # minimal stand-in for SpineElement (plan_element reads only these)
    def __init__(self, layer, title, body, source=(), serves=()):
        self.layer, self.title, self.body = layer, title, body
        self.source, self.serves = source, serves


def test_plan_source_backed_element_marks_redistill():
    el = _El("SourceMaterial", "Infoblox Platform v6 deck", "old body",
             source=("Reference Materials/Infoblox_Platform_v6_Synthesized_CB.pptx",))
    assets = [{"id": "a1", "title": "Infoblox_Platform_v6_Synthesized_CB.pptx"}]
    action = plan_element(el, assets)
    assert action.mode == "redistill"
    assert action.asset_id == "a1"
    assert action.layer == "SourceMaterial"
    assert action.label == "Infoblox Platform v6 deck"


def test_plan_synthesis_element_marks_carry():
    el = _El("Decisions", "DECISION: two-track", "the decision body")
    action = plan_element(el, [])
    assert action.mode == "carry"
    assert action.body == "the decision body"   # verbatim
    assert action.layer == "Decisions"
    assert action.label == "DECISION: two-track"


def test_plan_source_backed_without_asset_match_carries():
    el = _El("SourceMaterial", "X", "body", source=("Missing.pptx",))
    action = plan_element(el, [{"id": "a1", "title": "Other.docx"}])
    assert action.mode == "carry"   # no confident source → carry over


def test_plan_carries_serves_cross_links():
    el = _El("Decisions", "D", "b", serves=("ibx-5153/deliverable/foundation-pp-doc",))
    action = plan_element(el, [])
    assert action.serves == ("ibx-5153/deliverable/foundation-pp-doc",)


def test_classify_source_backed_layers():
    # SourceMaterial/ClientFeedback/Research/Brief/Agreement are source-backed
    # (when the element actually has a source to re-distill from).
    for layer in ("SourceMaterial", "ClientFeedback", "Research", "Brief", "Agreement"):
        assert classify_element(layer, has_source=True) == "source-backed"


def test_classify_synthesis_layers():
    # Decisions/Synthesis/Retrospective/Timeline/Stakeholders are synthesis.
    for layer in ("Decisions", "Synthesis", "Retrospective", "Timeline", "Stakeholders"):
        assert classify_element(layer, has_source=False) == "synthesis"


def test_source_backed_layer_without_source_falls_back_to_carry():
    # A source-backed LAYER but no `source:` field → can't re-distill → carry over.
    assert classify_element("SourceMaterial", has_source=False) == "synthesis"


def test_source_basename_strips_dir_prefix():
    assert source_basename("Reference Materials/Infoblox_Platform_v6_Synthesized_CB.pptx") \
        == "Infoblox_Platform_v6_Synthesized_CB.pptx"
    assert source_basename("Our AI Story.docx") == "Our AI Story.docx"


def test_match_source_asset_by_basename():
    assets = [
        {"id": "a1", "title": "Infoblox_Platform_v6_Synthesized_CB.pptx"},
        {"id": "a2", "title": "Our AI Story.docx"},
    ]
    m = match_source_asset(("Reference Materials/Infoblox_Platform_v6_Synthesized_CB.pptx",), assets)
    assert m == {"id": "a1", "title": "Infoblox_Platform_v6_Synthesized_CB.pptx"}


def test_match_returns_none_when_no_basename_match():
    assert match_source_asset(("Unknown File.pdf",), [{"id": "a1", "title": "Other.docx"}]) is None


def test_match_skips_typed_rag_asset_dict_sources():
    assets = [{"id": "a9", "title": "Whatever.docx"}]
    m = match_source_asset(({"type": "rag_asset", "id": "a9"},), assets)
    assert m == {"id": "a9", "title": "Whatever.docx"}


def test_match_empty_sources_returns_none():
    assert match_source_asset((), [{"id": "a1", "title": "X.docx"}]) is None


# ---- load_legacy_elements ---------------------------------------------------

def test_load_legacy_elements_returns_only_legacy_layer_files(tmp_path: Path):
    spine = tmp_path / "spine"

    # A LEGACY element in a capitalized LAYER dir → should load.
    legacy = spine / "Decisions" / "two-track.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        "---\n"
        "id: ibx-5153/decisions/two-track\n"
        "project: ibx-5153\n"
        "layer: Decisions\n"
        'title: "Two-track AI story"\n'
        "status: active\n"
        "last_touched: 2026-05-27\n"
        "---\n"
        "The two-track decision body.\n",
        encoding="utf-8",
    )

    # A NEW-substance file in a lowercase phase dir → NOT a LAYER → skipped.
    new = spine / "phase-0" / "messaging.md"
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_text(
        "---\n"
        "est_item_id: 11111111-2222-3333-4444-555555555555\n"
        "est_item_kind: deliverable\n"
        "phase: phase-0\n"
        "binding: live\n"
        "---\n"
        "## v1 — 2026-06-19 · live\n\nmessaging substance\n",
        encoding="utf-8",
    )

    els = load_legacy_elements(tmp_path)

    assert len(els) == 1
    assert els[0].layer == "Decisions"
    assert els[0].title == "Two-track AI story"
    assert els[0].id == "ibx-5153/decisions/two-track"


# ---- redistill_body ---------------------------------------------------------

def test_redistill_body_calls_distiller_with_framing_and_source():
    captured = {}
    def fake_distiller(prompt):
        captured["prompt"] = prompt
        return "fresh distilled body"
    action = RecoveryAction(
        mode="redistill", layer="SourceMaterial", label="Infoblox v6 deck",
        body="", serves=(), asset_id="a1", source_basename="Infoblox_v6.pptx",
    )
    out = redistill_body(action, asset_text="...51-slide deck text about IQ...", distiller=fake_distiller)
    assert out == "fresh distilled body"
    assert "Infoblox v6 deck" in captured["prompt"]          # the framing/label
    assert "51-slide deck text about IQ" in captured["prompt"]  # the source text


def test_redistill_body_strips_distiller_output():
    action = RecoveryAction(mode="redistill", layer="Research", label="X", body="",
                            serves=(), asset_id="a1", source_basename="x.pdf")
    out = redistill_body(action, asset_text="src", distiller=lambda p: "  body with spaces  \n")
    assert out == "body with spaces"
