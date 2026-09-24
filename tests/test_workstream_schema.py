"""#300/#301 — the workstream reader, one entry kind.

mc-2 migration 190 added `projects.parent_id`, 192 merged `initiatives` into
`projects` (keeping the initiative uuids), 194 retired the `initiatives`
table. v0.123.x carried a dual-shape reader across that cutover; since #301
the workstream schema is the ONLY schema: `read_projects` is one query of
`projects`, every row comes through as the same `ProjectState` shape, and
every owner-scoped table has exactly one owner column, `project_id`.

These tests drive a fake PostgREST client that records which tables were
queried, so a re-introduced second read (`initiatives`, standalone `repos`)
fails here, not in production.
"""

from __future__ import annotations

import pytest

import cp_engine.mc2_db as mc2_db
from cp_engine import commitments
from cp_engine.state import derive_label
from cp_engine.sync_mc2 import MC2Backend, workstream_rows_to_states


# ------------------------------------------------------------------ fake


class _Query:
    def __init__(self, client, table):
        self._c = client
        self._t = table
        self._filters: list[tuple] = []

    def select(self, cols):
        self._cols = cols
        return self

    def eq(self, k, v):
        self._filters.append(("eq", k, v))
        return self

    def neq(self, k, v):
        self._filters.append(("neq", k, v))
        return self

    def is_(self, k, v):
        self._filters.append(("is", k, v))
        return self

    def in_(self, k, vals):
        self._filters.append(("in", k, list(vals)))
        return self

    def ilike(self, k, pattern):
        prefix = pattern.rstrip("%").lower()
        self._filters.append(("ilike", k, prefix))
        return self

    def order(self, *a, **kw):
        return self

    def limit(self, n):
        return self

    def execute(self):
        self._c.calls.append((self._t, self._cols))
        if self._t not in self._c.tables:
            raise RuntimeError(f"relation {self._t!r} does not exist")
        rows = self._c.tables[self._t]
        for op, k, v in self._filters:
            if op == "eq":
                rows = [r for r in rows if r.get(k) == v]
            elif op == "neq":
                rows = [r for r in rows if r.get(k) != v]
            elif op == "is" and v == "null":
                rows = [r for r in rows if r.get(k) is None]
            elif op == "in":
                rows = [r for r in rows if r.get(k) in v]
            elif op == "ilike":
                rows = [r for r in rows if str(r.get(k) or "").lower().startswith(v)]
        return type("R", (), {"data": list(rows)})()


class _Client:
    """`tables` maps name → rows. Rows carry whatever keys the schema has;
    the probe reads `parent_id` off the first `projects` row, exactly as
    PostgREST returns `parent_id: null` for a real column."""

    def __init__(self, tables: dict[str, list[dict]]):
        self.tables = tables
        self.calls: list[tuple[str, str]] = []

    def schema(self, _name):
        return self

    def table(self, name):
        return _Query(self, name)


def _backend(client) -> MC2Backend:
    b = MC2Backend()
    b._client = client  # bypass creds
    return b


def _cfg():
    """`read_projects` only uses the config to build a client, and the tests
    inject one — so no tenant config is needed."""
    return None


@pytest.fixture(autouse=True)
def _fresh_probe():
    mc2_db._reset_workstream_probe()
    yield
    mc2_db._reset_workstream_probe()


# ------------------------------------------------------------------ rows

_GGL = {"code": "GGL", "name": "Google", "kind": "client"}
_FP = {"code": "1PI", "name": "First Person", "kind": "self-fpsf"}


def _ws(**kw):
    """A workstream-schema `projects` row."""
    base = {
        "id": "p-5136",
        "number": 5136,
        "full_job_name": "GGL 5136 Go Safety Website",
        "name": "Go Safety Website",
        "mc_status": "Open",
        "account_manager": "Tony Welch",
        "is_internal": False,
        "deal_stage": "Won",
        "budget": "146250",
        "updated_at": "2026-09-01T00:00:00+00:00",
        "parent_id": None,
        "companies": _GGL,
        "repos": [],
    }
    base.update(kw)
    return base


def _internal(**kw):
    """An internal workstream: self company, no agreement, `is_internal` as
    MC-2 stores it, the initiative's uuid kept by mig 192."""
    base = dict(
        id="i-mc",
        number=9005,
        full_job_name="1PI 9005 Mission Control",
        name="Mission Control",
        deal_stage=None,
        budget=None,
        is_internal=True,
        companies=_FP,
    )
    base.update(kw)
    return _ws(**base)


# ------------------------------------------------------------------ probe


def test_probe_reads_workstream_when_parent_id_present_even_if_null():
    c = _Client({"projects": [_ws()]})
    assert mc2_db.workstream_schema(c) is True


