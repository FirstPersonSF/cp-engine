"""In-memory stand-in for the slice of the supabase client the spine code uses.

Store-backed (``client.store[<table>]`` is a list of row dicts) so a test can
seed MC-2, run a writer, and read back exactly what landed. Chained ``.eq()``
filters AND together; ``select('*')`` is refused (house rule); writes can be
made to fail per table to exercise the loud paths.
"""

from __future__ import annotations


class _R:
    def __init__(self, data):
        self.data = data


class FakeTable:
    def __init__(self, client, name):
        self.client, self.name = client, name
        self._op = None
        self._filters: list[tuple[str, object]] = []
        self._limit = None

    # ── ops ──
    def select(self, cols, **_kw):
        assert "*" not in cols, "never select('*')"
        self._op = ("select", cols)
        return self

    def insert(self, rows):
        self._op = ("insert", rows if isinstance(rows, list) else [rows])
        return self

    def upsert(self, rows, on_conflict=None):
        self._op = ("upsert", rows if isinstance(rows, list) else [rows])
        return self

    def update(self, values):
        self._op = ("update", values)
        return self

    def delete(self):
        self._op = ("delete", None)
        return self

    # ── modifiers ──
    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def in_(self, col, vals):
        self._filters.append((col, ("__in__", tuple(vals))))
        return self

    # Comparison filters (added for the step-6 end-to-end test, which drives
    # sync, the webhook and the hosted server through this one store). Each
    # FILTERS like PostgREST, so a query that asks the wrong question gets
    # the wrong answer here too.
    def _cmp(self, op, col, val):
        self._filters.append((col, ("__op__", op, val)))
        return self

    def neq(self, col, val):
        return self._cmp("neq", col, val)

    def gt(self, col, val):
        return self._cmp("gt", col, val)

    def gte(self, col, val):
        return self._cmp("gte", col, val)

    def lt(self, col, val):
        return self._cmp("lt", col, val)

    def lte(self, col, val):
        return self._cmp("lte", col, val)

    def ilike(self, col, pattern):
        return self._cmp("ilike", col, pattern)

    def like(self, col, pattern):
        return self._cmp("like", col, pattern)

    def is_(self, col, val):
        return self._cmp("is", col, val)

    def or_(self, *_a, **_k):
        # PostgREST's `or=(...)` grammar is not modelled: the filter is a
        # no-op (rows are over-returned, never under-returned).
        return self

    @property
    def not_(self):
        return _Not(self)

    def single(self):
        self._single = True
        return self

    def maybe_single(self):
        self._single = True
        return self

    def range(self, lo, hi):
        self._range = (lo, hi)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def order(self, *a, **k):
        return self

    def _matches(self, row):
        for c, v in self._filters:
            if isinstance(v, tuple) and v[:1] == ("__in__",):
                if row.get(c) not in v[1]:
                    return False
            elif isinstance(v, tuple) and v[:1] == ("__op__",):
                if not _compare(v[1], row.get(c), v[2]):
                    return False
            elif isinstance(v, tuple) and v[:1] == ("__not__",):
                if _compare(v[1], row.get(c), v[2]):
                    return False
            elif row.get(c) != v:
                return False
        return True

    def execute(self):
        op, payload = self._op
        if self.name in self.client.fail.get(op, set()) or self.name in self.client.fail.get("*", set()):
            raise RuntimeError(f"fake {op} failure on {self.name}")
        rows = self.client.store.setdefault(self.name, [])
        self.client.calls.append((op, self.name, list(self._filters)))
        if op == "select":
            hits = [dict(r) for r in rows if self._matches(r)]
            lo, hi = getattr(self, "_range", (0, None))
            hits = hits[lo: None if hi is None else hi + 1]
            hits = hits[: self._limit] if self._limit is not None else hits
            if getattr(self, "_single", False):
                return _R(hits[0] if hits else None)
            return _R(hits)
        if op == "insert":
            default = self.client.defaults.get(self.name)
            out = []
            for r in payload:
                if default is not None:
                    r = {**default(), **r}
                if "id" in r and any(x.get("id") == r["id"] for x in rows):
                    raise RuntimeError(f"duplicate key {r['id']}")
                rows.append(dict(r))
                out.append(dict(r))
            return _R(out)
        if op == "upsert":
            for r in payload:
                hit = next((x for x in rows if x.get("id") == r.get("id")), None)
                if hit is None:
                    rows.append(dict(r))
                else:
                    hit.update(r)
            return _R(payload)
        if op == "update":
            hit = []
            for r in rows:
                if self._matches(r):
                    r.update(payload)
                    hit.append(dict(r))
            return _R(hit)
        if op == "delete":
            rows[:] = [r for r in rows if not self._matches(r)]
            return _R([])
        raise AssertionError(op)


