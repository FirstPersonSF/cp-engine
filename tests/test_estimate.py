from datetime import date

from cp_engine.estimate import (
    Estimate,
    EstimateItem,
    ScheduleItem,
    fetch_estimate,
    fetch_schedule,
    native_schedule_events,
    schedule_for_item,
    week_to_date,
)


def test_week_to_date():
    assert week_to_date("2026-04-27", 0) == date(2026, 4, 27)
    assert week_to_date("2026-04-27", 7) == date(2026, 6, 15)  # +49 days
    assert week_to_date(None, 7) is None
    assert week_to_date("2026-04-27", None) is None
    # start_week may arrive as a float (numeric DB column).
    assert week_to_date("2026-04-27", 7.0) == date(2026, 6, 15)


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


def test_from_rows_missing_required_key_raises_value_error():
    import pytest

    project_row = {"id": "est-1", "mc_project_id": "mc-1", "name": "Estimate 1"}
    phases = [{"id": "ph-0", "name": "Phase 0", "overview": "…", "position": 0}]
    # Activity row missing the required "name" key.
    activities = [{"id": "a-0", "phase_id": "ph-0", "position": 0}]
    with pytest.raises(ValueError) as exc:
        Estimate.from_rows(project_row, phases, activities, [])
    assert "name" in str(exc.value)


def test_from_rows_drops_item_with_unknown_phase_id():
    project_row = {"id": "est-1", "mc_project_id": "mc-1", "name": "Estimate 1"}
    phases = [{"id": "ph-0", "name": "Phase 0", "overview": "…", "position": 0}]
    activities = [
        {"id": "a-0", "phase_id": "ph-0", "name": "Known", "position": 0, "library_item_id": None},
        {"id": "a-orphan", "phase_id": "ph-ghost", "name": "Orphan", "position": 0, "library_item_id": None},
    ]
    est = Estimate.from_rows(project_row, phases, activities, [])
    # Orphan dropped, not crashed on, and no phantom phase created.
    assert [p.id for p in est.phases] == ["ph-0"]
    assert {i.id for i in est.all_items()} == {"a-0"}


# ── Fake Supabase client for fetch_estimate (no network) ──────────────────


class _FakeQuery:
    """Records a single table query's schema/table/columns/filters and
    returns canned `.data` on .execute(). Mirrors the supabase-py builder
    chain used in sync_mc2.py: schema().table().select().eq()/.in_().execute()."""

    def __init__(self, client, table, schema_name="estimator"):
        self.client = client
        self.table = table
        self.schema_name = schema_name
        self.columns = None
        self.eq_filters = {}
        self.in_filters = {}

    def select(self, columns):
        self.columns = columns
        return self

    def eq(self, col, val):
        self.eq_filters[col] = val
        return self

    def in_(self, col, vals):
        self.in_filters[col] = list(vals)
        return self

    def execute(self):
        self.client.queries.append(self)
        # Prefer a schema-qualified key ("public.projects") so the estimator and
        # public `projects` tables don't collide; fall back to the bare table
        # name for the existing estimator canned tables.
        rows = self.client.tables.get(
            f"{self.schema_name}.{self.table}",
            self.client.tables.get(self.table, []),
        )
        # Apply eq + in filters so the fake behaves like PostgREST.
        out = []
        for r in rows:
            if all(r.get(k) == v for k, v in self.eq_filters.items()):
                if all(r.get(k) in vs for k, vs in self.in_filters.items()):
                    out.append(r)
        return _FakeResult(out)


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeSchema:
    def __init__(self, client, schema_name):
        self.client = client
        self.schema_name = schema_name

    def table(self, name):
        # Record (schema, table) so tests can assert the estimator schema.
        self.client.schemas_used.add(self.schema_name)
        return _FakeQuery(self.client, name, self.schema_name)


class _FakeClient:
    def __init__(self, tables):
        self.tables = tables
        self.queries = []
        self.schemas_used = set()

    def schema(self, name):
        return _FakeSchema(self, name)


def _canned_tables():
    return {
        "projects": [
            {"id": "est-1", "mc_project_id": "mc-1", "name": "Estimate 1", "is_default": True},
            {"id": "est-other", "mc_project_id": "mc-2", "name": "Estimate 1", "is_default": True},
            {"id": "est-draft", "mc_project_id": "mc-1", "name": "Draft", "is_default": False},
        ],
        "phases": [
            {"id": "ph-0", "project_id": "est-1", "name": "Phase 0", "overview": "o", "position": 0},
            {"id": "ph-1", "project_id": "est-1", "name": "Phase 1", "overview": "o", "position": 1},
            {"id": "ph-x", "project_id": "est-other", "name": "Other", "overview": "o", "position": 0},
        ],
        "phase_activities": [
            {"id": "a-0", "phase_id": "ph-0", "name": "Act 0", "short_description": "s", "position": 0, "library_item_id": None},
            {"id": "a-x", "phase_id": "ph-x", "name": "Other act", "short_description": "s", "position": 0, "library_item_id": None},
        ],
        "phase_deliverables": [
            {"id": "d-0", "phase_id": "ph-1", "name": "Del 0", "short_description": "s", "position": 0, "library_item_id": None},
        ],
        # public.projects — keyed by mc_project_id, carries the kickoff start_date.
        "public.projects": [
            {"id": "mc-1", "start_date": "2026-04-27"},
        ],
    }


