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


# ── Fake Supabase client for fetch_estimate (no network) ──────────────────


class _FakeQuery:
    """Records a single table query's schema/table/columns/filters and
    returns canned `.data` on .execute(). Mirrors the supabase-py builder
    chain used in sync_mc2.py: schema().table().select().eq()/.in_().execute()."""

    def __init__(self, client, table):
        self.client = client
        self.table = table
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
        rows = self.client.tables.get(self.table, [])
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
        return _FakeQuery(self.client, name)


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

    # All queries hit the estimator schema, never `*`.
    assert client.schemas_used == {"estimator"}
    for q in client.queries:
        assert q.columns is not None and "*" not in q.columns

    # The projects query filtered to default for this mc_project_id.
    proj_q = next(q for q in client.queries if q.table == "projects")
    assert proj_q.eq_filters == {"mc_project_id": "mc-1", "is_default": True}


def test_fetch_estimate_returns_none_when_no_default():
    client = _FakeClient(_canned_tables())
    assert fetch_estimate(client, "mc-nonexistent") is None
