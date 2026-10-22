"""The stdio `pull_source` never passes a row-capped read off as whole (#349 item 6).

The no-query title pull reads the `read_scoped_asset_chunks` RPC window and,
when it saturates, widens to `_MISS_RETRY_LIMIT` (2000). PostgREST caps every
response at `max-rows` (1000 on MC-2) — RPC results included — so the widened
read came back with at most 1,000 rows and the completeness test
`len(rows) >= 2000` could never fire: a 1,500-chunk document was returned as
its first ~1,000 chunks with no `truncated` flag. The existing test of that
branch hands back 2,000 rows from one call, which the server never does.

The fake here enforces the cap on every response. With the asset table
readable, the pull now reads the resolved document directly, paged by
`.range()`; without it, the pull says the read may be incomplete.
"""
from __future__ import annotations

from types import SimpleNamespace

from cp_engine.project_sources import pull_source

_MAX_ROWS = 1000
_TITLE = "Brand Campaign Storyline Map_v02"


def _rpc_rows(title, n, *, offset=0):
    return [{"text": f"{title} #{i}", "title": title, "scope": "project",
             "citation_url": "u", "chunk_index": i, "page": None}
            for i in range(offset, offset + n)]


class _Q:
    def __init__(self, rows):
        self._rows, self._f, self._order, self._range = rows, [], None, None

    def select(self, cols, *a, **k):
        assert "*" not in cols
        return self

    def eq(self, col, val):
        self._f.append(lambda r, c=col, v=val: r.get(c) == v)
        return self

    def order(self, col, *a, **k):
        self._order = col
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        rows = [dict(r) for r in self._rows if all(f(r) for f in self._f)]
        if self._order:
            rows.sort(key=lambda r: r.get(self._order))
        start, end = self._range or (0, len(rows) - 1)
        return SimpleNamespace(data=rows[start:end + 1][:_MAX_ROWS])


class _CappedRpc:
    """The RPC honours `p_limit` and then the server's max-rows cap."""

    def __init__(self, pool, *, assets=None, chunks=None):
        self.pool, self.assets, self.chunks = pool, assets, chunks
        self.rpc_limits: list[int] = []

    def rpc(self, name, params):
        assert name == "read_scoped_asset_chunks"
        self.rpc_limits.append(params["p_limit"])
        rows = self.pool[: params["p_limit"]][:_MAX_ROWS]
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=list(rows)))

    def table(self, name):
        if self.assets is None:
            raise RuntimeError("table reads unavailable in this fake")
        return _Q(self.assets if name == "rag_assets" else self.chunks)


def _stored_doc(n):
    assets = [{"id": "a1", "title": _TITLE, "project_id": "proj-1",
               "status": "active", "scope": "project", "created_at": "2026-10-06",
               "prev_asset_id": None, "coverage": None}]
    # Stored in reverse id order so an unsorted read is caught.
    chunks = [{"id": f"c{i:06d}", "asset_id": "a1", "text": f"{_TITLE} #{i}",
               "chunk_index": i, "meta_chunk_index": str(i)}
              for i in reversed(range(n))]
    return assets, chunks


def test_capped_widened_window_without_a_direct_read_is_flagged_truncated():
    pool = _rpc_rows(_TITLE, 1500)
    client = _CappedRpc(pool)

    out = pull_source(client, "proj-1", "co-9", doc_title=_TITLE)

    assert client.rpc_limits[-1] > _MAX_ROWS  # it asked for the widened window
    assert out["chunk_count"] == _MAX_ROWS     # and got the capped one
    assert out.get("truncated") is True
    assert "may be incomplete" in out["note"]


def test_document_over_the_cap_is_read_whole_through_the_asset_table():
    assets, chunks = _stored_doc(1500)
    client = _CappedRpc(_rpc_rows(_TITLE, 1500), assets=assets, chunks=chunks)

    out = pull_source(client, "proj-1", "co-9", doc_title=_TITLE)

    assert out["chunk_count"] == 1500
    assert out["chunks"] == [f"{_TITLE} #{i}" for i in range(1500)]
    assert out["order"] == "chunk_index"
    assert "truncated" not in out


def test_document_entirely_below_a_capped_window_is_found_not_missed():
    # 1,000 newer chunks of other documents fill the capped window; the
    # target never appears in it. That is a false "no source named …".
    assets, chunks = _stored_doc(30)
    assets.append({"id": "n1", "title": "Newer Doc", "project_id": "proj-1",
                   "status": "active", "scope": "project",
                   "created_at": "2026-10-20", "prev_asset_id": None,
                   "coverage": None})
    pool = _rpc_rows("Newer Doc", 1200) + _rpc_rows(_TITLE, 30)
    client = _CappedRpc(pool, assets=assets, chunks=chunks)

    out = pull_source(client, "proj-1", "co-9", doc_title=_TITLE)

    assert out["title"] == _TITLE
    assert out["chunk_count"] == 30
    assert out["chunks"][0] == f"{_TITLE} #0"
