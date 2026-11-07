"""`create_vendor` and `add_rfp_respondent` (cp-engine #412).

Until these existed the registry and the RFP pipeline could be read but not
written from cp-hosted, so adding Orbital Studios for sap-5198 on 11-04 took
raw SQL: no attribution, and no `email_confidence` check. What these tests pin:

- both verbs exist on `/mcp` and NOT on `/mcp/read` (they write);
- the write goes through the CALLER's client, as an explicit-column INSERT with
  a minted id and no read-back of the row;
- an unvouched address is refused before any write;
- a duplicate vendor (same slug) and a duplicate respondent (same registry
  vendor, or same name, on the project) are refused, and nothing is inserted;
- `add_rfp_respondent` echoes `level`; both are team-gated.

    python -m pytest prototypes/hosted-mcp/test_vendor_writes_412.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "http://example.invalid")
    mp.setenv("SUPABASE_ANON_KEY", "x")
    mp.delenv("TENANT_REPO", raising=False)
    import server as mod

    yield mod
    mp.undo()


@pytest.fixture
def pt(server):
    return server.ported_tools


class _Store:
    """A tiny PostgREST stand-in: filtered SELECTs, recorded INSERTs."""

    def __init__(self, tables, fail_insert: Exception | None = None):
        self.tables = {k: [dict(r) for r in v] for k, v in tables.items()}
        self.inserts: list[tuple[str, dict, dict]] = []
        self.selects: list[tuple[str, str]] = []
        self.fail_insert = fail_insert

    def table(self, name):
        store = self
        rows = list(store.tables.get(name, []))

        class _Q:
            def select(self, cols):
                assert "*" not in cols, f"select('*') on {name}"
                store.selects.append((name, cols))
                return self

            def eq(self, c, v):
                nonlocal rows
                rows = [r for r in rows if r.get(c) == v]
                return self

            def limit(self, n):
                nonlocal rows
                rows = rows[:n]
                return self

            def insert(self, row, **kw):
                if store.fail_insert:
                    raise store.fail_insert
                store.inserts.append((name, dict(row), kw))
                store.tables.setdefault(name, []).append(dict(row))
                return self

            def execute(self):
                return SimpleNamespace(data=[dict(r) for r in rows])

        return _Q()


class _Audit(list):
    def __call__(self, client, tool, args, rows):
        self.append((tool, dict(args), rows))


ORBITAL = {"id": "v-orb", "name": "Orbital Studios", "slug": "orbital-studios",
           "status": "active"}
GONE = {"id": "v-gone", "name": "Gone Co", "slug": "gone-co", "status": "archived"}


@pytest.fixture
def env(server, monkeypatch):
    store = _Store({
        "vendors": [ORBITAL, GONE],
        "rfp_respondents": [{"id": "r1", "project_id": "p1", "vendor_id": "v-orb",
                             "vendor_name": "Orbital Studios", "status": "not_sent"}],
    })
    audit = _Audit()
    monkeypatch.setattr(server, "user_client", lambda: store)
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(server, "caller_email", lambda: "drew@example.com")
    monkeypatch.setattr(server, "resolve_project_id",
                        lambda c, code: None if code == "ghost" else "p1")
    monkeypatch.setattr(server, "resolve_company_id", lambda c, pid: "co1")
    monkeypatch.setattr(server, "_level_for", lambda code: {
        "code": "sap-5198-2027-ad-videos", "label": "job",
        "parent": "sap-5160-sap", "indexed": True})
    monkeypatch.setattr(server, "audit", audit)
    return SimpleNamespace(store=store, audit=audit)


# ── registration ──────────────────────────────────────────────────────


def test_both_verbs_are_registered_as_writers_on_main_only(server):
    """Fails on unfixed code: neither verb existed before #412."""
    main = {t.name for t in server.mcp_server._tool_manager.list_tools()}
    read = {t.name for t in server.read_server._tool_manager.list_tools()}
    for verb in ("create_vendor", "add_rfp_respondent"):
        assert verb in main
        assert verb in server.MAIN_ONLY_TOOLS
        assert verb not in read


# ── create_vendor ─────────────────────────────────────────────────────


