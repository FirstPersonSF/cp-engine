"""Every capture names its level + `promote_uphill` on the hosted server (#304).

Plan §3.6. Two contracts, each with the failure it guards against:

1. **The response says where the write landed.** Every write verb that
   takes a workstream code echoes `level: {code, label, parent}` read off
   `.cp-engine/paths.json` on the tree clone, and its description states
   the rule. Without the echo a caller who named the wrong level finds out
   at the next weekly review; without the rule in the description a hosted
   session (which never sees CLAUDE.md unless it asks) has no way to know
   there IS a level to name.

2. **`promote_uphill` is the only way up.** It copies a commitment to the
   PARENT from the index (never a guessed code), leaves the original,
   leaves a step on the parent's trail, and does nothing the second time.
   The decision path is CARRIED, not served: this server cannot write a
   sprint file, so it POSTs to mc-2 under the caller's own token and mc-2
   hands the signed hop to cp-engine-webhook, which runs the CLI's own
   `promote_decision` on a clone (v0.124.1). The tests below fake httpx and
   assert the payload, the merged result, and the degraded answer when
   MC2_API_BASE is unset — never a pretend success.

The tools are plain callables (`MCPServer.tool()` returns the function), so
these call them directly with the identity, client and tree monkeypatched.

    python -m pytest prototypes/hosted-mcp/test_promote_uphill.py -v
"""
from __future__ import annotations

import ast
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

SRC_PATH = Path(__file__).resolve().parent / "server.py"
SRC = SRC_PATH.read_text(encoding="utf-8")

CHILD = "ggl-5136-go-safety-website"
PROGRAM = "ggl-5300-go-safety"
ACCOUNT = "google"
CHILD_ID = "11111111-1111-1111-1111-111111111111"
PROGRAM_ID = "22222222-2222-2222-2222-222222222222"
ACCOUNT_ID = "33333333-3333-3333-3333-333333333333"
SCOPES = {
    CHILD: {"id": CHILD_ID, "kind": "project", "project_code": CHILD},
    PROGRAM: {"id": PROGRAM_ID, "kind": "project", "project_code": PROGRAM},
    ACCOUNT: {"id": ACCOUNT_ID, "kind": "project", "project_code": ACCOUNT},
}


@pytest.fixture
def server(monkeypatch):
    for k, v in {
        "MC2_API_BASE": "http://example.invalid",
        "SUPABASE_URL": "http://example.invalid",
        "SUPABASE_ANON_KEY": "x",
    }.items():
        monkeypatch.setenv(k, v)
    import server as mod

    return mod


# ── fakes ─────────────────────────────────────────────────────────────


class _Query:
    def __init__(self, store, table):
        self._rows = store.setdefault(table, [])
        self._filters = []
        self._op = "select"
        self._payload = None

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def eq(self, column, value):
        self._filters.append((column, value))
        return self

    def _passthrough(self, *_a, **_k):
        return self

    in_ = ilike = order = limit = neq = is_ = or_ = _passthrough

    def execute(self):
        if self._op == "insert":
            rows = self._payload if isinstance(self._payload, list) else [self._payload]
            out = []
            for r in rows:
                r = dict(r)
                r.setdefault("id", str(uuid.uuid4()))
                self._rows.append(r)
                out.append(dict(r))
            return SimpleNamespace(data=out)
        hit = [dict(r) for r in self._rows
               if all(str(r.get(c)) == str(v) for c, v in self._filters)]
        return SimpleNamespace(data=hit)


class FakeClient:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _Query(self.store, name)

    def rpc(self, *_a, **_k):
        return SimpleNamespace(execute=lambda: SimpleNamespace(data="entity-1"))

    def rows(self, table):
        return self.store.get(table, [])


def _tree(tmp_path: Path) -> Path:
    doc = {
        "version": 1,
        "generated_at": "2026-09-24T00:00:00+00:00",
        "workstreams": {
            ACCOUNT: {"path": "1p/google", "parent": None, "label": "account",
                      "has_agreement": False, "mc2_id": ACCOUNT_ID, "company": "GGL",
                      "status": "Open"},
            PROGRAM: {"path": f"1p/google/{PROGRAM}", "parent": ACCOUNT, "label": "program",
                      "has_agreement": True, "mc2_id": PROGRAM_ID, "company": "GGL",
                      "status": "Open"},
            CHILD: {"path": f"1p/google/{PROGRAM}/{CHILD}", "parent": PROGRAM,
                    "label": "job", "has_agreement": True, "mc2_id": CHILD_ID,
                    "company": "GGL", "status": "Open"},
        },
    }
    (tmp_path / ".cp-engine").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".cp-engine" / "paths.json").write_text(json.dumps(doc), encoding="utf-8")
    return tmp_path


