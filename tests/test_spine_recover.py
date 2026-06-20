from cp_engine.spine_recover import classify_element, match_source_asset, plan_element, source_basename


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
