"""#306 — the engine never reads `projects.is_internal`.

`projects.is_internal` still exists in the shared database until mc-2 PROD is
promoted and a later migration drops it. The engine must work on BOTH sides
of that drop, which it does only by never naming the column: a PostgREST
select that names a dropped column is an APIError (42703), not a miss.

The fake below answers like the post-drop database: any select or filter
that mentions `is_internal` raises. The rows themselves carry no such key.

The engagement-vs-internal hours split in the allocation rollup now derives
from the company kind (First Person's own company, `self-fpsf`, is internal
admin; client and Canonic work is engagement), and every workstream gets its
own per-project allocation row (Drew, 2026-09-30: internal hours go into
per-project rows once the flag is retired).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import cp_engine.mc2_db as mc2_db
from cp_engine.sync_mc2 import MC2Backend, workstream_rows_to_states


class _DroppedColumn(Exception):
    pass


class _Query:
    def __init__(self, client, table):
        self._c, self._t = client, table

    def _guard(self, text):
        if "is_internal" in str(text):
            raise _DroppedColumn(
                f"column projects.is_internal does not exist ({self._t}: {text})"
            )

    def select(self, cols):
        self._guard(cols)
        self._cols = cols
        return self

    def eq(self, k, v):
        self._guard(k)
        self._eq = (k, v)
        return self

    def neq(self, k, v):
        self._guard(k)
        return self

    def in_(self, k, v):
        self._guard(k)
        return self

    def order(self, *a, **kw):
        return self

    def limit(self, n):
        return self

    def execute(self):
        self._c.calls.append((self._t, self._cols))
        return type("R", (), {"data": list(self._c.tables.get(self._t, []))})()


class _Client:
    def __init__(self, tables):
        self.tables = tables
        self.calls: list[tuple[str, str]] = []

    def schema(self, _n):
        return self

    def table(self, name):
        return _Query(self, name)


_GGL = {"code": "GGL", "name": "Google", "kind": "client"}
_FP = {"code": "1PI", "name": "First Person", "kind": "self-fpsf"}
_CNC = {"code": "CNC", "name": "Canonic", "kind": "self-canonic"}


def _row(**kw):
    base = {
        "id": "p-5136", "number": 5136,
        "full_job_name": "GGL 5136 Go Safety Website", "name": "Go Safety Website",
        "mc_status": "Open", "account_manager": None, "deal_stage": "Won",
        "budget": None, "updated_at": "2026-09-01T00:00:00+00:00",
        "parent_id": None, "companies": _GGL, "repos": [],
    }
    base.update(kw)
    return base


def _backend(client):
    b = MC2Backend()
    b._client = client
    return b


def test_column_lists_never_name_is_internal():
    for name in dir(mc2_db):
        if name.endswith("_COLUMNS"):
            val = getattr(mc2_db, name)
            if isinstance(val, str):
                assert "is_internal" not in val, name


def test_project_state_has_no_is_internal():
    (s,) = workstream_rows_to_states([_row()])
    assert not hasattr(s, "is_internal")


def test_read_projects_works_after_the_column_is_dropped():
    c = _Client({"projects": [_row()]})
    (s,) = _backend(c).read_projects(None)
    assert s.code == "ggl-5136-go-safety-website"


def _alloc(person, hours, project):
    return {"hours": hours, "entities": {"name": person}, "projects": project}


def _proj(number, name, company):
    return {"number": number, "full_job_name": name, "companies": company}


def test_allocations_work_after_the_drop_and_split_on_company_kind():
    job = _proj(5136, "GGL 5136 Go Safety Website", _GGL)
    sales = _proj(9002, "1PI 9002 First Person Sales", _FP)
    storyos = _proj(9004, "CNC 9004 StoryOS", _CNC)
    c = _Client({"sprint_allocations": [
        _alloc("Tony Welch", 4, job),
        _alloc("Tony Welch", 3, sales),
        _alloc("Tony Welch", 5, storyos),
    ]})
    a = _backend(c).read_allocations(None, "2026-09-28")
    # Every workstream gets its own per-project row now — internal included.
    assert set(a.by_project) == {
        "ggl-5136-go-safety-website",
        "1pi-9002-first-person-sales",
        "cnc-9004-storyos",
    }
    assert not hasattr(a.by_project["1pi-9002-first-person-sales"], "is_internal")
    (r,) = a.rollup
    # First Person's own company is internal admin; Canonic is engagement
    # (StoryOS was flipped to is_internal=false on 2026-09-30 — unchanged).
    assert r.internal_hours == 3
    assert r.engagement_hours == 9
    assert r.engagement_project_count == 2


def test_slack_channel_map_works_after_the_drop():
    from cp_engine import slack

    c = _Client({"projects": [
        {"id": "p1", "number": 5136, "name": "Go Safety", "mc_status": "Open",
         "enable_slack": True, "full_job_name": "GGL 5136 Go Safety Website",
         "companies": {"code": "GGL"}},
    ], "project_integrations": []})
    import cp_engine.mc2_db as m
    orig = m.get_client
    m.get_client = lambda config: c
    try:
        rows = slack.list_channel_map(None)
    finally:
        m.get_client = orig
    assert [r.code for r in rows] == ["ggl-5136-go-safety-website"]


_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("tree", ["src", "prototypes/hosted-mcp", "webhook"])
def test_no_engine_code_queries_is_internal(tree):
    """Grep the shipped code (not tests, not archived scripts) for the column
    name inside a string literal. Person-roster `is_internal(...)` methods
    and `owner_is_internal` are a different concept and are excluded."""
    import re

    pat = re.compile(r"""["'][^"'\n]*\bis_internal\b[^"'\n]*["']""")
    hits = []
    for p in (_ROOT / tree).rglob("*.py"):
        if p.name.startswith("test_") or "/tests/" in str(p):
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if pat.search(line):
                hits.append(f"{p.relative_to(_ROOT)}:{i}: {line.strip()}")
    assert hits == []