def test_probe_is_false_when_parent_id_missing_or_unreachable():
    """False is a health answer now — a database the engine no longer
    supports — not a second code path (the legacy reader is gone)."""
    legacy = _Client({"projects": [{k: v for k, v in _ws().items() if k != "parent_id"}]})
    assert mc2_db.workstream_schema(legacy) is False
    down = _Client({})
    assert mc2_db.workstream_schema(down) is False
    n = len(down.calls)
    assert mc2_db.workstream_schema(down) is False
    assert len(down.calls) == n, "second probe must come from the cache"


def test_the_reader_does_not_consult_the_probe():
    """One stream, unconditionally: no probe select, no schema branch."""
    c = _Client({"projects": [_ws()]})
    _backend(c).read_projects(_cfg())
    assert [cols for t, cols in c.calls if t == "projects" and cols == "id, parent_id"] == []


# ------------------------------------------------------------------ reader


def test_read_projects_is_one_query_of_projects_and_nothing_else():
    c = _Client(
        {
            "projects": [_ws()],
            "initiatives": [{"id": "boom"}],  # present but must never be read
            "repos": [{"id": "boom"}],
        }
    )
    states = _backend(c).read_projects(_cfg())
    queried = [t for t, _ in c.calls]
    assert queried == ["projects"]
    assert [s.code for s in states] == ["ggl-5136-go-safety-website"]
    # the sync read asks for parent_id explicitly (never `*`)
    (cols,) = [cols for t, cols in c.calls]
    assert "parent_id" in cols and "*" not in cols


def test_project_state_has_no_source_field():
    """The one-entry-kind contract in one line: nothing can branch on it."""
    (s,) = workstream_rows_to_states([_ws()])
    assert not hasattr(s, "source")


def test_workstream_rows_derive_parent_label_and_account_node_flows_through():
    account = _ws(
        id="p-ggl",
        number=5201,
        full_job_name="GGL 5201 Google",
        name="Google",
        deal_stage=None,
        budget=None,
    )
    program = _ws(
        id="p-gosafety",
        number=5202,
        full_job_name="GGL 5202 Go Safety",
        name="Go Safety",
        deal_stage=None,
        budget=None,
        parent_id="p-ggl",
    )
    job = _ws(parent_id="p-gosafety")  # 5136 under Go Safety
    job_no_budget = _ws(
        id="p-5168",
        number=5168,
        full_job_name="GGL 5168 Activation",
        name="Activation",
        deal_stage="Won",
        budget=None,
        parent_id="p-ggl",
    )
    states = workstream_rows_to_states([account, program, job, job_no_budget])
    by_code = {s.code: s for s in states}

    # the account node flows through as a workstream (#302): label
    # `account`, no parent, no agreement — sync gives it `1p/google/`
    acct = by_code["ggl-5201-google"]
    assert acct.label == "account"
    assert acct.parent_code is None
    assert acct.has_agreement is False
    assert by_code["ggl-5202-go-safety"].label == "program"
    # a child directly under the account must not read as parentless, or
    # it would label "account"
    assert by_code["ggl-5202-go-safety"].parent_code == "ggl-5201-google"
    assert by_code["ggl-5202-go-safety"].has_agreement is False

    j = by_code["ggl-5136-go-safety-website"]
    assert j.label == "job"
    assert j.parent_code == "ggl-5202-go-safety"

    # budget is NOT the agreement signal (5168: Won, no budget)
    assert by_code["ggl-5168-activation"].has_agreement is True
    assert by_code["ggl-5168-activation"].label == "job"


def test_workstream_client_job_with_missing_deal_stage_is_not_an_account_node():
    """A real job whose `deal_stage` was never filled (5168 before the data
    fix) has no children, so it stays in the tree instead of vanishing."""
    lonely = _ws(deal_stage=None)
    states = workstream_rows_to_states([lonely])
    assert [s.code for s in states] == ["ggl-5136-go-safety-website"]
    assert states[0].has_agreement is False


def test_internal_rows_come_through_as_mc2_stores_them():
    """#301 retired the initiative shape: no status mapping, no clearing of
    `is_internal`. The row is a workstream like any other; what makes it
    internal is its SHAPE (self company, no agreement), which `label`
    reads and the renderers branch on."""
    mc = _internal(
        mc_status="Holding",
        repos=[
            {
                "repo_name": "mc-2",
                "status": "Active",
                "description": "MC app",
                "github_orgs": {"name": "FirstPersonSF"},
            }
        ],
    )
    (s,) = workstream_rows_to_states([mc])
    assert s.code == "1pi-9005-mission-control"
    assert s.mc2_id == "i-mc"  # the initiative uuid survives the merge
    assert s.status == "Holding"  # the real mc_status, not "On hold"
    assert s.is_internal is True  # as stored; gates nothing any more
    assert s.company_kind == "self-fpsf"
    assert s.has_agreement is False
    assert s.label == "initiative"
    assert [r.repo_name for r in s.linked_repos] == ["mc-2"]


