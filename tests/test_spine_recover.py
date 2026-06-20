from cp_engine.spine_recover import classify_element


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
