"""`mc2_db.canonical_spine_code` must actually canonicalise (#309).

It shipped 2026-08-03 ordering by `spine_substance.created_at` — a column the
table does not have — inside a bare `except: pass`, so PostgREST's rejection
was swallowed and the function returned the caller's fallback on EVERY call.
Its two callers (inbox promote, `promote_uphill`'s promotions card) were
written precisely to stop short-code drift, and neither ever did.

The fake below raises on any `spine_substance` column not named in
`mc2_db`'s own column constants or in the engine's own insert row (each
verified against the live schema), the way PostgREST does — a fake that
accepts any name is how the original passed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cp_engine import mc2_db
from cp_engine.authored_element import build_create_rows

PID = "44444444-4444-4444-4444-444444444444"
CANONICAL = "slt-5196-brand-campaign-26"


def _columns(spec: str) -> set[str]:
    return {c.strip() for c in spec.split(",") if c.strip()}


# Two real sources: every `SPINE_*_COLUMNS` read shape, and the keys of the
# row the engine's own create path INSERTS (which production accepts).
_KNOWN_SPINE = set().union(
    *(
        _columns(getattr(mc2_db, name))
        for name in dir(mc2_db)
        if name.startswith("SPINE_") and name.endswith("_COLUMNS")
    ),
    *(
        row.keys()
        for row in build_create_rows(
            project_id=PID, project_code=CANONICAL, label="x", type_="note",
            body="x", serves=[], now_iso="2026-09-29T00:00:00+00:00",
        )
    ),
)


class _Query:
    def __init__(self, store, table):
        self._table = table
        self._rows = store.get(table, [])
        self._filters: list[tuple[str, str]] = []

    def _check(self, col):
        if self._table == mc2_db.Tables.SPINE_SUBSTANCE and col not in _KNOWN_SPINE:
            raise RuntimeError(f"42703 column spine_substance.{col} does not exist")

    def select(self, spec):
        for c in _columns(spec):
            self._check(c)
        return self

    def eq(self, col, val):
        self._check(col)
        self._filters.append((col, val))
        return self

    def order(self, col, **_k):
        self._check(col)
        return self

    def limit(self, _n):
        return self

    def execute(self):
        return SimpleNamespace(data=[
            r for r in self._rows if all(str(r.get(c)) == str(v) for c, v in self._filters)
        ])


class _Client:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _Query(self.store, name)


def test_the_column_set_is_derived_and_excludes_created_at():
    """Guard the guard: if the derived set ever grew `created_at` the tests
    below would stop being able to fail on the #309 defect."""
    assert "version_date" in _KNOWN_SPINE
    assert "created_at" not in _KNOWN_SPINE


def test_reads_the_spelling_existing_rows_carry():
    client = _Client({
        "spine_substance": [{"project_id": PID, "project_code": CANONICAL}],
        "projects": [{"id": PID, "full_job_name": "SLT 5196 Something Else"}],
    })
    assert mc2_db.canonical_spine_code(client, PID, "slt-5196") == CANONICAL


def test_project_with_no_spine_rows_gets_the_directory_slug():
    """The first write must not let the caller's short form define the code."""
    client = _Client({
        "spine_substance": [],
        "projects": [{"id": PID, "full_job_name": "SLT 5196 Brand Campaign 26"}],
    })
    assert mc2_db.canonical_spine_code(client, PID, "slt-5196") == CANONICAL


@pytest.mark.parametrize("store", [{}, {"projects": [{"id": PID, "full_job_name": None}]}])
def test_falls_back_to_the_caller_only_when_nothing_is_knowable(store):
    assert mc2_db.canonical_spine_code(_Client(store), PID, "slt-5196") == "slt-5196"
