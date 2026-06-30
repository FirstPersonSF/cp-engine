"""Tests for `project_sources.list_project_meetings` — the meetings-as-sources
list helper. Mirrors the spine-list test style: an injected fake client whose
.table(...).select(...).eq(...).order(...).execute() chain returns canned rows.

Key invariants under test:
  - *_at fields → derived booleans (summary_embedded / transcript_promoted),
  - NEVER selects the heavy `transcript` column and never `select *`,
  - empty project → [],
  - filters by project_id (the .eq filter is captured and asserted).
"""

from __future__ import annotations

from cp_engine.project_sources import list_project_meetings


class _FakeQuery:
    """Records the select cols + eq filter, returns canned rows on execute()."""

    def __init__(self, captured, rows):
        self._captured = captured
        self._rows = rows

    def select(self, cols):
        self._captured["select"] = cols
        return self

    def eq(self, col, val):
        self._captured.setdefault("eq", []).append((col, val))
        return self

    def order(self, col, desc=False):
        self._captured["order"] = (col, desc)
        return self

    def execute(self):
        return type("Resp", (), {"data": self._rows})()


class _FakeClient:
    def __init__(self, rows, captured):
        self._rows = rows
        self._captured = captured

    def table(self, name):
        self._captured["table"] = name
        return _FakeQuery(self._captured, self._rows)


def test_derives_booleans_from_at_fields():
    rows = [
        {
            "recording_id": "rec-1",
            "title": "Kickoff",
            "meeting_date": "2026-06-20",
            "work_item_id": "wi-1",
            "fathom_url": "https://fathom/1",
            "summary_embedded_at": "2026-06-21T00:00:00Z",
            "transcript_promoted_at": "2026-06-22T00:00:00Z",
        },
        {
            "recording_id": "rec-2",
            "title": "Neither",
            "meeting_date": "2026-06-19",
            "work_item_id": None,
            "fathom_url": "https://fathom/2",
            "summary_embedded_at": None,
            "transcript_promoted_at": None,
        },
        {
            "recording_id": "rec-3",
            "title": "Summary only",
            "meeting_date": "2026-06-18",
            "work_item_id": "wi-3",
            "fathom_url": "https://fathom/3",
            "summary_embedded_at": "2026-06-19T00:00:00Z",
            "transcript_promoted_at": None,
        },
    ]
    captured = {}
    out = list_project_meetings(_FakeClient(rows, captured), "pid")

    assert out[0]["summary_embedded"] is True
    assert out[0]["transcript_promoted"] is True
    assert out[1]["summary_embedded"] is False
    assert out[1]["transcript_promoted"] is False
    assert out[2]["summary_embedded"] is True
    assert out[2]["transcript_promoted"] is False

    # Identity fields pass through.
    assert out[0]["recording_id"] == "rec-1"
    assert out[0]["title"] == "Kickoff"
    assert out[0]["meeting_date"] == "2026-06-20"
    assert out[0]["work_item_id"] == "wi-1"
    assert out[0]["fathom_url"] == "https://fathom/1"


def test_never_selects_transcript_or_star():
    captured = {}
    list_project_meetings(_FakeClient([], captured), "pid")
    cols = captured["select"]
    assert cols != "*"
    # Tokenize on commas so `transcript_promoted_at` (which legitimately
    # contains "transcript") doesn't trip the bare-column check.
    tokens = {c.strip() for c in cols.split(",")}
    assert "transcript" not in tokens  # never the heavy jsonb blob
    assert "summary" not in tokens  # never the large full summary text in a list


def test_empty_project_returns_empty_list():
    captured = {}
    assert list_project_meetings(_FakeClient([], captured), "pid") == []


def test_filters_by_project_id():
    captured = {}
    list_project_meetings(_FakeClient([], captured), "the-pid")
    assert ("project_id", "the-pid") in captured["eq"]
    assert captured["table"] == "fathom_meetings"
    # Ordered by meeting_date, most recent first.
    assert captured["order"] == ("meeting_date", True)


def test_missing_fields_default_safely():
    """A row missing fields → bools False, values None (defensive)."""
    captured = {}
    out = list_project_meetings(_FakeClient([{"recording_id": "rec-x"}], captured), "pid")
    assert out[0]["recording_id"] == "rec-x"
    assert out[0]["title"] is None
    assert out[0]["summary_embedded"] is False
    assert out[0]["transcript_promoted"] is False
