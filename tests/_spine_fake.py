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
    def select(self, cols):
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

    def limit(self, n):
        self._limit = n
        return self

    def order(self, *a, **k):
        return self

    def _matches(self, row):
        return all(row.get(c) == v for c, v in self._filters)

    def execute(self):
        op, payload = self._op
        if self.name in self.client.fail.get(op, set()) or self.name in self.client.fail.get("*", set()):
            raise RuntimeError(f"fake {op} failure on {self.name}")
        rows = self.client.store.setdefault(self.name, [])
        self.client.calls.append((op, self.name, list(self._filters)))
        if op == "select":
            hits = [dict(r) for r in rows if self._matches(r)]
            return _R(hits[: self._limit] if self._limit is not None else hits)
        if op == "insert":
            for r in payload:
                if "id" in r and any(x.get("id") == r["id"] for x in rows):
                    raise RuntimeError(f"duplicate key {r['id']}")
                rows.append(dict(r))
            return _R(payload)
        if op == "upsert":
            for r in payload:
                hit = next((x for x in rows if x.get("id") == r.get("id")), None)
                if hit is None:
                    rows.append(dict(r))
                else:
                    hit.update(r)
            return _R(payload)
        if op == "update":
            for r in rows:
                if self._matches(r):
                    r.update(payload)
            return _R([])
        if op == "delete":
            rows[:] = [r for r in rows if not self._matches(r)]
            return _R([])
        raise AssertionError(op)


class FakeClient:
    def __init__(self, **tables):
        self.store: dict[str, list[dict]] = {k: [dict(r) for r in v] for k, v in tables.items()}
        self.fail: dict[str, set[str]] = {}
        self.calls: list[tuple] = []

    def table(self, name):
        return FakeTable(self, name)

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