def test_fetch_estimate_returns_built_default_estimate():
    client = _FakeClient(_canned_tables())
    est = fetch_estimate(client, "mc-1")

    assert isinstance(est, Estimate)
    assert est.id == "est-1"
    assert est.mc_project_id == "mc-1"
    assert [p.name for p in est.phases] == ["Phase 0", "Phase 1"]
    # Children scoped to this estimate's phases only (no est-other rows).
    assert {i.id for i in est.all_items()} == {"a-0", "d-0"}
    assert est.item_by_id("a-0").kind == "activity"
    assert est.item_by_id("d-0").kind == "deliverable"

    # Queries hit the estimator schema (+ public for start_date), never `*`.
    assert client.schemas_used == {"estimator", "public"}
    for q in client.queries:
        assert q.columns is not None and "*" not in q.columns

    # The estimator projects query filtered to default for this mc_project_id.
    proj_q = next(
        q for q in client.queries
        if q.table == "projects" and q.schema_name == "estimator"
    )
    assert proj_q.eq_filters == {"mc_project_id": "mc-1", "is_default": True}


def test_from_rows_accepts_start_date_kwarg():
    project_row = {"id": "est-1", "mc_project_id": "mc-1", "name": "Estimate 1"}
    est = Estimate.from_rows(project_row, [], [], [], start_date="2026-04-27")
    assert est.start_date == "2026-04-27"
    # Defaults to None when not passed.
    est2 = Estimate.from_rows(project_row, [], [], [])
    assert est2.start_date is None


def test_fetch_estimate_reads_start_date():
    client = _FakeClient(_canned_tables())
    est = fetch_estimate(client, "mc-1")
    assert est.start_date == "2026-04-27"

    # The start_date read hit public.projects by mc_project_id, explicit columns.
    sd_q = next(
        q for q in client.queries
        if q.table == "projects" and q.schema_name == "public"
    )
    assert sd_q.eq_filters == {"id": "mc-1"}
    assert sd_q.columns is not None and "*" not in sd_q.columns


def test_fetch_estimate_start_date_none_when_absent():
    # public.projects has no row / no start_date for this project → None.
    tables = _canned_tables()
    tables["public.projects"] = []
    client = _FakeClient(tables)
    est = fetch_estimate(client, "mc-1")
    assert est.start_date is None


def test_fetch_schedule_orders_and_defaults_absent_columns():
    # Two bars, out of order; one milestone. The work_item_*/done columns are
    # absent from the rows (not yet in the live schema) → default None/False.
    tables = {
        "schedule_items": [
            {"id": "s-late", "project_id": "est-1", "phase_id": "ph-1",
             "label": "Storybuilding", "start_week": 3.0, "duration": 2.0,
             "position": 0, "item_type": "activity", "emphasis": "important"},
            {"id": "s-early", "project_id": "est-1", "phase_id": "ph-0",
             "label": "Kickoff", "start_week": 0.0, "duration": 1.0,
             "position": 0, "item_type": "milestone", "emphasis": None},
            {"id": "s-other", "project_id": "est-other", "phase_id": "ph-x",
             "label": "Not ours", "start_week": 0.0, "duration": 1.0,
             "position": 0, "item_type": "activity", "emphasis": None},
        ],
    }
    client = _FakeClient(tables)
    items = fetch_schedule(client, "est-1")

    # Scoped to est-1, ordered by (start_week, position).
    assert [i.id for i in items] == ["s-early", "s-late"]
    early = items[0]
    assert isinstance(early, ScheduleItem)
    assert early.item_type == "milestone"
    assert early.start_week == 0.0 and early.duration == 1.0
    # Not-yet-existing columns default safely.
    assert early.work_item_id is None
    assert early.work_item_kind is None
    assert early.done is False

    # Filtered by project_id == estimate_id, estimator schema, explicit cols.
    assert client.schemas_used == {"estimator"}
    q = next(q for q in client.queries if q.table == "schedule_items")
    assert q.eq_filters == {"project_id": "est-1"}
    assert q.columns is not None and "*" not in q.columns


def test_fetch_schedule_coerces_numeric_and_reads_present_work_item():
    tables = {
        "schedule_items": [
            {"id": "s-1", "project_id": "est-1", "phase_id": "ph-0",
             "label": "Bar", "start_week": "2", "duration": "1",
             "position": 0, "item_type": "activity", "emphasis": None,
             "work_item_id": "wi-9", "work_item_kind": "deliverable", "done": True},
        ],
    }
    client = _FakeClient(tables)
    items = fetch_schedule(client, "est-1")
    assert len(items) == 1
    it = items[0]
    # numeric coercion from string-y numerics.
    assert it.start_week == 2.0 and it.duration == 1.0
    # present work-item columns are read through.
    assert it.work_item_id == "wi-9"
    assert it.work_item_kind == "deliverable"
    assert it.done is True


