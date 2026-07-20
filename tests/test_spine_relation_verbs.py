# tests/test_spine_relation_verbs.py — issues #96 + #97:
# create/retire_spine_relation MCP verbs + retire_spine_element edge cascade.
import cp_engine.mcp_server as srv


class _Table:
    """Fake supabase table modeling select/insert/delete/update + eq chaining.

    Records inserts and deletes on the shared `log` so tests assert what was
    written and which rows were targeted. `select().execute().data` returns
    whatever `existing` was seeded for this table name.
    """

    def __init__(self, name, log, existing):
        self.name = name
        self.log = log
        self.existing = existing
        self._eqs = []
        self._op = None
        self._payload = None

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def update(self, patch):
        self._op = "update"
        self._payload = patch
        return self

    def eq(self, col, val):
        self._eqs.append((col, val))
        return self

    def limit(self, *_a):
        return self

    def execute(self):
        if self._op == "insert":
            self.log.setdefault("inserts", []).append((self.name, self._payload))
            return type("R", (), {"data": [self._payload]})()
        if self._op == "delete":
            # Return the seeded matching rows so len(res.data) is the removed count.
            matched = self.existing.get((self.name, "delete"), [])
            self.log.setdefault("deletes", []).append((self.name, list(self._eqs)))
            return type("R", (), {"data": matched})()
        if self._op == "update":
            self.log.setdefault("updates", []).append((self.name, self._payload, list(self._eqs)))
            return type("R", (), {"data": []})()
        # select
        return type("R", (), {"data": self.existing.get((self.name, "select"), [])})()


class _Client:
    def __init__(self, log, existing):
        self.log = log
        self.existing = existing

    def table(self, name):
        return _Table(name, self.log, self.existing)


# ── create_spine_relation ───────────────────────────────────────────────────

def test_create_relation_happy(monkeypatch):
    log = {}
    client = _Client(log, {})
    monkeypatch.setattr(srv, "_resolve", lambda code: (client, "pid", "cid"))
    _patch_resolve_client(monkeypatch, client, {"a", "b"})
    out = srv.create_spine_relation("ibx-5192", "responds_to", "a", "b")
    assert out == {"kind": "responds_to", "from_item_id": "a",
                   "to_item_id": "b", "created": True}
    assert log["inserts"][0][0] == "spine_relations"
    assert log["inserts"][0][1]["kind"] == "responds_to"


def test_create_relation_rejects_unknown_kind(monkeypatch):
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))
    out = srv.create_spine_relation("ibx-5192", "supercedes", "a", "b")  # typo
    assert "unknown relation kind" in out["error"]


def test_create_relation_rejects_self_edge(monkeypatch):
    log = {}
    client = _Client(log, {})
    monkeypatch.setattr(srv, "_resolve", lambda code: (client, "pid", "cid"))
    _patch_resolve_client(monkeypatch, client, {"a"})
    out = srv.create_spine_relation("ibx-5192", "informs", "a", "a")
    assert out["error"] == "an element cannot relate to itself"


def test_create_relation_idempotent(monkeypatch):
    log = {}
    client = _Client(log, {("spine_relations", "select"): [{"id": "x"}]})
    monkeypatch.setattr(srv, "_resolve", lambda code: (client, "pid", "cid"))
    _patch_resolve_client(monkeypatch, client, {"a", "b"})
    out = srv.create_spine_relation("ibx-5192", "informs", "a", "b")
    assert out["created"] is False
    assert "inserts" not in log  # existing edge → no insert


def test_create_relation_unresolved_from(monkeypatch):
    log = {}
    client = _Client(log, {})
    monkeypatch.setattr(srv, "_resolve", lambda code: (client, "pid", "cid"))
    _patch_resolve_client(monkeypatch, client, {"b"})  # 'a' missing
    out = srv.create_spine_relation("ibx-5192", "informs", "a", "b")
    assert "from_key" in out["note"]


# ── retire_spine_relation ───────────────────────────────────────────────────

def test_retire_relation_happy(monkeypatch):
    log = {}
    client = _Client(log, {("spine_relations", "delete"): [{"id": "e1"}]})
    monkeypatch.setattr(srv, "_resolve", lambda code: (client, "pid", "cid"))
    _patch_resolve_client(monkeypatch, client, {"a", "b"})
    out = srv.retire_spine_relation("ibx-5192", "supersedes", "a", "b")
    assert out["removed"] == 1


def test_retire_relation_tolerates_dead_endpoint(monkeypatch):
    """An orphaned edge (endpoint already retired) is cleanable via raw ids."""
    log = {}
    client = _Client(log, {("spine_relations", "delete"): [{"id": "e1"}]})
    monkeypatch.setattr(srv, "_resolve", lambda code: (client, "pid", "cid"))
    _patch_resolve_client(monkeypatch, client, set())  # neither resolves
    out = srv.retire_spine_relation("ibx-5192", "informs", "_authored/dead", "b")
    assert out["from_item_id"] == "_authored/dead"  # raw key used verbatim
    assert out["removed"] == 1


def test_retire_relation_none_matched(monkeypatch):
    log = {}
    client = _Client(log, {})  # delete returns []
    monkeypatch.setattr(srv, "_resolve", lambda code: (client, "pid", "cid"))
    _patch_resolve_client(monkeypatch, client, {"a", "b"})
    out = srv.retire_spine_relation("ibx-5192", "informs", "a", "b")
    assert "no informs edge" in out["note"]


# ── retire_spine_element edge cascade (#96) ─────────────────────────────────

def test_retire_element_cascades_edges(monkeypatch):
    log = {}
    # delete on spine_relations returns 2 rows per direction (from + to sides).
    client = _Client(log, {("spine_relations", "delete"): [{"id": "e1"}, {"id": "e2"}]})
    monkeypatch.setattr(srv, "_resolve", lambda code: (client, "pid", "cid"))
    monkeypatch.setattr(
        "cp_engine.project_sources.resolve_live_element",
        lambda *_a, **_k: {"est_item_id": "_authored/stub", "project_id": "pid"},
    )
    out = srv.retire_spine_element("ibx-5192", "_authored/stub")
    assert out["retired"] is True
    # two deletes (from_item_id + to_item_id sides), each returning 2 rows
    assert out["edges_removed"] == 4
    rel_deletes = [d for d in log["deletes"] if d[0] == "spine_relations"]
    assert len(rel_deletes) == 2
    sides = {eqs[-1][0] for _, eqs in rel_deletes}
    assert sides == {"from_item_id", "to_item_id"}


def _patch_resolve_client(monkeypatch, _client, elements):
    """resolve_live_element that knows a fixed set of live element keys."""
    def fake_resolve(_c, _pid, key, _cid=None):
        return {"est_item_id": key} if key in elements else None

    monkeypatch.setattr(
        "cp_engine.project_sources.resolve_live_element", fake_resolve
    )
