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
    """In-memory PostgREST subset: select/eq/overlaps/ilike/limit/order."""

    def __init__(self, captured, rows, name):
        self._captured = captured
        self._rows = list(rows)
        self._name = name
        self._limit = None

    def select(self, cols):
        if self._name == "fathom_meetings":
            self._captured["select"] = cols
        return self

    def eq(self, col, val):
        if self._name == "fathom_meetings":
            self._captured.setdefault("eq", []).append((col, val))
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def ilike(self, col, val):
        self._rows = [r for r in self._rows if str(r.get(col, "")).lower() == val.lower()]
        return self

    def overlaps(self, col, vals):
        self._captured.setdefault("overlaps", []).append((col, list(vals)))
        self._rows = [r for r in self._rows if set(r.get(col) or []) & set(vals)]
        return self

    def limit(self, n):
        self._limit = n
        return self

    def order(self, col, desc=False):
        self._captured["order"] = (col, desc)
        return self

    def execute(self):
        rows = self._rows[: self._limit] if self._limit else self._rows
        return type("Resp", (), {"data": rows})()


class _FakeClient:
    """`rows` are fathom_meetings rows, all linked to "pid" unless they say
    otherwise; `projects`/`companies` default to one SAP project."""

    def __init__(self, rows, captured, projects=None, companies=None):
        self._tables = {
            "fathom_meetings": [
                {"id": f"m{i}", "project_id": "pid", "project_tags": [], **r}
                for i, r in enumerate(rows)
            ],
            "projects": projects if projects is not None else [{
                "id": "pid", "code": "SAP-enablement", "number": 5238,
                "full_job_name": "SAP 5238 Enablement", "company_id": "c-sap",
            }],
            "companies": companies if companies is not None else [
                {"id": "c-sap", "code": "SAP"}],
        }
        self._captured = captured

    def table(self, name):
        if name == "fathom_meetings":
            self._captured["table"] = name
        return _FakeQuery(self._captured, self._tables.get(name, []), name)


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


def test_newest_first():
    rows = [{"recording_id": "old", "meeting_date": "2026-01-01"},
            {"recording_id": "new", "meeting_date": "2026-11-01"}]
    out = list_project_meetings(_FakeClient(rows, {}), "pid")
    assert [m["recording_id"] for m in out] == ["new", "old"]


def test_a_meeting_tagged_but_never_linked_is_listed():
    """sap-5238 on 2026-11-10: the 10-23 enablement meeting carried the tag
    `sap-5238-enablement` and no project_id (the webhook's link step never
    wrote one), so every project_id-only read said the project had no
    meetings. The tag is the assignment."""
    rows = [
        {"recording_id": "linked", "meeting_date": "2026-10-01"},
        {"recording_id": "slug-tag", "meeting_date": "2026-10-23",
         "project_id": None, "project_tags": ["sap-5238-enablement"]},
        {"recording_id": "display-tag", "meeting_date": "2026-10-24",
         "project_id": None, "project_tags": ["SAP 5238 Enablement"]},
        {"recording_id": "short-tag", "meeting_date": "2026-10-25",
         "project_id": None, "project_tags": ["sap-5238"]},
        {"recording_id": "someone-else", "meeting_date": "2026-10-26",
         "project_id": None, "project_tags": ["sap-5198-2027-ad-videos"]},
    ]
    out = list_project_meetings(_FakeClient(rows, {}), "pid")
    ids = {m["recording_id"] for m in out}
    assert ids == {"linked", "slug-tag", "display-tag", "short-tag"}


def test_a_meeting_both_linked_and_tagged_is_listed_once():
    rows = [{"recording_id": "both", "meeting_date": "2026-10-01",
             "project_tags": ["sap-5238-enablement"]}]
    out = list_project_meetings(_FakeClient(rows, {}), "pid")
    assert [m["recording_id"] for m in out] == ["both"]


def test_missing_fields_default_safely():
    """A row missing fields → bools False, values None (defensive)."""
    captured = {}
    out = list_project_meetings(_FakeClient([{"recording_id": "rec-x"}], captured), "pid")
    assert out[0]["recording_id"] == "rec-x"
    assert out[0]["title"] is None
    assert out[0]["summary_embedded"] is False
    assert out[0]["transcript_promoted"] is False
