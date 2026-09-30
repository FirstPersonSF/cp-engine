"""A hosted write stores the project's CANONICAL code, never the caller's
short form (#309).

WHAT BROKE. `create_spine_element(project_code="slt-5196", ...)` stored
`project_code='slt-5196'` and `id='slt-5196/_authored/...'` beside 63 rows
spelled `slt-5196-brand-campaign-26` — a forked project, from a verb whose
docstring promised the opposite. The resolver meant to prevent it,
`resolve_write_scope`, ordered its lookup by `spine_substance.created_at`: a
column that table does not have. PostgREST rejected the query, a bare
`except: pass` swallowed the rejection, and every hosted writer fell back to
the caller's string. It had never canonicalised once since it shipped.

WHY THE FAKE REJECTS COLUMNS. A fake that accepts any column name is how the
original resolver passed review: it asserts the query was BUILT, not that the
database would RUN it. So the fake here knows `spine_substance`'s columns —
derived from the server's own column constants, each of which was verified
against the live schema — and raises, as PostgREST does, on anything else.

    python -m pytest prototypes/hosted-mcp/test_canonical_write_code.py -v
"""
from __future__ import annotations

import fnmatch
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

SHORT = "slt-5196"
CANONICAL = "slt-5196-brand-campaign-26"
PID = "44444444-4444-4444-4444-444444444444"
COMPANY_ID = "55555555-5555-5555-5555-555555555555"


@pytest.fixture
def server(monkeypatch):
    for k, v in {
        "SUPABASE_URL": "http://example.invalid",
        "SUPABASE_ANON_KEY": "x",
    }.items():
        monkeypatch.setenv(k, v)
    import server as mod

    return mod


def _columns(spec: str) -> set[str]:
    return {c.strip() for c in spec.split(",") if c.strip()}


class _ColumnError(Exception):
    """Stands in for PostgREST's 42703 `column ... does not exist`."""


class _Query:
    def __init__(self, client, table):
        self._client = client
        self._table = table
        self._rows = client.store.setdefault(table, [])
        self._filters = []
        self._op = "select"
        self._payload = None
        self._limit = None

    def _check(self, column):
        known = self._client.known.get(self._table)
        if known is not None and column not in known:
            raise _ColumnError(f"column {self._table}.{column} does not exist")

    def select(self, spec="*", *_a, **_k):
        for col in _columns(spec):
            self._check(col)
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def eq(self, column, value):
        self._check(column)
        self._filters.append(lambda r, c=column, v=value: str(r.get(c)) == str(v))
        return self

    def like(self, column, pattern):
        self._check(column)
        glob = pattern.replace("%", "*")
        self._filters.append(lambda r, c=column: fnmatch.fnmatchcase(str(r.get(c) or ""), glob))
        return self

    def ilike(self, column, pattern):
        self._check(column)
        glob = pattern.replace("%", "*").lower()
        self._filters.append(
            lambda r, c=column: fnmatch.fnmatchcase(str(r.get(c) or "").lower(), glob)
        )
        return self

    def order(self, column, *_a, **_k):
        self._check(column)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        if self._op == "insert":
            rows = self._payload if isinstance(self._payload, list) else [self._payload]
            for r in rows:
                self._rows.append(dict(r))
            return SimpleNamespace(data=[dict(r) for r in rows])
        hit = [dict(r) for r in self._rows if all(f(r) for f in self._filters)]
        return SimpleNamespace(data=hit[: self._limit] if self._limit else hit)


class FakeClient:
    def __init__(self, known):
        self.store: dict[str, list[dict]] = {}
        self.known = known

    def table(self, name):
        return _Query(self, name)

    def rpc(self, *_a, **_k):
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=None))


@pytest.fixture
def client(server, monkeypatch):
    # The server's own verified column lists — not a list written for this test.
    known = {
        "spine_substance": _columns(server.SPINE_PULL_COLUMNS)
        | _columns(server._ELEMENT_RESOLVE_COLUMNS),
    }
    c = FakeClient(known)
    c.store["projects"] = [{
        "id": PID, "code": "SLT-brand-campaign-26",
        "full_job_name": "SLT 5196 Brand Campaign 26",
        "company_id": COMPANY_ID, "number": 5196,
    }]
    c.store["companies"] = [{"id": COMPANY_ID, "code": "SLT"}]
    monkeypatch.setattr(server, "user_client", lambda: c)
    monkeypatch.setattr(server, "caller_subject", lambda: "user-sub-1")
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_paths_index_rows", lambda: {})
    return c


def _create(server):
    return server.create_spine_element(
        project_code=SHORT, framing="Participant: Erik Hagen", body="Interview notes.",
    )


def test_short_code_writes_the_code_the_existing_rows_carry(server, client):
    """The #309 repro: the project already has spine rows under the dir-slug.

    A write under the short code must land beside them, not fork a second
    project_code. The failure this guards is silent — the response echoed
    both spellings side by side and nothing flagged it.
    """
    client.store["spine_substance"] = [{
        "id": f"{CANONICAL}/_authored/brief/v1", "project_id": PID,
        "project_code": CANONICAL, "est_item_id": "_authored/brief",
        "version_date": "2026-09-01", "status": "live",
    }]
    out = _create(server)
    assert "error" not in out, out
    new = [r for r in client.store["spine_substance"] if r["est_item_id"] != "_authored/brief"]
    assert len(new) == 1
    assert new[0]["project_code"] == CANONICAL
    assert new[0]["id"].startswith(f"{CANONICAL}/_authored/")
    assert out["project_code"] == CANONICAL


def test_first_row_of_a_new_project_still_gets_the_canonical_code(server, client):
    """No spine rows yet: the caller's string must NOT define the spelling.

    The old fallback let the first write under `slt-5196` set the project's
    code for good. The directory name is knowable from MC-2
    (`full_job_name`), so the first row carries that.
    """
    out = _create(server)
    assert "error" not in out, out
    (row,) = client.store["spine_substance"]
    assert row["project_code"] == CANONICAL
    assert row["id"] == f"{CANONICAL}/_authored/participant-erik-hagen/v1"


def test_the_resolver_uses_only_columns_spine_substance_has(server, client):
    """The root cause, pinned directly: a lookup against a column the table
    lacks must not be the thing standing between a write and its canonical
    code. Resolving through the column-checking fake would have raised on
    `created_at` — this asserts the resolver completes and returns the slug.
    """
    client.store["spine_substance"] = [{
        "id": f"{CANONICAL}/_authored/brief/v1", "project_id": PID,
        "project_code": CANONICAL, "est_item_id": "_authored/brief",
        "version_date": "2026-09-01",
    }]
    scope = server.resolve_write_scope(client, SHORT)
    assert scope == {"id": PID, "kind": "project", "project_code": CANONICAL}