@pytest.fixture
def wired(server, monkeypatch, tmp_path):
    """Identity, client, scope resolution and the tree — all faked."""
    client = FakeClient()
    client.store["commitments"] = [{
        "id": "c-orig", "project_id": CHILD_ID, "status": "open", "cp_hash": "abcd1234",
        "description": "Send Janet the migration runbook", "owner_email": "drew@firstperson.is",
        "owner_name": "Drew", "direction": "us_to_them", "due_date": "2026-10-01",
        "work_item_id": None, "work_item_kind": "deliverable", "spine_element_id": None,
        "source_meeting_id": "m-1",
    }]
    monkeypatch.setattr(server, "user_client", lambda: client)
    monkeypatch.setattr(server, "caller_subject", lambda: "user-sub-1")
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    monkeypatch.setattr(server, "resolve_write_scope", lambda c, code: SCOPES.get(code))
    monkeypatch.setattr(server, "tree_available", lambda: (True, ""))
    root = _tree(tmp_path)
    monkeypatch.setattr(server, "tree_root", lambda: root)
    return client


# ──────────────────────────────────────────────────────────────────────
#  1. Every capture names its level
# ──────────────────────────────────────────────────────────────────────


def _tools(tree):
    """{name: FunctionDef} for every registered tool."""
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        if any(
            isinstance((d.func if isinstance(d, ast.Call) else d), ast.Attribute)
            and (d.func if isinstance(d, ast.Call) else d).attr == "tool"
            for d in node.decorator_list
        ):
            out[node.name] = node
    return out


def _writers(tree):
    """Derived from the tree, the way the instructions test derives them —
    NOT hand-listed, so a new write verb cannot slip past."""
    return {
        name for name, node in _tools(tree).items()
        if any(k in ast.dump(node)
               for k in ("'insert'", "'update'", "'upsert'", "'delete'", "call_mc2_"))
    }


def _has_level_decorator(node) -> bool:
    return any("_names_its_level" in ast.unparse(d) for d in node.decorator_list)


def _code_params(node) -> set[str]:
    names = {a.arg for a in node.args.args + node.args.kwonlyargs}
    return {n for n in names if n.endswith("_code")}


def test_every_write_verb_with_a_workstream_code_echoes_its_level():
    tree = ast.parse(SRC)
    tools = _tools(tree)
    missing = sorted(
        name for name in _writers(tree)
        if _code_params(tools[name]) and not _has_level_decorator(tools[name])
    )
    assert not missing, f"write verbs without the level echo: {missing}"


def test_the_decorator_sits_under_the_registration():
    """`@mcp_server.tool()` must see the WRAPPER, or the registered tool has
    neither the echo nor the rule in its description."""
    tree = ast.parse(SRC)
    for name, node in _tools(tree).items():
        decos = [ast.unparse(d) for d in node.decorator_list]
        if not any("_names_its_level" in d for d in decos):
            continue
        assert decos[0].startswith("mcp_server.tool"), (name, decos)


def test_read_verbs_do_not_claim_a_level():
    tree = ast.parse(SRC)
    tools = _tools(tree)
    for name in ("list_commitments", "list_spine_elements", "get_project_state",
                 "list_worksets", "spine_lint", "wrap_status"):
        assert not _has_level_decorator(tools[name]), name


def test_every_decorated_verb_states_the_rule_in_its_description(server):
    tree = ast.parse(SRC)
    for name, node in _tools(tree).items():
        if not _has_level_decorator(node):
            continue
        tool = server.mcp_server._tool_manager.get_tool(name)
        assert "never inferred from content" in tool.description, name
        assert "promote_uphill" in tool.description, name


def test_decorated_verbs_keep_their_real_parameters(server):
    """A wrapper that leaked `*args, **kwargs` into the schema would make
    every write verb unusable from a client."""
    tool = server.mcp_server._tool_manager.get_tool("create_commitment")
    assert list(tool.parameters["properties"]) == [
        "project_code", "description", "owner_email", "due_date", "direction"
    ]
    tool = server.mcp_server._tool_manager.get_tool("pull_element_from_project")
    assert "to_code" in tool.parameters["properties"]


def test_the_instructions_state_the_rule(server):
    text = server.mcp_server.instructions
    assert "EVERY CAPTURE NAMES ITS LEVEL" in text
    assert "promote_uphill" in text


def test_create_commitment_echoes_the_level_it_landed_on(server, wired):
    out = server.create_commitment(CHILD, "Book the launch review", due_date="2026-10-02")
    assert out["commitment_id"], out
    assert out["level"] == {"code": CHILD, "label": "job", "parent": PROGRAM, "indexed": True}


