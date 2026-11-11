"""`log_lesson`, `list_lessons`, `set_lesson` (learning from projects, phase 2).

Drew, 2026-11-11: "remember this" in any session, and the project owner
reviews lessons. What these tests pin:

- the writers are on `/mcp` only and the reader is on `/mcp/read` too;
- `log_lesson` inserts through the CALLER's client, defaults to confirmed,
  takes the client from the project, the types from the project for a type
  lesson, and embeds best-effort (a failed embed keeps the lesson);
- refusals are plain and write nothing;
- `list_lessons` for a project reads its client's lessons, its types' and
  practice, and never another client's;
- `set_lesson` restarts the review year on confirm and re-embeds on a text edit.

    python -m pytest prototypes/hosted-mcp/test_lessons_verbs.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

TYPES = [{"slug": s, "name": s, "sort": i, "active": True}
         for i, s in enumerate(["campaign", "video", "design"])]
MOMENTS = [{"slug": s, "name": s, "description": None, "sort": i, "active": True}
           for i, s in enumerate(["travel", "invoicing", "client-review-round"])]


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
    """PostgREST stand-in: filtered SELECTs, recorded INSERT/UPDATE."""

    def __init__(self, tables):
        self.tables = {k: [dict(r) for r in v] for k, v in tables.items()}
        self.writes: list[tuple[str, str, dict]] = []
        self.n = 0

    def table(self, name):
        store = self
        state = {"rows": list(store.tables.get(name, [])), "write": None}

        class _Q:
            def select(self, cols):
                assert "*" not in cols, f"select('*') on {name}"
                return self

            def eq(self, c, v):
                state["rows"] = [r for r in state["rows"] if r.get(c) == v]
                return self

            def in_(self, c, vals):
                state["rows"] = [r for r in state["rows"] if r.get(c) in vals]
                return self

            def overlaps(self, c, vals):
                state["rows"] = [r for r in state["rows"] if set(r.get(c) or []) & set(vals)]
                return self

            def lte(self, c, v):
                state["rows"] = [r for r in state["rows"] if (r.get(c) or "9") <= v]
                return self

            def order(self, *a, **k):
                return self

            def limit(self, n):
                state["rows"] = state["rows"][:n]
                return self

            def insert(self, row, **kw):
                store.n += 1
                saved = {"id": f"L{store.n}", "owner_name": "Drew Fiero", **row}
                store.writes.append((name, "insert", dict(row)))
                store.tables.setdefault(name, []).append(saved)
                state["rows"], state["write"] = [saved], "insert"
                return self

            def update(self, patch):
                state["write"] = ("update", dict(patch))
                return self

            def execute(self):
                w = state["write"]
                if isinstance(w, tuple):
                    store.writes.append((name, "update", w[1]))
                    for r in state["rows"]:
                        r.update(w[1])
                return SimpleNamespace(data=[dict(r) for r in state["rows"]])

        return _Q()

    def rpc(self, fn, params):
        self.writes.append(("rpc", fn, params))
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))


class _Audit(list):
    def __call__(self, client, tool, args, rows):
        self.append((tool, dict(args), rows))


LESSONS = [
    {"id": "a", "text": "Infoblox decides late", "scope": "client", "company_id": "ibx",
     "status": "confirmed", "project_types": [], "moments": [], "source_project_id": "p-ibx"},
    {"id": "b", "text": "SAP legal adds a round", "scope": "client", "company_id": "sap",
     "status": "confirmed", "project_types": [], "moments": [], "source_project_id": None},
    {"id": "c", "text": "Campaigns need a review calendar", "scope": "type", "company_id": None,
     "status": "confirmed", "project_types": ["campaign"], "moments": [], "source_project_id": None},
    {"id": "d", "text": "Video lessons", "scope": "type", "company_id": None,
     "status": "confirmed", "project_types": ["video"], "moments": [], "source_project_id": None},
    {"id": "e", "text": "Expenses in before the final invoice", "scope": "practice",
     "company_id": None, "status": "confirmed", "project_types": [], "moments": ["invoicing"],
     "source_project_id": None},
]


@pytest.fixture
def env(server, monkeypatch):
    store = _Store({
        "project_types": TYPES, "lesson_moments": MOMENTS, "lessons": LESSONS,
        "companies": [{"id": "ibx", "code": "IBX", "name": "Infoblox"}],
        "projects": [{"id": "p-ibx", "code": "IBX-ai-campaign", "project_types": ["campaign"]}],
    })
    audit = _Audit()
    embeds: list[str] = []
    monkeypatch.setattr(server, "user_client", lambda: store)
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(server, "caller_email", lambda: "drew@example.com")
    monkeypatch.setattr(server, "resolve_project_id",
                        lambda c, code: None if code == "ghost" else "p-ibx")
    monkeypatch.setattr(server, "resolve_company_id", lambda c, pid: "ibx")
    monkeypatch.setattr(server, "_level_for", lambda code: {"code": code, "label": "job"})
    monkeypatch.setattr(server, "audit", audit)
    monkeypatch.setattr(server, "embedding_available", lambda: (True, ""))
    monkeypatch.setattr(server, "embed_document", lambda t: embeds.append(t) or [0.1])
    monkeypatch.setattr(server, "embed_query", lambda t: [0.2])
    return SimpleNamespace(store=store, audit=audit, embeds=embeds)


def _inserts(env):
    return [w[2] for w in env.store.writes if w[:2] == ("lessons", "insert")]


def test_registration(server):
    main = {t.name for t in server.mcp_server._tool_manager.list_tools()}
    read = {t.name for t in server.read_server._tool_manager.list_tools()}
    for verb in ("log_lesson", "set_lesson"):
        assert verb in main and verb in server.MAIN_ONLY_TOOLS and verb not in read
    assert "list_lessons" in read and "list_lessons" in server.READ_ONLY_TOOLS


def test_remember_this_logs_a_confirmed_client_lesson_from_the_project(pt, env):
    out = pt.log_lesson("Infoblox decisions land in the final two weeks; book review time.",
                        "client", project_code="ibx-5153", moments=["client-review-round"])
    row = _inserts(env)[0]
    assert row["status"] == "confirmed" and row["confirmed_at"]
    assert row["company_id"] == "ibx" and row["source_project_id"] == "p-ibx"
    assert out["embedded"] is True and env.embeds == [row["text"]]
    assert "internal" in out["internal_only_note"]
    assert env.audit[-1][0] == "log_lesson" and env.audit[-1][2] == 1


def test_a_type_lesson_takes_the_projects_types(pt, env):
    pt.log_lesson("Lock the review calendar at kickoff on campaigns.", "type",
                  project_code="ibx-5153")
    assert _inserts(env)[0]["project_types"] == ["campaign"]


def test_practice_lesson_needs_no_project(pt, env):
    out = pt.log_lesson("Get travelling directors' expenses in before the final invoice.",
                        "practice", moments=["travel", "invoicing"])
    assert "error" not in out and _inserts(env)[0]["source_project_id"] is None


@pytest.mark.parametrize("args,needle", [
    (("Too short", "practice"), "10–1,200"),
    (("A lesson with a bad moment here.", "practice"), "other_moment"),
    (("A client lesson with no client.", "client"), "needs the client"),
    (("A lesson with a made-up scope.", "everyone"), "scope is one of"),
])
def test_refusals_write_nothing(pt, env, args, needle):
    kw = {"moments": ["made-up"]} if "bad moment" in args[0] else {}
    out = pt.log_lesson(*args, **kw)
    assert needle in out["error"]
    assert not _inserts(env)


def test_unknown_company_is_refused(pt, env):
    out = pt.log_lesson("A client lesson for nobody.", "client", company="NOPE")
    assert "no company" in out["error"] and not _inserts(env)


def test_a_failed_embed_keeps_the_lesson(pt, env, server, monkeypatch):
    def boom(_t):
        raise RuntimeError("voyage down")

    monkeypatch.setattr(server, "embed_document", boom)
    out = pt.log_lesson("Book the shoot crew two weeks out.", "practice")
    assert out["id"] and out["embedded"] is False and "voyage down" in out["embed_reason"]


def test_lookouts_for_a_project_never_include_another_client(pt, env):
    out = pt.list_lessons(project_code="ibx-5153")
    ids = {r["id"] for r in out["lessons"]}
    assert ids == {"a", "c", "e"}  # its client, its type, practice; not SAP's, not video
    assert out["mode"] == "lookouts"
    assert [r["source_project"] for r in out["lessons"] if r["id"] == "a"] == ["IBX-ai-campaign"]


def test_search_scopes_the_rpc_to_the_projects_client(pt, env):
    pt.list_lessons(project_code="ibx-5153", query="when do decisions happen")
    rpc = [w for w in env.store.writes if w[0] == "rpc"][0]
    assert rpc[1] == "match_lessons" and rpc[2]["p_company_id"] == "ibx"
    assert rpc[2]["p_project_types"] == ["campaign"]


def test_bare_list_shows_the_lists(pt, env):
    out = pt.list_lessons()
    assert out["moments"] == ["travel", "invoicing", "client-review-round"]
    assert "campaign" in out["project_types"]


def test_set_lesson_confirm_restarts_the_year_and_text_edit_reembeds(pt, env):
    out = pt.set_lesson("a", status="confirmed")
    patch = [w for w in env.store.writes if w[:2] == ("lessons", "update")][-1][2]
    assert patch["status"] == "confirmed" and patch["review_due"]
    out = pt.set_lesson("a", text="Infoblox decides in the last fortnight; plan for it.")
    assert out["embedded"] is True


def test_set_lesson_refuses_nothing_to_change_and_unknown_ids(pt, env):
    assert "nothing to change" in pt.set_lesson("a")["error"]
    assert "no lesson" in pt.set_lesson("zzz", status="retired", reason="x")["error"]


def test_retire_keeps_the_reason(pt, env):
    pt.set_lesson("a", status="retired", reason="Their process changed")
    patch = [w for w in env.store.writes if w[:2] == ("lessons", "update")][-1][2]
    assert patch == {"status": "retired", "retired_reason": "Their process changed"}


def test_non_team_caller_is_refused(pt, env, server, monkeypatch):
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (False, "tree access is team-only"))
    assert "error" in pt.log_lesson("Something we learned here.", "practice")
    assert not _inserts(env)


# ── Phase 3: the guard on client-facing spine writes ─────────────────


def _guard_store():
    return _Store({
        "projects": [{"id": "p-ibx", "company_id": "ibx", "project_types": ["campaign"]}],
        "lessons": [{"id": "j", "text": "Janet ends a review round only once legal has seen the work.",
                     "scope": "person", "company_id": "ibx", "status": "confirmed",
                     "stakeholder_name": "Janet Noe", "project_types": [], "moments": []}],
    })


def test_guard_refuses_a_proposal_that_quotes_a_client_lesson(server):
    out = server._internal_lesson_guard(
        _guard_store(), "p-ibx", "Engagement Summary — AI Campaign",
        "Timeline: Janet ends a review round only once legal has seen the work, so...")
    assert "nothing was written" in out["error"]
    assert out["internal_lessons_quoted"][0]["id"] == "j"


def test_guard_lets_names_and_internal_documents_through(server):
    store = _guard_store()
    assert server._internal_lesson_guard(
        store, "p-ibx", "Engagement Summary — AI Campaign", "Janet Noe approves each round.") is None
    assert server._internal_lesson_guard(
        store, "p-ibx", "Synthesis — working notes",
        "Janet ends a review round only once legal has seen the work.") is None


def test_create_spine_element_runs_the_guard_before_writing(server, monkeypatch):
    store = _guard_store()
    monkeypatch.setattr(server, "user_client", lambda: store)
    monkeypatch.setattr(server, "caller_subject", lambda: "u1")
    monkeypatch.setattr(server, "resolve_write_scope",
                        lambda c, code: {"id": "p-ibx", "project_code": "ibx-5153"})
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    out = server.create_spine_element(
        "ibx-5153", "Engagement Summary — AI Campaign",
        "Janet ends a review round only once legal has seen the work.", layer="brief")
    assert "internal lesson" in out["error"]
    assert not [w for w in store.writes if w[0] == "spine_substance"]


def test_get_project_state_documents_lookouts_as_internal(server):
    assert "INTERNAL" in server.get_project_state.__doc__