def _compare(op, have, want):
    if op == "is":
        want = None if want in (None, "null") else want
        return have is want or have == want
    if op == "neq":
        return have != want
    if op == "notin":
        return have not in want
    if op in ("ilike", "like"):
        import fnmatch

        pat = str(want).replace("%", "*")
        h = "" if have is None else str(have)
        return (fnmatch.fnmatchcase(h.lower(), pat.lower()) if op == "ilike"
                else fnmatch.fnmatchcase(h, pat))
    if have is None:
        return False
    return {"gt": have > want, "gte": have >= want,
            "lt": have < want, "lte": have <= want}[op]


class _Not:
    """``.not_.is_(col, "null")`` / ``.not_.in_(...)`` — negates one filter."""

    def __init__(self, table):
        self._t = table

    def _neg(self, op, col, val):
        self._t._filters.append((col, ("__not__", op, val)))
        return self._t

    def is_(self, col, val):
        return self._neg("is", col, val)

    def eq(self, col, val):
        return self._neg("is", col, val)

    def ilike(self, col, val):
        return self._neg("ilike", col, val)

    def in_(self, col, vals):
        self._t._filters.append((col, ("__op__", "notin", tuple(vals))))
        return self._t


class _Schema:
    def __init__(self, client, schema):
        self.client, self.schema_name = client, schema

    def table(self, name):
        key = name if self.schema_name == "public" else f"{self.schema_name}.{name}"
        return FakeTable(self.client, key)

    def rpc(self, name, params=None):
        return self.client.rpc(name, params)


class FakeClient:
    def __init__(self, **tables):
        self.store: dict[str, list[dict]] = {k: [dict(r) for r in v] for k, v in tables.items()}
        self.fail: dict[str, set[str]] = {}
        self.calls: list[tuple] = []
        #: ``rpc(name, params)`` → ``rpcs[name](params)``; unknown → no rows.
        self.rpcs: dict = {}
        #: Column defaults Postgres would fill on INSERT (``id``,
        #: ``created_at``), per table: ``defaults[table]() -> dict``. Opt-in,
        #: so a test that asserts the exact inserted row is unaffected.
        self.defaults: dict = {}

    def table(self, name):
        return FakeTable(self, name)

    def schema(self, name):
        return _Schema(self, name)

    def rpc(self, name, params=None):
        self.calls.append(("rpc", name, params))
        fn = self.rpcs.get(name)
        data = fn(params or {}) if fn is not None else []
        return type("RpcQ", (), {"execute": lambda _s: _R(data)})()

    def writes(self, table: str | None = None):
        return [c for c in self.calls
                if c[0] in ("insert", "upsert", "update", "delete")
                and (table is None or c[1] == table)]


def substance_row(code, eid, label="v1", *, project_id="pid", status="live",
                  origin="authored", body="body text", framing="Framing",
                  layer="Brief", rel_path=None, kind=None, phase=None,
                  binding="unbound", placement="context", scope="project",
                  archived=False, serves=(), sources=(), date="2026-09-01",
                  company_id=None):
    return {
        "id": f"{code}/{eid}/{label}", "project_id": project_id,
        "project_code": code, "company_id": company_id, "est_item_id": eid,
        "est_item_kind": kind, "phase": phase, "binding": binding,
        "layer": layer, "placement": placement, "serves": list(serves),
        "version_label": label, "version_date": date, "status": status,
        "framing": framing, "body": body, "sources": list(sources),
        "origin": origin, "scope": scope, "archived": archived,
        "rel_path": rel_path, "field_states": {}, "review_flags": [],
    }