def test_level_echo_uses_the_short_code_when_unique(server, wired):
    out = server.create_commitment(CHILD, "Book the launch review")
    assert out["level"]["parent"] == PROGRAM
    monkeypatch_scopes = dict(SCOPES)
    monkeypatch_scopes["ggl-5136"] = SCOPES[CHILD]
    server.resolve_write_scope = lambda c, code: monkeypatch_scopes.get(code)
    out = server.create_commitment("ggl-5136", "Book the launch review again")
    assert out["level"]["code"] == CHILD and out["level"]["parent"] == PROGRAM


def test_an_unindexed_code_is_echoed_as_unindexed_not_guessed(server, wired, monkeypatch):
    monkeypatch.setattr(server, "resolve_write_scope",
                        lambda c, code: {"id": "z", "kind": "project", "project_code": code})
    out = server.create_commitment("zzz-9999", "Something on an unsynced workstream")
    assert out["commitment_id"]
    assert out["level"] == {"code": "zzz-9999", "label": None, "parent": None, "indexed": False}


def test_an_unavailable_tree_never_fails_the_write(server, wired, monkeypatch):
    monkeypatch.setattr(server, "tree_available", lambda: (False, "no TENANT_REPO"))
    out = server.create_commitment(CHILD, "Book the launch review")
    assert out["commitment_id"] and out["level"]["indexed"] is False


def test_an_error_result_carries_no_level(server, wired):
    out = server.create_commitment(CHILD, "")
    assert "error" in out and "level" not in out


def test_the_echo_covers_a_note_and_a_step(server, wired, monkeypatch):
    note = server.create_note(CHILD, "ping")
    assert note["note_id"] and note["level"]["parent"] == PROGRAM
    monkeypatch.setattr(server, "resolve_live_element_id",
                        lambda c, pid, key: ("_authored/x", None))
    step = server.propose_spine_step(CHILD, "_authored/x", "Ratified the pillars")
    assert step["proposed"] and step["level"]["code"] == CHILD


# ──────────────────────────────────────────────────────────────────────
#  2. promote_uphill
# ──────────────────────────────────────────────────────────────────────


def test_promote_uphill_is_registered_and_counted_as_a_writer(server):
    tree = ast.parse(SRC)
    assert "promote_uphill" in _tools(tree)
    assert "promote_uphill" in _writers(tree), "it inserts — the count must say so"
    tool = server.mcp_server._tool_manager.get_tool("promote_uphill")
    assert list(tool.parameters["properties"]) == ["project_code", "item_kind", "item_ref", "note", "week"]


def test_commitment_copy_lands_on_the_parent_with_the_original_untouched(server, wired):
    before = dict(wired.rows("commitments")[0])
    out = server.promote_uphill(CHILD, "commitment", "c-orig")
    assert out["ok"] and out["promoted"] and not out["already"], out
    assert out["from"]["code"] == CHILD and out["to"]["code"] == PROGRAM
    assert out["level"]["code"] == PROGRAM, "the echo names where the copy landed"
    assert wired.rows("commitments")[0] == before
    copies = [r for r in wired.rows("commitments") if r["id"] != "c-orig"]
    assert len(copies) == 1
    copy = copies[0]
    assert copy["project_id"] == PROGRAM_ID
    assert copy["source_kind"] == "promoted"
    assert copy["status"] == "open" and copy["date_status"] == "proposed"
    assert copy["work_item_kind"] == "deliverable"
    assert copy["cp_hash"] == server._promoted_hash("abcd1234", PROGRAM) == out["cp_hash"]
    assert out["commitment_id"] == copy["id"]


def test_commitment_promotion_leaves_a_step_on_the_parent(server, wired):
    out = server.promote_uphill(CHILD, "commitment", "c-orig", note="Janet owns it")
    elements = wired.rows("spine_substance")
    assert len(elements) == 1
    el = elements[0]
    assert el["project_id"] == PROGRAM_ID
    assert el["est_item_id"] == "_authored/promoted-uphill"
    assert el["author_id"] == "user-sub-1", "the INSERT policy needs the caller"
    assert el["status"] == "live"
    steps = wired.rows("spine_steps")
    assert len(steps) == 1
    step = steps[0]
    assert step["project_id"] == PROGRAM_ID
    assert step["est_item_id"] == "_authored/promoted-uphill"
    assert step["title"] == f"Promoted from {CHILD}: Send Janet the migration runbook"
    assert f"commitment c-orig promoted from {CHILD}" in step["note"]
    assert "Janet owns it" in step["note"]
    assert step["status"] == "done"
    assert out["step"]["position"] == 1


