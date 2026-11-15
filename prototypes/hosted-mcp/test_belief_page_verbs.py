"""`set_belief_page`, and `belief_pages` on `get_map_state`.

A belief check publishes a page (a claude.ai artifact link) of what Claude
believes about a project, for a person to correct. The link lived only in the
chat that made it, so the next session could not find the page or tell
whether it had been answered. mc-2 migration 226 added
`project_belief_pages`; these verbs keep it. What these tests pin:

- the first call for a url inserts, dated today unless a day is passed, and
  names the caller;
- a later call for the same url updates only the fields passed, and a call
  that changes nothing writes nothing;
- `answered_on=""` clears it;
- a url that is not `https://` and a date that is not `YYYY-MM-DD` are
  refused, with nothing written;
- it works on a workstream with no agreement, and never reads the estimate;
- `get_map_state` lists the pages newest first, an empty list when there are
  none, and a failed read of the table does not fail the verb;
- the writer is on `/mcp` only.

    python -m pytest prototypes/hosted-mcp/test_belief_page_verbs.py -v
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_map_verbs import ESTIMATE, _Audit, _Store  # noqa: E402

TABLE = "project_belief_pages"
URL = "https://claude.ai/artifact/page-one"
OTHER = "https://claude.ai/artifact/page-two"


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


@pytest.fixture
def env(server, monkeypatch):
    store = _Store({TABLE: [], "spine_map_marks": [], "fathom_meetings": [],
                    "work_item_deliverables": [], "spine_substance": [],
                    "estimator.phases": [], "rpc:map_agreements": []})
    audit = _Audit()
    estimates: list[str] = []

    def fake_estimate(client, pid):
        estimates.append(pid)
        return ESTIMATE if pid == "p-ibx" else None

    import cp_engine.estimate as estimate_mod

    monkeypatch.setattr(server, "user_client", lambda: store)
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(server, "caller_email", lambda: "drew@example.com")
    monkeypatch.setattr(server, "resolve_project_id",
                        lambda c, code: {"ibx-5153": "p-ibx", "1pi-9005": "p-init"}.get(code))
    monkeypatch.setattr(server, "resolve_company_id", lambda c, pid: "co")
    monkeypatch.setattr(server, "_level_for", lambda code: {"code": code, "label": "job"})
    monkeypatch.setattr(server, "audit", audit)
    monkeypatch.setattr(server, "call_mc2_get", lambda path, what: {"ok": True, "data": []})
    monkeypatch.setattr(estimate_mod, "fetch_estimate", fake_estimate)
    return SimpleNamespace(store=store, audit=audit, estimates=estimates)


def _writes(env):
    return [(op, row) for t, op, row in env.store.writes if t == TABLE]


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


# ── registration and gating ──────────────────────────────────────────


def test_registration(server):
    main = {t.name for t in server.mcp_server._tool_manager.list_tools()}
    read = {t.name for t in server.read_server._tool_manager.list_tools()}
    assert "set_belief_page" in main and "set_belief_page" in server.MAIN_ONLY_TOOLS
    assert "set_belief_page" not in read and "set_belief_page" not in server.READ_ONLY_TOOLS
    assert "get_map_state" in read


def test_a_non_member_is_refused_and_nothing_is_written(server, pt, env, monkeypatch):
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (False, "tree access is for the team"))
    assert "error" in pt.set_belief_page("ibx-5153", URL)
    assert env.store.writes == []


def test_an_unknown_project_is_refused(pt, env):
    out = pt.set_belief_page("ghost", URL)
    assert "no project or initiative resolves" in out["error"] and env.store.writes == []


# ── the first call for a url ─────────────────────────────────────────


def test_the_first_call_inserts_dated_today_and_names_the_caller(pt, env):
    out = pt.set_belief_page("ibx-5153", f"  {URL} ")
    assert "error" not in out
    assert out["created"] is True
    assert out["belief_page"] == {"url": URL, "published_on": _today(), "answered_on": None,
                                  "note": None, "set_by": "drew@example.com"}
    assert out["level"] == {"code": "ibx-5153", "label": "job"}
    assert _writes(env) == [("insert", {
        "project_id": "p-ibx", "url": URL, "published_on": _today(),
        "answered_on": None, "note": None, "set_by": "drew@example.com"})]
    assert env.audit[-1][0] == "set_belief_page" and env.audit[-1][2] == 1


def test_the_first_call_takes_the_day_it_went_out_and_a_note(pt, env):
    out = pt.set_belief_page("ibx-5153", URL, published_on="2026-11-10",
                             answered_on="2026-11-12", note="  first pass ")
    assert out["created"] is True
    assert out["belief_page"]["published_on"] == "2026-11-10"
    assert out["belief_page"]["answered_on"] == "2026-11-12"
    assert out["belief_page"]["note"] == "first pass"


def test_it_works_on_a_workstream_with_no_agreement_and_reads_no_estimate(pt, env):
    out = pt.set_belief_page("1pi-9005", URL)
    assert out["created"] is True and _writes(env)[0][1]["project_id"] == "p-init"
    assert env.estimates == []


def test_the_same_url_on_another_workstream_is_its_own_page(pt, env):
    pt.set_belief_page("ibx-5153", URL)
    out = pt.set_belief_page("1pi-9005", URL)
    assert out["created"] is True and [op for op, _ in _writes(env)] == ["insert", "insert"]


# ── a later call for the same url ────────────────────────────────────


def test_a_later_call_updates_only_the_fields_passed(pt, env):
    pt.set_belief_page("ibx-5153", URL, published_on="2026-11-10", note="first pass")
    out = pt.set_belief_page("ibx-5153", URL, answered_on="2026-11-13")
    assert out["created"] is False and out["changed"] == ["answered_on"]
    assert out["belief_page"] == {"url": URL, "published_on": "2026-11-10",
                                  "answered_on": "2026-11-13", "note": "first pass",
                                  "set_by": "drew@example.com"}
    op, row = _writes(env)[-1]
    assert op == "update"
    assert {k: v for k, v in row.items() if k != "id"} == {
        "answered_on": "2026-11-13", "set_by": "drew@example.com"}
    assert len(env.store.tables[TABLE]) == 1


def test_an_update_names_whoever_made_it(server, pt, env, monkeypatch):
    pt.set_belief_page("ibx-5153", URL)
    monkeypatch.setattr(server, "caller_email", lambda: "tony@example.com")
    out = pt.set_belief_page("ibx-5153", URL, note="Tony answered in chat")
    assert out["belief_page"]["set_by"] == "tony@example.com"


def test_an_empty_answered_on_clears_it(pt, env):
    pt.set_belief_page("ibx-5153", URL, published_on="2026-11-10", answered_on="2026-11-13",
                       note="first pass")
    out = pt.set_belief_page("ibx-5153", URL, answered_on="")
    assert out["belief_page"]["answered_on"] is None and out["changed"] == ["answered_on"]
    assert out["belief_page"]["note"] == "first pass"
    assert _writes(env)[-1][1]["answered_on"] is None
    assert env.store.tables[TABLE][0]["answered_on"] is None


def test_an_empty_note_clears_it(pt, env):
    pt.set_belief_page("ibx-5153", URL, note="first pass")
    out = pt.set_belief_page("ibx-5153", URL, note="")
    assert out["belief_page"]["note"] is None and out["changed"] == ["note"]


def test_a_call_that_changes_nothing_writes_nothing(pt, env):
    pt.set_belief_page("ibx-5153", URL, published_on="2026-11-10")
    before = len(env.store.writes)
    out = pt.set_belief_page("ibx-5153", URL, published_on="2026-11-10")
    assert "error" not in out and out["created"] is False and out["changed"] == []
    assert len(env.store.writes) == before and env.audit[-1][2] == 0


# ── refusals ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "http://claude.ai/artifact/page-one",
    "claude.ai/artifact/page-one",
    "https://",
    "https://claude.ai/artifact/page one",
    "",
])
def test_a_url_that_is_not_https_is_refused(pt, env, url):
    out = pt.set_belief_page("ibx-5153", url)
    assert "https://" in out["error"] and env.store.writes == []


@pytest.mark.parametrize("kwargs,needle", [
    ({"published_on": "last week"}, "published_on must be a date as YYYY-MM-DD"),
    ({"published_on": "11/14/2026"}, "published_on must be a date as YYYY-MM-DD"),
    ({"published_on": "20261114"}, "published_on must be a date as YYYY-MM-DD"),
    ({"published_on": "2026-02-30"}, "published_on must be a date as YYYY-MM-DD"),
    ({"answered_on": "yesterday"}, "answered_on must be a date as YYYY-MM-DD"),
    ({"published_on": ""}, "published_on cannot be cleared"),
])
def test_a_bad_date_is_refused(pt, env, kwargs, needle):
    out = pt.set_belief_page("ibx-5153", URL, **kwargs)
    assert needle in out["error"] and env.store.writes == []
    # On a page already recorded too: the row is left as it was.
    pt.set_belief_page("ibx-5153", URL, published_on="2026-11-10")
    before = len(env.store.writes)
    again = pt.set_belief_page("ibx-5153", URL, **kwargs)
    assert needle in again["error"] and len(env.store.writes) == before


# ── get_map_state ────────────────────────────────────────────────────


def _row(id_, url, published_on, created_at, pid="p-ibx", **more):
    return {"id": id_, "project_id": pid, "url": url, "published_on": published_on,
            "answered_on": None, "note": None, "set_by": "drew@example.com",
            "created_at": created_at, **more}


def test_the_map_state_lists_the_pages_newest_first(pt, env):
    env.store.tables[TABLE] = [
        _row("b1", "https://claude.ai/artifact/oldest", "2026-11-01", "2026-11-01T09:00:00+00:00",
             answered_on="2026-11-02", note="answered in chat"),
        _row("b2", "https://claude.ai/artifact/same-day-early", "2026-11-10", "2026-11-10T09:00:00+00:00"),
        _row("b3", "https://claude.ai/artifact/newest", "2026-11-14", "2026-11-14T09:00:00+00:00"),
        _row("b4", "https://claude.ai/artifact/same-day-late", "2026-11-10", "2026-11-10T17:00:00+00:00"),
        _row("b5", "https://claude.ai/artifact/elsewhere", "2026-11-15", "2026-11-15T09:00:00+00:00",
             pid="p-init"),
    ]
    out = pt.get_map_state("ibx-5153")
    assert "error" not in out and "belief_pages_error" not in out
    assert [p["url"].rsplit("/", 1)[1] for p in out["belief_pages"]] == [
        "newest", "same-day-late", "same-day-early", "oldest"]
    assert out["belief_pages"][-1] == {
        "url": "https://claude.ai/artifact/oldest", "published_on": "2026-11-01",
        "answered_on": "2026-11-02", "note": "answered in chat", "set_by": "drew@example.com"}
    assert len(out["phases"]) == 2 and env.store.writes == []


def test_the_map_state_reads_back_what_the_verb_wrote(pt, env):
    pt.set_belief_page("ibx-5153", URL, published_on="2026-11-10")
    pt.set_belief_page("ibx-5153", OTHER, published_on="2026-11-12")
    pt.set_belief_page("ibx-5153", URL, answered_on="2026-11-13")
    out = pt.get_map_state("ibx-5153")
    assert [(p["url"], p["answered_on"]) for p in out["belief_pages"]] == [
        (OTHER, None), (URL, "2026-11-13")]


def test_with_no_page_the_map_state_has_an_empty_list(pt, env):
    for code in ("ibx-5153", "1pi-9005"):
        out = pt.get_map_state(code)
        assert out["belief_pages"] == [] and "belief_pages_error" not in out


def test_pages_that_cannot_be_read_do_not_fail_the_map_state(pt, env, monkeypatch):
    real = env.store.table

    def table(name):
        if name == TABLE:
            raise RuntimeError("permission denied for table project_belief_pages")
        return real(name)

    monkeypatch.setattr(env.store, "table", table)
    out = pt.get_map_state("ibx-5153")
    assert "error" not in out
    assert out["belief_pages"] == [] and "permission denied" in out["belief_pages_error"]
    assert len(out["phases"]) == 2 and "meetings" in out