def test_internal_row_is_active_by_the_one_vocabulary():
    """Deal ∪ Open is active for every workstream — `is_internal=True` does
    not remove Mission Control from the active set."""
    from cp_engine.status import is_active_status

    (s,) = workstream_rows_to_states([_internal(mc_status="Open")])
    assert is_active_status(s.status) is True


def test_workstream_self_company_row_with_children_is_a_program():
    parent = _internal()
    child = _internal(id="i-cp", number=9006, full_job_name="1PI 9006 CP", parent_id="i-mc")
    by_code = {s.code: s for s in workstream_rows_to_states([parent, child])}
    assert by_code["1pi-9005-mission-control"].label == "program"
    assert by_code["1pi-9006-cp"].label == "initiative"
    assert by_code["1pi-9006-cp"].parent_code == "1pi-9005-mission-control"


# ------------------------------------------------------------------ label


@pytest.mark.parametrize(
    "kind,parent,agreement,children,expected",
    [
        ("client", None, False, True, "account"),
        ("client", None, True, True, "account"),  # account rule wins first
        ("client", "ggl-x", False, True, "program"),
        ("client", "ggl-x", True, True, "program"),  # children beat agreement
        ("client", "ggl-x", True, False, "job"),
        ("client", "ggl-x", False, False, "initiative"),
        ("self-fpsf", None, False, True, "program"),
        ("self-fpsf", None, False, False, "initiative"),
        ("self-fpsf", None, True, False, "job"),
    ],
)
def test_derive_label_first_match_wins(kind, parent, agreement, children, expected):
    assert (
        derive_label(
            company_kind=kind,
            parent_code=parent,
            has_agreement=agreement,
            has_children=children,
        )
        == expected
    )


# ------------------------------------------------------------------ owners


def test_commitment_owner_numbered_internal_code_resolves_as_project():
    """Post-merge codes carry a number, so they resolve on `projects` and
    are stamped `kind="project"` — the only kind."""
    c = _Client({"projects": [_internal()]})
    owner = commitments.resolve_commitment_owner(c, "1pi-9005-mission-control")
    assert owner == {"id": "i-mc", "code": "1pi-9005-mission-control", "kind": "project"}
    assert [t for t, _ in c.calls] == ["projects"]


def test_commitment_owner_bare_slug_resolves_to_nothing_without_a_query():
    """`mission-control` was an `initiatives.code`; that table is gone and
    a numberless slug is not a workstream code, so nothing is read."""
    c = _Client({"projects": [_internal()], "initiatives": [{"id": "boom"}]})
    assert commitments.resolve_commitment_owner(c, "mission-control") is None
    assert c.calls == []


def test_resolve_project_id_uuid_branch_reads_projects_only():
    uid = "4e39be45-0000-4000-8000-000000000001"
    c = _Client({"projects": [_ws(id=uid)], "initiatives": [{"id": uid}]})
    assert mc2_db._resolve_project_id(c, uid) == uid
    assert [t for t, _ in c.calls] == ["projects"]


def test_resolve_project_id_bare_slug_is_none():
    """The old fall-through to an initiatives lookup is gone."""
    c = _Client({"projects": [_internal()], "initiatives": [{"id": "boom"}]})
    assert mc2_db._resolve_project_id(c, "mission-control") is None
    assert "initiatives" not in [t for t, _ in c.calls]


# ------------------------------------------------------------------ owner column


def test_owner_columns_is_project_id_whatever_the_client():
    ws = _Client({"projects": [_ws()]})
    down = _Client({})
    assert mc2_db.OWNER_COLUMN == "project_id"
    assert mc2_db.owner_columns(ws) == "project_id"
    assert mc2_db.owner_columns(down) == "project_id"
    assert mc2_db.owner_filter(ws, "x") == "project_id.eq.x"
    assert "initiative_id" not in mc2_db.owner_filter(down, "x")


def test_binding_rows_read_one_owner_column():
    """The first post-migration sync skipped every sources manifest because
    the bindings read still SELECTed `initiative_id` (v0.123.1). One
    column, one query, and the kwarg for the second owner kind is gone."""
    from cp_engine.mc2_bindings import fetch_binding_rows

    c = _Client(
        {
            "projects": [_ws()],
            "project_integrations": [
                {"project_id": "p-5136", "service": "slack", "external_ref": {"id": "C1"}, "label": ""},
                {"project_id": "i-mc", "service": "slack", "external_ref": {"id": "C9"}, "label": ""},
            ],
        }
    )
    rows = fetch_binding_rows(c, project_ids=["p-5136", "i-mc"])
    selects = [cols for t, cols in c.calls if t == "project_integrations"]
    assert len(selects) == 1 and "initiative_id" not in selects[0]
    assert set(rows) == {"p-5136", "i-mc"}
    with pytest.raises(TypeError):
        fetch_binding_rows(c, project_ids=[], initiative_ids=["i-mc"])  # type: ignore[call-arg]
