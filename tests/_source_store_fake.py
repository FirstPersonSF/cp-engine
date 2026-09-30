"""A small in-memory PostgREST for the #324 source-store tests.

It FILTERS (eq / in_ / is_ / range), so a query that asks the wrong question
gets the wrong answer here too, rather than whatever canned rows a permissive
fake hands back. Shared by test_source_visibility_324 and its behaviour-level
control file (which must import nothing that only exists after the fix).
"""
from __future__ import annotations

from types import SimpleNamespace


class _Query:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.filters: list = []
        self.cols = None
        self.payload = None
        self.lo, self.hi = 0, None
        self.lim = None

    def select(self, cols, **_kw):
        self.db.selects.append((self.name, cols))
        self.cols = cols
        return self

    def update(self, payload):
        self.payload = payload
        return self

    def eq(self, col, val):
        self.filters.append(lambda r, c=col, v=val: r.get(c) == v)
        return self

    def in_(self, col, vals):
        vals = set(vals)
        self.filters.append(lambda r, c=col: r.get(c) in vals)
        return self

    def is_(self, col, val):
        assert val == "null"
        self.filters.append(lambda r, c=col: r.get(c) is None)
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, lo, hi):
        self.lo, self.hi = lo, hi
        return self

    def limit(self, n):
        self.lim = n
        return self

    def execute(self):
        rows = [r for r in self.db.tables.get(self.name, [])
                if all(f(r) for f in self.filters)]
        if self.payload is not None:
            for r in rows:
                r.update(self.payload)
            self.db.updates.append((self.name, dict(self.payload), len(rows)))
            return SimpleNamespace(data=[dict(r) for r in rows])
        rows = rows[self.lo:(self.hi + 1 if self.hi is not None else None)]
        if self.lim is not None:
            rows = rows[: self.lim]
        return SimpleNamespace(data=[dict(r) for r in rows])


class _DB:
    """`tables` of rows + the scoped-chunk RPC (project arm ∪ account arm)."""

    def __init__(self, **tables):
        self.tables = {k: list(v) for k, v in tables.items()}
        self.selects: list = []
        self.updates: list = []

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        assert name == "read_scoped_asset_chunks"
        assets = {a["id"]: a for a in self.tables.get("rag_assets", [])}
        out = []
        for c in self.tables.get("asset_chunks", []):
            a = assets.get(c["asset_id"])
            if not a or a.get("status") != "active":
                continue
            if (a.get("scope") == "project" and a.get("project_id") == params["p_project_id"]) or (
                a.get("scope") == "account" and a.get("company_id") == params["p_company_id"]
            ):
                out.append({"text": c["text"], "citation_url": None, "title": a["title"],
                            "scope": a["scope"], "chunk_index": c.get("chunk_index"),
                            "page": None})
        out = out[: params["p_limit"]]
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=out))


def _asset(id_, title, *, project="p-job", company="co-1", scope="project",
           status="active", **kw):
    return {"id": id_, "title": title, "project_id": project, "company_id": company,
            "scope": scope, "status": status, "source_type": "doc",
            "created_at": kw.pop("created_at", "2026-09-01"), **kw}


def _chunk(asset_id, text, i=0):
    return {"id": f"{asset_id}-c{i}", "asset_id": asset_id, "text": text, "chunk_index": i}