def test_commitment_second_call_is_idempotent(server, wired):
    first = server.promote_uphill(CHILD, "commitment", "c-orig")
    second = server.promote_uphill(CHILD, "commitment", "c-orig")
    assert first["promoted"] is True
    assert second["ok"] and second["already"] is True and second["promoted"] is False
    assert second["commitment_id"] == first["commitment_id"]
    assert len(wired.rows("commitments")) == 2
    assert len(wired.rows("spine_steps")) == 1
    assert len(wired.rows("spine_substance")) == 1


def test_no_parent_is_refused(server, wired):
    wired.rows("commitments")[0]["project_id"] = ACCOUNT_ID
    out = server.promote_uphill(ACCOUNT, "commitment", "c-orig")
    assert "no parent" in out["error"]
    assert out["level"]["code"] == ACCOUNT
    assert len(wired.rows("commitments")) == 1


def test_unknown_commitment_is_an_error(server, wired):
    out = server.promote_uphill(CHILD, "commitment", "nope")
    assert "no commitment with id" in out["error"]
    assert len(wired.rows("commitments")) == 1


def test_a_siblings_commitment_is_refused(server, wired):
    wired.rows("commitments")[0]["project_id"] = PROGRAM_ID
    out = server.promote_uphill(CHILD, "commitment", "c-orig")
    assert "is not on " + CHILD in out["error"]


def test_unindexed_code_is_an_error(server, wired, monkeypatch):
    monkeypatch.setattr(server, "resolve_write_scope",
                        lambda c, code: {"id": "z", "kind": "project", "project_code": code})
    out = server.promote_uphill("zzz-9999", "commitment", "c-orig")
    assert "paths.json" in out["error"]


def test_unknown_item_kind_is_an_error(server, wired):
    out = server.promote_uphill(CHILD, "risk", "c-orig")
    assert "item_kind" in out["error"]


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body) if not isinstance(body, str) else body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


def _fake_httpx(server, monkeypatch, status=200, body=None, *, raise_exc=None):
    """Capture the POST the decision path makes and answer it."""
    box = {}

    def _post(url, headers=None, json=None, timeout=None):
        box.update(url=url, headers=headers, payload=json, timeout=timeout)
        if raise_exc is not None:
            raise raise_exc
        return _FakeResponse(status, body)

    monkeypatch.setattr(server.httpx, "post", _post)
    monkeypatch.setattr(server, "caller_jwt", lambda: "jwt-of-the-caller")
    return box


BACKEND_OK = {
    "ok": True, "promoted": True, "already": False, "item_kind": "decision",
    "item_ref": "abcd1234", "parent_code": PROGRAM,
    "from": {"code": CHILD, "label": "job", "parent": PROGRAM, "indexed": True},
    "to": {"code": PROGRAM, "label": "program", "parent": ACCOUNT, "indexed": True},
    "level": {"code": PROGRAM, "label": "program", "parent": ACCOUNT, "indexed": True},
    "cp_hash": "0badf00d", "sprint_path": f"sprints/2026-W39/{PROGRAM}.md",
    "source": f"sprints/2026-W39/{CHILD}.md", "commit": "deadbeefcafe",
    "step": {"est_item_id": "_authored/promoted-uphill", "position": 1},
}


def test_decision_path_is_carried_to_mc2_under_the_callers_token(server, wired, monkeypatch):
    """The file write lives on the webhook; this server's job is to hand the
    call to mc-2 with the caller's own JWT and pass the landing back."""
    box = _fake_httpx(server, monkeypatch, 200, BACKEND_OK)
    out = server.promote_uphill(CHILD, "decision", "abcd1234", note="account-wide")
    assert box["url"].endswith("/api/promote-uphill")
    assert box["headers"] == {"Authorization": "Bearer jwt-of-the-caller"}
    assert box["payload"] == {
        "project_code": CHILD, "item_kind": "decision",
        "item_ref": "abcd1234", "note": "account-wide",
    }
    assert "actor" not in box["payload"] and "user" not in box["payload"], (
        "identity comes from the token, never from this hop"
    )
    assert out["ok"] is True and out["promoted"] is True and out["already"] is False
    assert out["item_kind"] == "decision"
    assert out["sprint_path"] == BACKEND_OK["sprint_path"]
    assert out["commit"] == "deadbeefcafe"
    assert out["level"]["code"] == PROGRAM, "the echo names where the copy landed"
    assert "unsupported_here" not in out
    # nothing is written locally — the DB path for the step runs on the webhook
    assert not wired.rows("spine_steps") and len(wired.rows("commitments")) == 1


