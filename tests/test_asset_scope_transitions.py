"""Task C6 — scope-transition verbs on `rag_assets`.

`archive_project_assets` is a pure Supabase re-tag (UPDATE). Promote / demote /
unarchive / list_promotable were retired in architecture step 5a (no use).
These tests drive them against a fake Supabase client that records the payload,
the .eq() filters, and the selected columns, and returns canned `.data` (the
shape supabase-py's `update(...).execute()` / `select(...).execute()` return:
an object whose `.data` is the list of affected/selected rows).
"""

from __future__ import annotations

from cp_engine.asset_ingest import (
    archive_project_assets,
)


# ──────────────────────────────────────────────────────────────────────
#  Fake Supabase client — records update/select chains, returns canned data
# ──────────────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeUpdateChain:
    def __init__(self, recorder, affected_rows):
        self._recorder = recorder
        self._affected = affected_rows
        self._payload = None
        self._filters = {}

    def update(self, payload):
        self._payload = payload
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def execute(self):
        self._recorder.append(
            {"op": "update", "payload": self._payload, "filters": dict(self._filters)}
        )
        return _FakeResponse(list(self._affected))


class _FakeSelectChain:
    def __init__(self, recorder, rows):
        self._recorder = recorder
        self._rows = rows
        self._columns = None
        self._filters = {}

    def select(self, columns):
        self._columns = columns
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def execute(self):
        self._recorder.append(
            {"op": "select", "columns": self._columns, "filters": dict(self._filters)}
        )
        return _FakeResponse(list(self._rows))


class _FakeTable:
    def __init__(self, recorder, affected_rows, select_rows):
        self._recorder = recorder
        self._affected = affected_rows
        self._select_rows = select_rows

    def update(self, payload):
        return _FakeUpdateChain(self._recorder, self._affected).update(payload)

    def select(self, columns):
        return _FakeSelectChain(self._recorder, self._select_rows).select(columns)


class _FakeClient:
    """Fake Supabase client; only `.table('rag_assets')` is exercised.

    `affected_rows` is what an UPDATE's `.data` returns; `select_rows` is what a
    SELECT's `.data` returns. Operations are recorded in `self.ops`.
    """

    def __init__(self, *, affected_rows=None, select_rows=None):
        self.ops = []
        self._affected = affected_rows if affected_rows is not None else []
        self._select_rows = select_rows if select_rows is not None else []

    def table(self, name):
        assert name == "rag_assets", f"unexpected table {name!r}"
        return _FakeTable(self.ops, self._affected, self._select_rows)


# ──────────────────────────────────────────────────────────────────────
#  promote
# ──────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────
#  demote
# ──────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────
#  archive
# ──────────────────────────────────────────────────────────────────────


def test_archive_only_affects_project_scoped():
    client = _FakeClient(affected_rows=[{"id": "a-1"}, {"id": "a-2"}])
    count = archive_project_assets(client, "proj-1")

    assert count == 2
    op = client.ops[0]
    assert op["payload"]["scope"] == "archived"
    assert op["payload"]["archived_at"]  # timestamp set
    assert op["filters"]["project_id"] == "proj-1"
    # the guard: only un-promoted (project) assets archive
    assert op["filters"]["scope"] == "project"


def test_archive_leaves_account_assets():
    # The contract is enforced by the WHERE filter: scope='project' is present,
    # so account-scoped rows can never match the UPDATE.
    client = _FakeClient(affected_rows=[])
    archive_project_assets(client, "proj-1")

    op = client.ops[0]
    assert op["filters"]["scope"] == "project"
    assert "account" not in op["filters"].values()


# ──────────────────────────────────────────────────────────────────────
#  unarchive
# ──────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────
#  list_promotable
# ──────────────────────────────────────────────────────────────────────


