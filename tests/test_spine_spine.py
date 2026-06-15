from datetime import date
from pathlib import Path

from cp_engine.spine import SpineElement, element_to_row


def _el(**over):
    base = dict(
        id="ibx-5153/deliverable/positioning-narrative",
        project="ibx-5153",
        layer="Deliverables",
        title="IBX positioning narrative",
        status="active",
        last_touched="2026-06-13",
        path=Path("/t/1p/infoblox/ibx-5153/spine/Deliverables/pos.md"),
        body="# body",
        type="positioning-narrative",
        stage="revised",
        target_date="2026-06-17",
        depends_on=("ibx-5153/deliverable/foundation",),
        serves=("ibx-5153/deliverable/positioning-narrative",),
    )
    base.update(over)
    return SpineElement(**base)


def test_element_to_row_maps_spine_fields():
    row = element_to_row(_el(), project_id="uuid-123", project_root=Path("/t"))
    assert row["element_id"] == "ibx-5153/deliverable/positioning-narrative"
    assert row["project_id"] == "uuid-123"
    assert row["project_code"] == "ibx-5153"
    assert row["layer"] == "Deliverables"
    assert row["type"] == "positioning-narrative"
    assert row["stage"] == "revised"
    assert row["target_date"] == "2026-06-17"
    assert row["status"] == "active"
    assert row["last_touched"] == "2026-06-13"
    assert row["depends_on"] == ["ibx-5153/deliverable/foundation"]
    assert row["serves"] == ["ibx-5153/deliverable/positioning-narrative"]


def test_element_to_row_empty_date_becomes_null():
    row = element_to_row(_el(target_date=None, last_touched=""),
                         project_id="u", project_root=Path("/t"))
    assert row["target_date"] is None
    assert row["last_touched"] is None  # "" is not a valid date → null


def test_element_to_row_rel_path_is_relative_to_project_root():
    row = element_to_row(_el(), project_id="u", project_root=Path("/t"))
    assert row["rel_path"] == "1p/infoblox/ibx-5153/spine/Deliverables/pos.md"


def test_element_to_row_target_history_passes_through_as_list():
    history = (
        {"date": "2026-06-02", "set": "2026-06-02", "reason": "initial"},
        {"date": "2026-06-13", "set": "2026-06-13", "reason": "client late"},
    )
    row = element_to_row(_el(target_history=history),
                         project_id="u", project_root=Path("/t"))
    assert row["target_history"] == list(history)
    assert isinstance(row["target_history"], list)
