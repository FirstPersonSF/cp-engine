# tests/test_spine_done.py
from cp_engine.spine_done import build_done_map, derive_done


def _bar(work_item_id, done):
    return {"work_item_id": work_item_id, "done": done}


def test_build_done_map_any_bar_done_wins():
    bars = [_bar("w1", False), _bar("w1", True), _bar("w2", False)]
    m = build_done_map(bars)
    assert m["w1"] is True      # any bar done → True (Rule 1 semantics)
    assert m["w2"] is False     # bound, no bar done → False


def test_build_done_map_ignores_bars_without_work_item():
    bars = [_bar(None, True), _bar("w1", False)]
    m = build_done_map(bars)
    assert None not in m
    assert m["w1"] is False


def test_derive_done_three_states():
    done_map = {"w1": True, "w2": False}
    assert derive_done("w1", done_map) is True
    assert derive_done("w2", done_map) is False
    assert derive_done("_authored/x", done_map) is None
    assert derive_done(None, done_map) is None