def test_decision_already_promoted_passes_through(server, wired, monkeypatch):
    body = {**BACKEND_OK, "promoted": False, "already": True, "commit": None}
    _fake_httpx(server, monkeypatch, 200, body)
    out = server.promote_uphill(CHILD, "decision", "abcd1234")
    assert out["ok"] and out["already"] is True and out["promoted"] is False
    assert out["commit"] is None and out["level"]["code"] == PROGRAM


def test_decision_refusal_from_mc2_is_reported_not_faked(server, wired, monkeypatch):
    _fake_httpx(server, monkeypatch, 400, {"detail": "no parent: google is a top-level workstream"})
    out = server.promote_uphill(ACCOUNT, "decision", "abcd1234")
    assert out["ok"] is False and out["refused"] is True
    assert "no parent" in out["reason"]
    assert out["level"]["code"] == ACCOUNT, "the echo still tells the caller where they are"
    assert "promoted" not in out


def test_decision_unauthorized_names_the_token(server, wired, monkeypatch):
    _fake_httpx(server, monkeypatch, 401, {"detail": "expired"})
    out = server.promote_uphill(CHILD, "decision", "abcd1234")
    assert out["ok"] is False and out["unauthorized"] is True


def test_decision_timeout_says_a_retry_is_safe(server, wired, monkeypatch):
    import httpx

    _fake_httpx(server, monkeypatch, raise_exc=httpx.TimeoutException("slow"))
    out = server.promote_uphill(CHILD, "decision", "abcd1234")
    assert out["ok"] is False and out["timeout"] is True
    assert "no-op" in out["reason"]


def test_decision_non_json_success_is_not_a_success(server, wired, monkeypatch):
    _fake_httpx(server, monkeypatch, 200, "<html>gateway</html>")
    out = server.promote_uphill(CHILD, "decision", "abcd1234")
    assert out["ok"] is False and "non-object" in out["reason"]


def test_decision_is_degraded_when_mc2_is_unconfigured(server, wired, monkeypatch):
    """No MC2_API_BASE → say so. Never a pretend success, never a raise."""
    monkeypatch.setattr(server, "MC2_API_BASE", "")
    called = {}
    monkeypatch.setattr(server.httpx, "post", lambda *a, **k: called.setdefault("hit", True))
    out = server.promote_uphill(CHILD, "decision", "abcd1234")
    assert out["ok"] is False and out["degraded"] is True
    assert "MC2_API_BASE" in out["reason"]
    assert "hit" not in called
    assert out["level"]["code"] == CHILD


def test_decision_needs_a_ref(server, wired, monkeypatch):
    called = {}
    monkeypatch.setattr(server.httpx, "post", lambda *a, **k: called.setdefault("hit", True))
    out = server.promote_uphill(CHILD, "decision", "   ")
    assert "item_ref is required" in out["error"] and "hit" not in called


def test_call_helper_never_raises_on_a_connection_error(server, monkeypatch):
    import httpx

    _fake_httpx(server, monkeypatch, raise_exc=httpx.ConnectError("refused"))
    out = server.call_mc2_promote_uphill(CHILD, "abcd1234", None, None)
    assert out["ok"] is False and "could not reach mc-2" in out["reason"]


def test_call_helper_forwards_the_week(server, monkeypatch):
    box = _fake_httpx(server, monkeypatch, 200, BACKEND_OK)
    server.call_mc2_promote_uphill(CHILD, "abcd1234", "  ", "2026-W38")
    assert box["payload"]["week"] == "2026-W38"
    assert "note" not in box["payload"], "a blank note is not sent"


def test_the_description_no_longer_points_at_the_cli(server):
    tool = server.mcp_server._tool_manager.get_tool("promote_uphill")
    assert "unsupported_here" not in tool.description
    assert "mc-2" in tool.description and "sprint file" in tool.description
    assert "never inferred from content" in tool.description


def test_decision_week_is_forwarded_to_mc2(server, wired, monkeypatch):
    """A hosted caller can pick the parent's sprint week (v0.124.2); blank
    means the current week, and commitments never carry one."""
    box = _fake_httpx(server, monkeypatch, 200, BACKEND_OK)
    out = server.promote_uphill(CHILD, "decision", "abcd1234", note="account-wide", week="2026-W39")
    assert out.get("ok") is True
    assert box["payload"]["week"] == "2026-W39"

    box = _fake_httpx(server, monkeypatch, 200, BACKEND_OK)
    server.promote_uphill(CHILD, "decision", "abcd1234", week="   ")
    assert "week" not in box["payload"]
