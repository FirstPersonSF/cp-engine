# tests/test_spine_done.py
from cp_engine.spine_done import build_done_map, derive_done


def _bar(work_item_id, done):
    return {"work_item_id": work_item_id, "done": done}


def test_build_done_map_any_bar_done_wins():
    bars = [_bar("w1", False), _bar("w1", True), _bar("w2", False)]
    m = build_done_map(bars)
    assert m["w1"] is True      # any bar done → True (Rule 1 semantics)
    assert m["w2"] is False     # bound, no bar done → False


def test_build_done_map_earlier_done_survives_later_not_done():
    # True-then-False for the same id must stay True (order-independent OR-fold,
    # not last-write-wins).
    assert build_done_map([_bar("w1", True), _bar("w1", False)])["w1"] is True


def test_build_done_map_ignores_bars_without_work_item():
    bars = [_bar(None, True), _bar("w1", False)]
    m = build_done_map(bars)
    assert None not in m
    assert m["w1"] is False


def test_build_done_map_and_derive_done_match_on_uuid_string():
    uid = "6f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
    m = build_done_map([{"work_item_id": uid, "done": True}])
    assert derive_done(uid, m) is True          # str↔str uuid match


class _UUIDish:
    # mimics a uuid object: str() gives the canonical form, but != the str
    def __init__(self, s): self._s = s
    def __str__(self): return self._s


def test_done_join_coerces_uuid_object_to_str():
    uid = "6f1c2d3e-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
    # bar carries a uuid-OBJECT, est_item_id is the str form → must still match
    m = build_done_map([{"work_item_id": _UUIDish(uid), "done": True}])
    assert derive_done(uid, m) is True


def test_derive_done_three_states():
    done_map = {"w1": True, "w2": False}
    assert derive_done("w1", done_map) is True
    assert derive_done("w2", done_map) is False
    assert derive_done("_authored/x", done_map) is None
    assert derive_done(None, done_map) is None


from cp_engine import spine_done


def test_fetch_project_done_map_uses_estimate_then_schedule(monkeypatch):
    class _Est:
        id = "EST1"
    captured = {}
    monkeypatch.setattr(spine_done, "fetch_estimate", lambda c, pid: (_Est() if pid == "pid" else None))
    def _fake_schedule(c, estimate_id):
        captured["estimate_id"] = estimate_id
        return [{"work_item_id": "w1", "done": True}]
    monkeypatch.setattr(spine_done, "fetch_schedule", _fake_schedule)
    m = spine_done.fetch_project_done_map(object(), "pid")
    assert captured["estimate_id"] == "EST1"   # filtered by ESTIMATE id, not pid
    assert m == {"w1": True}


def test_fetch_project_done_map_no_estimate_returns_empty(monkeypatch):
    monkeypatch.setattr(spine_done, "fetch_estimate", lambda c, pid: None)
    monkeypatch.setattr(spine_done, "fetch_schedule",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call")))
    assert spine_done.fetch_project_done_map(object(), "pid") == {}


def test_fetch_project_done_map_empty_schedule_returns_empty(monkeypatch):
    class _Est:
        id = "EST1"
    monkeypatch.setattr(spine_done, "fetch_estimate", lambda c, pid: _Est())
    monkeypatch.setattr(spine_done, "fetch_schedule", lambda c, eid: [])
    assert spine_done.fetch_project_done_map(object(), "pid") == {}