def test_create_vendor_inserts_under_the_callers_client(pt, env):
    from postgrest.types import ReturnMethod

    out = pt.create_vendor(
        "Secret Level", city="Los Angeles, CA",
        capability_tags=["animation", "real-time"], watch_outs="busy in Q1")
    assert "error" not in out, out
    [(table, row, kw)] = env.store.inserts
    assert table == "vendors"
    assert kw == {"returning": ReturnMethod.minimal}
    assert row["id"] == out["vendor_id"] and row["slug"] == "secret-level"
    assert row["email_confidence"] == "unresearched" and row["contact_email"] is None
    assert row["capability_tags"] == ["animation", "real-time"]
    assert out["level"] is None and "add_rfp_respondent" in out["next"]
    assert env.audit[-1][0] == "create_vendor" and env.audit[-1][2] == 1


def test_create_vendor_refuses_an_unvouched_address_before_writing(pt, env):
    out = pt.create_vendor("Trilogy", contact_email="first.last@trilogy.example")
    assert "Refusing to store" in out["error"]
    assert env.store.inserts == []
    ok = pt.create_vendor("Trilogy", contact_email="hello@trilogy.example",
                          email_confidence="likely")
    assert ok["contact_email"] == "hello@trilogy.example"
    assert ok["email_confidence"] == "likely"


def test_create_vendor_refuses_a_duplicate_slug(pt, env):
    out = pt.create_vendor("orbital  studios")
    assert "already in the registry" in out["error"]
    assert out["duplicate_of"]["id"] == "v-orb"
    assert env.store.inserts == []
    archived = pt.create_vendor("Gone Co.")
    assert "archived" in archived["error"]


def test_create_vendor_names_a_concurrent_duplicate(pt, env):
    env.store.fail_insert = RuntimeError(
        "{'code': '23505', 'message': 'duplicate key value violates unique "
        "constraint \"vendors_slug_key\"'}")
    out = pt.create_vendor("Kristen & David")
    assert "concurrently" in out["error"]


def test_create_vendor_is_team_gated(server, pt, env, monkeypatch):
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (False, "tree access denied"))
    out = pt.create_vendor("Secret Level")
    assert "denied" in out["error"] and env.store.inserts == []


# ── add_rfp_respondent ────────────────────────────────────────────────


def test_add_rfp_respondent_invites_a_registry_vendor_and_echoes_level(pt, env):
    pt.create_vendor("Kristen & David")
    env.store.inserts.clear()
    out = pt.add_rfp_respondent("sap-5198", "Kristen & David")
    assert "error" not in out, out
    [(table, row, _kw)] = env.store.inserts
    assert table == "rfp_respondents"
    assert row["project_id"] == "p1" and row["vendor_name"] == "Kristen & David"
    assert row["status"] == "not_sent" and row["vendor_id"] == out["vendor_id"]
    assert out["level"]["code"] == "sap-5198-2027-ad-videos"


def test_add_rfp_respondent_accepts_the_slug(pt, env):
    pt.create_vendor("Secret Level")
    out = pt.add_rfp_respondent("sap-5198", "secret-level", status="shortlisted")
    assert out["vendor_name"] == "Secret Level" and out["status"] == "shortlisted"


def test_add_rfp_respondent_refuses_a_vendor_already_on_the_project(pt, env):
    out = pt.add_rfp_respondent("sap-5198", "Orbital Studios")
    assert "already a respondent" in out["error"]
    assert out["duplicate_of"]["id"] == "r1"
    assert env.store.inserts == []


def test_add_rfp_respondent_refuses_a_name_match_without_vendor_id(pt, env):
    env.store.tables["rfp_respondents"].append(
        {"id": "r2", "project_id": "p1", "vendor_id": None,
         "vendor_name": "secret level", "status": "sent"})
    pt.create_vendor("Secret Level")
    env.store.inserts.clear()
    out = pt.add_rfp_respondent("sap-5198", "Secret Level")
    assert out["duplicate_of"]["id"] == "r2" and env.store.inserts == []


def test_add_rfp_respondent_refuses_unregistered_and_archived(pt, env):
    assert "create_vendor first" in pt.add_rfp_respondent("sap-5198", "Nobody Inc")["error"]
    assert "archived" in pt.add_rfp_respondent("sap-5198", "Gone Co")["error"]
    assert env.store.inserts == []


def test_add_rfp_respondent_validates_status_and_project(pt, env):
    pt.create_vendor("Trilogy")
    env.store.inserts.clear()
    assert "status" in pt.add_rfp_respondent("sap-5198", "Trilogy", status="invited")["error"]
    assert "decline_reason" in pt.add_rfp_respondent(
        "sap-5198", "Trilogy", status="sent", decline_reason="busy")["error"]
    assert "no project" in pt.add_rfp_respondent("ghost", "Trilogy")["error"]
    assert env.store.inserts == []
