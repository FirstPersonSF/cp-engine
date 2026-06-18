from cp_engine.estimate import Estimate, EstimateItem, fetch_estimate


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
