"""`serves` holds two id spaces; reads name which one each id is in (#416)."""

from __future__ import annotations

from cp_engine.project_sources import _spine_element
from cp_engine.substance import serves_kind, serves_kinds

WORK_ITEM = "3f1a9284-616d-4c2b-9a51-0d6e7f8a9b0c"


def test_kind_from_the_id_shape():
    assert serves_kind(WORK_ITEM) == "work_item"
    assert serves_kind(WORK_ITEM.upper()) == "work_item"
    assert serves_kind("_authored/final-video-package") == "spine_element"
    assert serves_kind("final-video-package") == "unknown"
    assert serves_kind("") == "unknown"


def test_kinds_map_every_entry():
    assert serves_kinds([WORK_ITEM, "_authored/rfp"]) == {
        WORK_ITEM: "work_item", "_authored/rfp": "spine_element"}
    assert serves_kinds(None) == {}


def test_pull_shape_names_each_targets_space():
    row = {"est_item_id": "_authored/rfp", "serves": [WORK_ITEM, "_authored/brief"]}
    out = _spine_element(row)
    assert out["serves"] == [WORK_ITEM, "_authored/brief"]
    assert out["serves_kinds"] == {
        WORK_ITEM: "work_item", "_authored/brief": "spine_element"}


def test_nothing_served_adds_no_key():
    assert "serves_kinds" not in _spine_element({"est_item_id": "_authored/x"})
