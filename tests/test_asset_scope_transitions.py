"""Task C6 — scope-transition verbs on `rag_assets`.

promote / demote / archive_project_assets / unarchive_project_assets /
list_promotable are pure Supabase re-tags (UPDATE) + one review-gate SELECT.
These tests drive them against a fake Supabase client that records the payload,
the .eq() filters, and the selected columns, and returns canned `.data` (the
shape supabase-py's `update(...).execute()` / `select(...).execute()` return:
an object whose `.data` is the list of affected/selected rows).
"""

from __future__ import annotations

from cp_engine.asset_ingest import (
    archive_project_assets,
    demote_asset,
    list_promotable,
    promote_asset,
    unarchive_project_assets,
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


def test_promote_sets_account_scope_and_timestamp():
    client = _FakeClient(affected_rows=[{"id": "a-1"}])
    result = promote_asset(client, "a-1")

    assert result is True
    op = client.ops[0]
    assert op["op"] == "update"
    assert op["payload"]["scope"] == "account"
    assert op["payload"]["promoted_at"]  # a timestamp string was set
    assert op["filters"]["id"] == "a-1"
    # idempotency guard: only project-scoped rows are promoted
    assert op["filters"]["scope"] == "project"


def test_promote_already_account_is_noop():
    client = _FakeClient(affected_rows=[])  # WHERE scope='project' matched nothing
    result = promote_asset(client, "a-1")

    assert result is False  # no-op, no error
    assert client.ops[0]["filters"]["scope"] == "project"


# ──────────────────────────────────────────────────────────────────────
#  demote
# ──────────────────────────────────────────────────────────────────────


def test_demote_reverses_to_project():
    client = _FakeClient(affected_rows=[{"id": "a-1"}])
    result = demote_asset(client, "a-1")

    assert result is True
    op = client.ops[0]
    assert op["payload"]["scope"] == "project"
    assert op["payload"]["promoted_at"] is None
    assert op["filters"]["id"] == "a-1"
    # only account-scoped rows demote back
    assert op["filters"]["scope"] == "account"


def test_promote_demote_round_trip():
    promote_client = _FakeClient(affected_rows=[{"id": "a-1"}])
    assert promote_asset(promote_client, "a-1") is True
    assert promote_client.ops[0]["payload"]["scope"] == "account"

    demote_client = _FakeClient(affected_rows=[{"id": "a-1"}])
    assert demote_asset(demote_client, "a-1") is True
    assert demote_client.ops[0]["payload"]["scope"] == "project"
    assert demote_client.ops[0]["payload"]["promoted_at"] is None


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


def test_unarchive_restores_project():
    client = _FakeClient(affected_rows=[{"id": "a-1"}])
    count = unarchive_project_assets(client, "proj-1")

    assert count == 1
    op = client.ops[0]
    assert op["payload"]["scope"] == "project"
    assert op["payload"]["archived_at"] is None
    assert op["filters"]["project_id"] == "proj-1"
    assert op["filters"]["scope"] == "archived"


# ──────────────────────────────────────────────────────────────────────
#  list_promotable
# ──────────────────────────────────────────────────────────────────────


def test_list_promotable_filters_project_active():
    rows = [
        {
            "id": "a-1",
            "title": "Acme SOW",
            "url": "https://drive/acme-sow",
            "meta": {"classifier_decision": "include"},
        },
        {
            "id": "a-2",
            "title": "Acme Deck",
            "url": "https://drive/acme-deck",
            "meta": {"classifier_decision": "review"},
        },
    ]
    client = _FakeClient(select_rows=rows)
    result = list_promotable(client, "proj-1")

    op = client.ops[0]
    assert op["op"] == "select"
    # explicit columns, never '*'
    assert "*" not in op["columns"]
    for col in ("id", "title", "url", "meta"):
        assert col in op["columns"]
    # the review-gate filter
    assert op["filters"]["scope"] == "project"
    assert op["filters"]["status"] == "active"
    assert op["filters"]["project_id"] == "proj-1"

    # shaped for a human: id, title, classifier_decision surfaced
    assert result[0]["id"] == "a-1"
    assert result[0]["title"] == "Acme SOW"
    assert result[0]["classifier_decision"] == "include"
    assert result[1]["classifier_decision"] == "review"


def test_list_promotable_handles_missing_classifier_meta():
    rows = [{"id": "a-1", "title": "No meta", "url": "u", "meta": None}]
    client = _FakeClient(select_rows=rows)
    result = list_promotable(client, "proj-1")

    assert result[0]["classifier_decision"] is None
    assert result[0]["id"] == "a-1"
