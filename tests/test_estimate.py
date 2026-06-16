from cp_engine.estimate import Estimate, EstimateItem


def test_estimate_from_rows_builds_ordered_items():
    project_row = {"id": "est-1", "mc_project_id": "mc-1", "is_default": True, "name": "Estimate 1"}
    phases = [
        {"id": "ph-0", "project_id": "est-1", "name": "Phase 0 Discovery", "overview": "…", "position": 0},
        {"id": "ph-1", "project_id": "est-1", "name": "Phase 1 Storybuilding", "overview": "…", "position": 1},
    ]
    activities = [
        {"id": "a-1", "phase_id": "ph-0", "name": "Narrative Audit", "short_description": "…", "position": 1, "library_item_id": None},
        {"id": "a-0", "phase_id": "ph-0", "name": "Strategy Alignment", "short_description": "…", "position": 0, "library_item_id": None},
    ]
    deliverables = [
        {"id": "d-0", "phase_id": "ph-0", "name": "Perspectives & Possibilities Report", "short_description": "…", "position": 2, "library_item_id": None},
    ]
    est = Estimate.from_rows(project_row, phases, activities, deliverables)
    assert est.mc_project_id == "mc-1"
    assert [p.name for p in est.phases] == ["Phase 0 Discovery", "Phase 1 Storybuilding"]
    ph0 = est.phases[0]
    assert [(i.kind, i.name) for i in ph0.items] == [
        ("activity", "Strategy Alignment"),
        ("activity", "Narrative Audit"),
        ("deliverable", "Perspectives & Possibilities Report"),
    ]
    assert est.item_by_id("d-0").kind == "deliverable"
    assert est.item_by_id("nope") is None
