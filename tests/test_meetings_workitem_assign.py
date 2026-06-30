"""Tests for `assign_work_item` (meetings-as-sources work-item confidence gate).

`assign_work_item` is the PURE confidence-gate behind the `assigner` seam that
`link_meeting` calls. The actual classifier (an LLM routing a meeting to one of
the project's estimator work items) is a LATER webhook-layer task — here it is
always INJECTED as a plain function. The gate auto-attaches a work item only on
high confidence (>= threshold); below threshold it leaves the work item
unassigned (UI shows "needs assignment") while still surfacing the best-guess
confidence. NO LLM, NO Supabase, NO network — pure logic.
"""
from __future__ import annotations

from cp_engine.meetings import WORK_ITEM_ASSIGN_THRESHOLD, assign_work_item

_ROW = {"recording_id": 12345, "title": "Kickoff"}


def _classifier(result):
    """Build a fake classifier that records its call and returns `result`."""
    calls = []

    def _fake(meeting_row, project_id):
        calls.append((meeting_row, project_id))
        return result

    _fake.calls = calls
    return _fake


def test_threshold_constant_value():
    assert WORK_ITEM_ASSIGN_THRESHOLD == 0.75


def test_classifier_returns_none():
    clf = _classifier(None)
    assert assign_work_item(_ROW, "p1", classifier=clf) == (None, None)
    # classifier was called with (meeting_row, project_id).
    assert clf.calls == [(_ROW, "p1")]


def test_confidence_at_threshold_is_inclusive():
    # Boundary: confidence == threshold (0.75) → assigned (>= is inclusive).
    clf = _classifier(("wi-1", 0.75))
    assert assign_work_item(_ROW, "p1", classifier=clf) == ("wi-1", 0.75)


def test_confidence_above_threshold_assigned():
    clf = _classifier(("wi-1", 0.9))
    assert assign_work_item(_ROW, "p1", classifier=clf) == ("wi-1", 0.9)


def test_confidence_below_threshold_drops_id_keeps_confidence():
    # Below threshold: work_item_id dropped, confidence RETAINED for UI.
    clf = _classifier(("wi-1", 0.6))
    assert assign_work_item(_ROW, "p1", classifier=clf) == (None, 0.6)


def test_custom_threshold_kwarg_respected():
    # threshold=0.5 lets a 0.6 candidate through.
    clf = _classifier(("wi-1", 0.6))
    assert assign_work_item(_ROW, "p1", classifier=clf, threshold=0.5) == ("wi-1", 0.6)


def test_dict_shape_handled_like_tuple():
    clf = _classifier({"work_item_id": "wi-1", "confidence": 0.9})
    assert assign_work_item(_ROW, "p1", classifier=clf) == ("wi-1", 0.9)


def test_dict_shape_below_threshold():
    clf = _classifier({"work_item_id": "wi-1", "confidence": 0.6})
    assert assign_work_item(_ROW, "p1", classifier=clf) == (None, 0.6)


def test_tuple_confidence_none():
    # A candidate with confidence None → (None, None).
    clf = _classifier(("wi-1", None))
    assert assign_work_item(_ROW, "p1", classifier=clf) == (None, None)


def test_tuple_missing_work_item_id():
    # A candidate with no work_item_id → (None, None).
    clf = _classifier((None, 0.9))
    assert assign_work_item(_ROW, "p1", classifier=clf) == (None, None)


def test_dict_missing_work_item_id():
    clf = _classifier({"confidence": 0.9})
    assert assign_work_item(_ROW, "p1", classifier=clf) == (None, None)


def test_never_raises_when_classifier_raises():
    def _boom(meeting_row, project_id):
        raise RuntimeError("classifier exploded")

    assert assign_work_item(_ROW, "p1", classifier=_boom) == (None, None)


def test_confidence_zero_reaches_gate():
    # 0.0 is falsy but a VALID confidence: the guard is `confidence is None`,
    # not `not confidence`, so a zero-confidence candidate must reach the gate
    # and return (None, 0.0) — id dropped (below threshold) but confidence
    # retained, NOT misclassified as "no candidate". A refactor to
    # `if not confidence:` would break this while passing every other test.
    clf = _classifier(("wi-1", 0.0))
    assert assign_work_item(_ROW, "p1", classifier=clf) == (None, 0.0)


def test_malformed_tuple_is_swallowed():
    # A non-2-element tuple raises on destructure; the never-raises wrapper
    # turns it into (None, None) rather than propagating.
    clf = _classifier(("wi-1", 0.9, "extra"))
    assert assign_work_item(_ROW, "p1", classifier=clf) == (None, None)