def test_fetch_schedule_none_position_sorts_as_zero():
    # A row with an explicit NULL position (position: None) alongside a normal
    # row must not crash sorted() with a TypeError — None is treated as 0.
    tables = {
        "schedule_items": [
            {"id": "s-nullpos", "project_id": "est-1", "phase_id": "ph-1",
             "label": "Null pos", "start_week": 1.0, "duration": 1.0,
             "position": None, "item_type": "activity", "emphasis": None},
            {"id": "s-onepos", "project_id": "est-1", "phase_id": "ph-1",
             "label": "Pos one", "start_week": 1.0, "duration": 1.0,
             "position": 1, "item_type": "activity", "emphasis": None},
        ],
    }
    client = _FakeClient(tables)
    items = fetch_schedule(client, "est-1")
    # Sorts without raising; the None-position row orders as position 0 → first.
    assert [i.id for i in items] == ["s-nullpos", "s-onepos"]


# ── Pure schedule-bar ↔ work-item joins (Task 2.3) ────────────────────────


def _mixed_schedule():
    """A schedule list mixing linked bars (work_item_id set) and schedule-native
    events (work_item_id None: a milestone + a holiday), deliberately out of
    start_week order. One work-item ("wi-A") owns TWO bars (1:N is allowed)."""
    return [
        ScheduleItem(
            id="bar-A-late", label="A continued", phase_id="ph-0",
            start_week=4.0, duration=2.0, item_type="activity", emphasis=None,
            work_item_id="wi-A", work_item_kind="activity",
        ),
        ScheduleItem(
            id="bar-holiday", label="Holiday", phase_id=None,
            start_week=2.0, duration=1.0, item_type="holiday", emphasis=None,
            work_item_id=None, work_item_kind=None,
        ),
        ScheduleItem(
            id="bar-A-early", label="A start", phase_id="ph-0",
            start_week=1.0, duration=2.0, item_type="activity", emphasis=None,
            work_item_id="wi-A", work_item_kind="activity",
        ),
        ScheduleItem(
            id="bar-B", label="B", phase_id="ph-1",
            start_week=3.0, duration=1.0, item_type="deliverable", emphasis=None,
            work_item_id="wi-B", work_item_kind="deliverable",
        ),
        ScheduleItem(
            id="bar-milestone", label="Kickoff", phase_id=None,
            start_week=0.0, duration=0.0, item_type="milestone", emphasis=None,
            work_item_id=None, work_item_kind=None,
        ),
    ]


def test_schedule_for_item_returns_multiple_bars_ordered_by_start_week():
    bars = schedule_for_item(_mixed_schedule(), "wi-A")
    # Both wi-A bars, ordered by start_week (early before late).
    assert [b.id for b in bars] == ["bar-A-early", "bar-A-late"]
    assert all(b.work_item_id == "wi-A" for b in bars)


def test_schedule_for_item_single_bar():
    bars = schedule_for_item(_mixed_schedule(), "wi-B")
    assert [b.id for b in bars] == ["bar-B"]


def test_schedule_for_item_empty_when_no_bar():
    # An item with no schedule bar → empty (not an error).
    assert schedule_for_item(_mixed_schedule(), "wi-nope") == ()
    # Native events (work_item_id None) are never returned for a real item_id.
    assert schedule_for_item(_mixed_schedule(), None) == ()


def test_native_schedule_events_returns_only_unlinked_ordered():
    events = native_schedule_events(_mixed_schedule())
    # Only the work_item_id-None bars (milestone + holiday), ordered by start_week.
    assert [e.id for e in events] == ["bar-milestone", "bar-holiday"]
    assert all(e.work_item_id is None for e in events)
    # Linked bars are excluded.
    assert "bar-A-early" not in {e.id for e in events}


def test_native_schedule_events_empty_when_all_linked():
    linked_only = [b for b in _mixed_schedule() if b.work_item_id is not None]
    assert native_schedule_events(linked_only) == ()


def test_fetch_estimate_returns_none_when_no_default():
    client = _FakeClient(_canned_tables())
    assert fetch_estimate(client, "mc-nonexistent") is None


def test_fetch_estimate_with_zero_phases_skips_child_queries():
    # A default estimate that has no phases at all → the `if phase_ids:` else
    # branch: empty phases, and NO child-table queries fired.
    tables = {
        "projects": [
            {"id": "est-empty", "mc_project_id": "mc-empty", "name": "Estimate 1", "is_default": True},
        ],
        "phases": [],
        "phase_activities": [],
        "phase_deliverables": [],
    }
    client = _FakeClient(tables)
    est = fetch_estimate(client, "mc-empty")

    assert isinstance(est, Estimate)
    assert est.id == "est-empty"
    assert est.phases == ()
    # No child (activities/deliverables) query was ever recorded.
    queried_tables = {q.table for q in client.queries}
    assert "phase_activities" not in queried_tables
    assert "phase_deliverables" not in queried_tables
