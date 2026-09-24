"""#300 — the dual-shape reader.

mc-2 migration 190 adds `projects.parent_id`, 192 merges `initiatives` into
`projects`, 194 retires the `initiatives` table. Every installed cp surface
has to keep working on BOTH sides of that cutover, so `read_projects` probes
the schema once per client and picks ONE stream or THREE, and every
`initiatives` lookup is gated on the same answer.

These tests drive a fake PostgREST client that records which tables were
queried. The control (`test_legacy_schema_reads_three_streams`) is what the
suite looked like before #300; the workstream cases are the new contract.
"""

from __future__ import annotations

import pytest

import cp_engine.mc2_db as mc2_db
from cp_engine import commitments, sync_mc2
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


def _legacy_project(**kw):
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
        "companies": _GGL,
        "repos": [],
    }
    base.update(kw)
    return base


def _ws(**kw):
    """A workstream-schema row: a legacy row plus `parent_id`."""
    row = _legacy_project()
    row["parent_id"] = None
    row.update(kw)
    return row


# ------------------------------------------------------------------ probe


def test_probe_reads_legacy_when_parent_id_missing():
    c = _Client({"projects": [_legacy_project()]})
    assert mc2_db.workstream_schema(c) is False
    assert mc2_db.has_initiatives_table(c) is True


def test_probe_reads_workstream_when_parent_id_present_even_if_null():
    c = _Client({"projects": [_ws()]})
    assert mc2_db.workstream_schema(c) is True
    assert mc2_db.has_initiatives_table(c) is False


def test_probe_reads_legacy_on_error_and_caches_per_client():
    c = _Client({})  # no projects table at all → raises
    assert mc2_db.workstream_schema(c) is False
    n = len(c.calls)
    assert mc2_db.workstream_schema(c) is False
    assert len(c.calls) == n, "second probe must come from the cache"


# ------------------------------------------------------------------ reader


def test_legacy_schema_reads_three_streams():
    """CONTROL: the pre-#300 behaviour, byte-for-byte."""
    c = _Client(
        {
            "projects": [_legacy_project()],
            "repos": [],
            "initiatives": [
                {
                    "id": "i-mc",
                    "code": "mission-control",
                    "name": "Mission Control",
                    "status": "Active",
                    "owner": None,
                    "updated_at": "2026-09-01T00:00:00+00:00",
                    "companies": _FP,
                    "repos": [],
                }
            ],
        }
    )
    states = _backend(c).read_projects(_cfg())
    queried = [t for t, _ in c.calls]
    assert queried.count("initiatives") == 1
    assert queried.count("repos") == 1
    by_code = {s.code: s for s in states}
    assert by_code["mission-control"].source == "initiative"
    assert by_code["mission-control"].status == "Active"
    assert by_code["ggl-5136-go-safety-website"].source == "engagement"
    assert by_code["ggl-5136-go-safety-website"].has_agreement is True


def test_workstream_schema_reads_one_stream_and_never_touches_initiatives():
    c = _Client(
        {
            "projects": [_ws()],
            "initiatives": [{"id": "boom"}],  # present but must not be read
            "repos": [{"id": "boom"}],
        }
    )
    states = _backend(c).read_projects(_cfg())
    queried = [t for t, _ in c.calls]
    assert "initiatives" not in queried
    assert "repos" not in queried
    assert [s.code for s in states] == ["ggl-5136-go-safety-website"]
    # the sync read asked for parent_id explicitly (never `*`)
    cols = [cols for t, cols in c.calls if t == "projects"][-1]
    assert "parent_id" in cols and "*" not in cols


def test_workstream_rows_derive_parent_label_and_hold_back_account_node():
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

    # the account node is held back until #302 gives it a working dir
    assert "ggl-5201-google" not in by_code
    assert by_code["ggl-5202-go-safety"].label == "program"
    # the parent code still names the held-back account: a child directly
    # under it must not read as parentless, or it would label "account"
    assert by_code["ggl-5202-go-safety"].parent_code == "ggl-5201-google"
    assert by_code["ggl-5202-go-safety"].has_agreement is False

    j = by_code["ggl-5136-go-safety-website"]
    assert j.label == "job"
    assert j.parent_code == "ggl-5202-go-safety"
    assert j.source == "engagement"

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


def test_workstream_internal_rows_keep_the_initiative_shape_until_301():
    mc = _ws(
        id="i-mc",
        number=9005,
        full_job_name="1PI 9005 Mission Control",
        name="Mission Control",
        deal_stage=None,
        budget=None,
        is_internal=True,
        mc_status="Holding",
        companies=_FP,
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
    assert s.source == "initiative"  # initiative-cp.md.j2 keeps rendering it
    assert s.status == "On hold"  # mapped into the initiative vocabulary
    assert s.is_internal is False  # the scaffolding loop must NOT skip it
    assert s.label == "initiative"
    assert s.has_agreement is False
    assert [r.repo_name for r in s.linked_repos] == ["mc-2"]


def test_workstream_self_company_row_with_children_is_a_program():
    parent = _ws(
        id="i-mc",
        number=9005,
        full_job_name="1PI 9005 Mission Control",
        deal_stage=None,
        companies=_FP,
    )
    child = _ws(
        id="i-cp",
        number=9006,
        full_job_name="1PI 9006 CP",
        deal_stage=None,
        companies=_FP,
        parent_id="i-mc",
    )
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


# ------------------------------------------------------------------ gates


def test_commitment_owner_slug_lookup_skips_initiatives_on_workstream_schema():
    c = _Client({"projects": [_ws()]})  # no initiatives table
    assert commitments.resolve_commitment_owner(c, "mission-control") is None
    assert "initiatives" not in [t for t, _ in c.calls]


def test_commitment_owner_slug_lookup_still_reads_initiatives_on_legacy():
    c = _Client(
        {
            "projects": [_legacy_project()],
            "initiatives": [{"id": "i-mc", "code": "mission-control"}],
        }
    )
    owner = commitments.resolve_commitment_owner(c, "mission-control")
    assert owner == {"id": "i-mc", "code": "mission-control", "kind": "initiative"}


def test_commitment_owner_numbered_internal_code_resolves_as_project():
    """Post-merge codes carry a number, so they resolve on the projects branch."""
    row = _ws(id="i-mc", number=9005, full_job_name="1PI 9005 Mission Control", companies=_FP)
    c = _Client({"projects": [row]})
    owner = commitments.resolve_commitment_owner(c, "1pi-9005-mission-control")
    assert owner == {"id": "i-mc", "code": "1pi-9005-mission-control", "kind": "project"}


def test_resolve_initiative_id_returns_none_without_the_table():
    c = _Client({"projects": [_ws()]})
    assert mc2_db._resolve_initiative_id(c, "mission-control") is None
    assert "initiatives" not in [t for t, _ in c.calls]


def test_resolve_project_id_uuid_branch_skips_initiatives_on_workstream_schema():
    uid = "4e39be45-0000-4000-8000-000000000001"
    c = _Client({"projects": [_ws(id=uid)]})
    assert mc2_db._resolve_project_id(c, uid) == uid
    assert "initiatives" not in [t for t, _ in c.calls]
