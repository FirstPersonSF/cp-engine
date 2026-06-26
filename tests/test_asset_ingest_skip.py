"""_unchanged_since_last_ingest — the pre-download skip check (ingest caching).

Task 3 of the ingest-caching arc. Given a freshly-listed FileRef, return True
ONLY when an ACTIVE rag_asset for the SAME (provider, file) already carries a
meta.change_token equal to the listed token (→ unchanged → skip download+embed).
Everything else (no row, mismatch, missing/None token, any error) → False, so we
ingest normally. Fail-open: a pre-check must NEVER abort or wrongly-skip a run.

The match MUST be scoped to the same provider: a Drive md5 token must never be
compared against a Dropbox content_hash (different algorithms), so the query keys
on BOTH source_provider AND source_file_id.
"""
from __future__ import annotations

from types import SimpleNamespace

from cp_engine.asset_ingest import FileRef, _unchanged_since_last_ingest


class _FakeTable:
    """Captures the chained-builder calls (select cols + every eq filter) and
    returns a configured row set from execute(). Mirrors supabase-py."""

    def __init__(self, rows):
        self._rows = rows
        self.select_cols = None
        self.eq_filters = {}
        self.limit_n = None

    def select(self, cols):
        self.select_cols = cols
        return self

    def eq(self, col, val):
        self.eq_filters[col] = val
        return self

    def limit(self, n):
        self.limit_n = n
        return self

    def execute(self):
        return SimpleNamespace(data=list(self._rows))


class _FakeClient:
    def __init__(self, rows):
        self.tbl = _FakeTable(rows)

    def table(self, name):
        assert name == "rag_assets"
        return self.tbl


class _RaisingTable:
    def select(self, cols):
        return self

    def eq(self, col, val):
        return self

    def limit(self, n):
        return self

    def execute(self):
        raise RuntimeError("boom")


class _RaisingClient:
    def table(self, name):
        return _RaisingTable()


class _ExplodingClient:
    """Raises the moment .table() is touched — proves no query was attempted."""

    def table(self, name):  # pragma: no cover - must never be called
        raise AssertionError("client must not be queried")


def _ref(*, source="drive", file_id="file-1", change_token="tok-new"):
    return FileRef(
        source=source,
        id=file_id,
        name="x.pptx",
        mime_type=None,
        size=None,
        modified=None,
        path=None,
        change_token=change_token,
    )


def test_match_returns_true():
    client = _FakeClient([{"meta": {"change_token": "tok-new"}}])
    assert _unchanged_since_last_ingest(client, "proj-1", _ref()) is True


def test_no_row_returns_false():
    client = _FakeClient([])
    assert _unchanged_since_last_ingest(client, "proj-1", _ref()) is False


def test_token_mismatch_returns_false():
    client = _FakeClient([{"meta": {"change_token": "tok-old"}}])
    assert _unchanged_since_last_ingest(client, "proj-1", _ref()) is False


def test_none_token_short_circuits_before_query():
    # None token: can't prove unchanged → False, AND no query may be issued.
    client = _ExplodingClient()
    assert (
        _unchanged_since_last_ingest(client, "proj-1", _ref(change_token=None))
        is False
    )


def test_stored_token_missing_returns_false():
    # active row exists but its meta carries no change_token.
    client = _FakeClient([{"meta": {"chunks": 5}}])
    assert _unchanged_since_last_ingest(client, "proj-1", _ref()) is False


def test_stored_meta_none_returns_false():
    client = _FakeClient([{"meta": None}])
    assert _unchanged_since_last_ingest(client, "proj-1", _ref()) is False


def test_client_raises_fails_open_false():
    assert _unchanged_since_last_ingest(_RaisingClient(), "proj-1", _ref()) is False


def test_query_is_provider_scoped():
    client = _FakeClient([{"meta": {"change_token": "tok-new"}}])
    ref = _ref(source="dropbox", file_id="id:abc")
    _unchanged_since_last_ingest(client, "proj-1", ref)
    f = client.tbl.eq_filters
    assert f["project_id"] == "proj-1"
    assert f["source_provider"] == "dropbox"  # == file_ref.source
    assert f["source_file_id"] == "id:abc"  # == file_ref.id
    assert f["status"] == "active"
    # explicit column, never SELECT *
    assert client.tbl.select_cols == "meta"
    # single-row contract: we only need to know one active row exists + its token.
    assert client.tbl.limit_n == 1
