"""The read-only endpoint, the audit guarantee, and the calling-app record
(cp-engine #141 — ChatGPT / Codex access, READ ONLY).

WHAT IS BEING PROTECTED. The tenant holds client-confidential material for
SAP, Google, Infoblox and Salesloft. Drew's 2026-09-30 decision lets a second
vendor's client (ChatGPT) read it — on `/mcp/read`, for the partners, under
their own RLS — and nothing more. Three things have to be true for that to be
the decision that was made rather than a larger one:

1. **`/mcp/read` cannot write.** Enforced by REGISTRATION, not by sniffing
   the client. The writer set is DERIVED here from `server.py`'s own code
   (table mutations, non-read RPCs, outbound POST/PATCH to mc-2) through each
   tool's transitive call graph — so a read tool that grows a write, or a new
   write verb put on the allowlist, fails this file without anyone having
   updated a list. A runtime pass then calls every read-endpoint tool against
   a recording client and asserts it mutated nothing.
2. **Every call is audited** — every registered tool, on both endpoints,
   including the early-return and exception paths that used to write no row.
   Checked by calling every tool through the registry, not by grepping for
   `audit(`: a static grep passes while `semantic_search` returns
   "unavailable" without a row.
3. **The row says who called, through which door.** `client` keeps the
   server version first and adds `endpoint=`, `oauth_client=` (the token's
   `client_id` claim — minted by the AS, not asserted by the app) and `app=`.

CONTROL. Run against the unfixed `server.py` (origin/main before #141) this
file fails: `read_server` does not exist, and the audit-coverage test names
the tools whose calls wrote no row.

    python -m pytest prototypes/hosted-mcp/test_readonly_endpoint.py -v
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

SERVER_PY = Path(__file__).resolve().parent / "server.py"
# Verbs ported from the retired stdio server (step 5b) live beside it and are
# registered from it; they are derived exactly like server.py's own.
PORTED_PY = Path(__file__).resolve().parent / "ported_tools.py"

# RPCs that only read. Every other `client.rpc(name)` is a writer.
READ_RPCS = {
    "match_spine_context", "match_chunks_for_project", "match_chunks_simple",
    "is_team_member", "caller_entity_id",
}
TABLE_MUTATIONS = {"insert", "update", "upsert", "delete"}
HTTP_WRITES = {"post", "put", "patch", "delete"}
# Writes that leave through a storage SDK rather than PostgREST or httpx: the
# engine's Dropbox upload, the connector's, and a minted upload link (whose
# holder can write without asking again).
EXTERNAL_WRITES = {"push_to_dropbox", "upload_file", "files_get_temporary_upload_link"}


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "http://example.invalid")
    mp.setenv("SUPABASE_ANON_KEY", "x")
    mp.setenv("MC2_API_BASE", "http://example.invalid")
    mp.delenv("TENANT_REPO", raising=False)
    mp.delenv("VOYAGE_API_KEY", raising=False)
    import server as mod

    yield mod
    mp.undo()


# ── 1a. derive the writer set from the code ──────────────────────────


def _derive_writers() -> dict[str, set[str]]:
    """{registered tool: write primitives reachable from it}, by AST.

    A primitive is `<...>.table(T).insert|update|upsert|delete(...)` (the
    receiver must be a `.table()` call, so `dict.update` is not a write), a
    `.rpc(name)` whose name is not in READ_RPCS (a non-literal name counts),
    or `httpx.post|put|patch|delete`. Reachability follows every module-level
    function a body names, except `audit` — whose one INSERT, into
    `mcp_audit_log`, is the thing every tool must do.
    """
    funcs: dict[str, ast.AST] = {}
    tools: list[str] = []
    for path in (SERVER_PY, PORTED_PY):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name not in funcs or path is SERVER_PY, (
                    f"{path.name} redefines {node.name}; the derivation keys by name"
                )
                funcs[node.name] = node
                for dec in node.decorator_list:
                    target = dec.func if isinstance(dec, ast.Call) else dec
                    if (isinstance(target, ast.Attribute) and target.attr == "tool"
                            and getattr(target.value, "id", None) == "mcp_server"):
                        tools.append(node.name)
                    if isinstance(target, ast.Name) and target.id == "hosted_tool":
                        tools.append(node.name)

    direct: dict[str, tuple[set[str], set[str]]] = {}
    for name, fn in funcs.items():
        prims: set[str] = set()
        refs: set[str] = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and n.id in funcs and n.id != name:
                refs.add(n.id)
            # ported_tools.py reaches server helpers as `_srv.<name>`.
            if (isinstance(n, ast.Attribute) and getattr(n.value, "id", None) == "_srv"
                    and n.attr in funcs and n.attr != name):
                refs.add(n.attr)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                attr, recv = n.func.attr, n.func.value
                if (attr in TABLE_MUTATIONS and isinstance(recv, ast.Call)
                        and isinstance(recv.func, ast.Attribute)
                        and recv.func.attr in ("table", "from_")):
                    arg = recv.args[0] if recv.args else None
                    table = arg.value if isinstance(arg, ast.Constant) else "?"
                    prims.add(f"{attr}:{table}")
                if attr == "rpc":
                    arg = n.args[0] if n.args else None
                    rpc = arg.value if isinstance(arg, ast.Constant) else "?"
                    if rpc not in READ_RPCS:
                        prims.add(f"rpc:{rpc}")
                if attr in HTTP_WRITES and getattr(recv, "id", None) == "httpx":
                    prims.add(f"http_{attr}")
                if attr in EXTERNAL_WRITES:
                    prims.add(f"external:{attr}")
        direct[name] = (prims, refs)

    def closure(start: str) -> set[str]:
        seen, stack, prims = set(), [start], set()
        while stack:
            f = stack.pop()
            if f in seen or f == "audit":
                continue
            seen.add(f)
            p, r = direct[f]
            prims |= p
            stack.extend(r)
        return prims

    return {t: closure(t) for t in tools}


def test_detector_sees_known_writers_and_known_readers():
    """The detector's own control: if it could not see a write, every test
    below would pass vacuously."""
    derived = _derive_writers()
    assert "insert:notes" in derived["create_note"]
    assert "rpc:spine_retire_element" in derived["retire_spine_element"]
    assert "http_post" in derived["capture_project_state"]
    assert "http_patch" in derived["set_commitment_date"]
    assert derived["list_spine_elements"] == set()
    assert derived["semantic_search"] == set()
    # The ported module is derived too (step 5b): its writer is seen, and its
    # reads — which reach server helpers through `_srv.` — are not mistaken
    # for writers.
    assert "external:push_to_dropbox" in derived["push_to_dropbox"]
    assert "external:files_get_temporary_upload_link" in derived["push_to_dropbox"]
    assert derived["fetch_project_source"] == set()
    assert derived["preflight"] == set()


def test_no_read_endpoint_tool_can_reach_a_write(server):
    derived = _derive_writers()
    served = {t.name for t in server.read_server._tool_manager.list_tools()}
    offenders = {t: sorted(derived[t]) for t in served if derived.get(t)}
    assert not offenders, f"/mcp/read serves tools that can write: {offenders}"


def test_derived_writers_are_exactly_the_main_only_tools(server):
    """Every tool NOT on /mcp/read must be justified — and the justification
    must be true: the derived writer set and MAIN_ONLY_TOOLS agree exactly. A
    new read verb that nobody allowlisted fails here (not a writer, not on the
    read list); so does a writer someone put on the read list."""
    derived = {t for t, p in _derive_writers().items() if p}
    assert derived == set(server.MAIN_ONLY_TOOLS), (
        f"derived-but-unlisted={sorted(derived - set(server.MAIN_ONLY_TOOLS))} "
        f"listed-but-not-derived={sorted(set(server.MAIN_ONLY_TOOLS) - derived)}"
    )
    assert all(reason.strip() for reason in server.MAIN_ONLY_TOOLS.values())
    assert all(reason.strip() for reason in server.READ_ONLY_TOOLS.values())


def test_classification_is_total_and_disjoint(server):
    registered = {t.name for t in server.mcp_server._tool_manager.list_tools()}
    read, main = set(server.READ_ONLY_TOOLS), set(server.MAIN_ONLY_TOOLS)
    assert not read & main
    assert read | main == registered


def test_read_endpoint_registers_exactly_the_allowlist(server):
    served = {t.name for t in server.read_server._tool_manager.list_tools()}
    assert served == set(server.READ_ONLY_TOOLS)
    # Nothing else a client could invoke: no prompts, no resources.
    assert asyncio.run(server.read_server.list_prompts()) == []
    assert asyncio.run(server.read_server.list_resources()) == []


def test_no_write_shaped_name_on_the_read_endpoint(server):
    """Belt to the derived braces: the verb families the instructions call
    writers never appear on /mcp/read."""
    prefixes = ("create_", "set_", "add_", "promote_", "retire_", "route_",
                "capture_", "remove_", "reorder_", "resolve_", "seal_to_",
                "rotate_", "archive_", "rename_", "demote_", "record_",
                "log_", "updates_")
    served = [t.name for t in server.read_server._tool_manager.list_tools()]
    assert not [n for n in served if n.startswith(prefixes)]


def test_read_tools_are_annotated_read_only(server):
    """ChatGPT gates any tool WITHOUT readOnlyHint behind a write
    confirmation; the read endpoint says what it is."""
    for tool in server.read_server._tool_manager.list_tools():
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False


def test_read_endpoint_tools_reject_unknown_arguments(server):
    """The #318 strictness applies to the second registry too."""
    for tool in server.read_server._tool_manager.list_tools():
        assert tool.fn_metadata.arg_model.model_config.get("extra") == "forbid", tool.name


def test_a_write_verb_is_unknown_on_the_read_endpoint(server):
    with pytest.raises(Exception) as err:
        asyncio.run(server.read_server.call_tool(
            "create_note", {"project_code": "x", "body": "y"}))
    assert "create_note" in str(err.value)


def test_main_endpoint_surface_is_unchanged(server):
    """`/mcp` still serves every tool, with no annotations added."""
    main = server.mcp_server._tool_manager.list_tools()
    assert len(main) == len(server.READ_ONLY_TOOLS) + len(server.MAIN_ONLY_TOOLS)
    assert all(t.annotations is None for t in main)


# ── runtime harness: a recording client + a caller in context ────────


class _Result:
    def __init__(self):
        self.data: list = []
        self.count = 0


class RecordingClient:
    """Every query builder call chains; every write is recorded."""

    def __init__(self, log: list):
        self._log = log
        self._table: str | None = None

    def _child(self, table=None):
        c = RecordingClient(self._log)
        c._table = table if table is not None else self._table
        return c

    def table(self, name):
        return self._child(name)

    from_ = table

    def insert(self, row, *a, **k):
        self._log.append(("insert", self._table, row))
        return self._child()

    def update(self, *a, **k):
        self._log.append(("update", self._table, None))
        return self._child()

    def upsert(self, *a, **k):
        self._log.append(("upsert", self._table, None))
        return self._child()

    def delete(self, *a, **k):
        self._log.append(("delete", self._table, None))
        return self._child()

    def rpc(self, name, *a, **k):
        self._log.append(("rpc", name, None))
        return self._child()

    def execute(self):
        return _Result()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _Chain(self)


class _Chain:
    def __init__(self, client):
        self._client = client

    def __call__(self, *a, **k):
        return self._client

    def __getattr__(self, name):
        return getattr(self._client, name)


def _dummy_args(tool) -> dict[str, Any]:
    props = tool.parameters.get("properties") or {}
    out: dict[str, Any] = {}
    for name in tool.parameters.get("required") or []:
        schema = props.get(name, {})
        kind = schema.get("type")
        if kind is None and "anyOf" in schema:
            kind = next((s.get("type") for s in schema["anyOf"] if s.get("type") != "null"), None)
        out[name] = {"integer": 1, "number": 1, "boolean": False,
                     "array": [], "object": {}}.get(kind, "zzz-0141")
    return out


@pytest.fixture
def harness(server, monkeypatch):
    """A verified caller in context, every client a recorder, every outbound
    write-shaped HTTP call refused (and recorded)."""
    from mcp.server.auth.middleware.auth_context import auth_context_var
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
    from mcp.server.auth.provider import AccessToken

    log: list = []
    monkeypatch.setattr(server, "user_client", lambda: RecordingClient(log))

    def refuse(method):
        def _refused(*a, **k):
            log.append(("http", method, None))
            raise RuntimeError(f"outbound {method} refused in test")
        return _refused

    for method in HTTP_WRITES:
        monkeypatch.setattr(server.httpx, method, refuse(method))

    token = auth_context_var.set(AuthenticatedUser(AccessToken(
        token="t", client_id="00000000-0000-4000-8000-000000000141", scopes=[],
        expires_at=None, subject="00000000-0000-4000-8000-000000000141",
        claims={"sub": "00000000-0000-4000-8000-000000000141",
                "client_id": "c1a7c1a7-0000-4000-8000-000000000141",
                "email": "partner@example.invalid"},
    )))
    try:
        yield log
    finally:
        auth_context_var.reset(token)


def _call(mcp, name, args):
    try:
        asyncio.run(mcp.call_tool(name, args))
    except Exception:  # noqa: BLE001 — a raising tool must still audit
        pass


def test_read_endpoint_tools_mutate_nothing_at_runtime(server, harness):
    """The runtime half of #1: every /mcp/read tool, called through the read
    registry against a recording client, performs no write other than its
    audit row — and makes no outbound write-shaped HTTP call."""
    for tool in server.read_server._tool_manager.list_tools():
        harness.clear()
        _call(server.read_server, tool.name, _dummy_args(tool))
        writes = [
            e for e in harness
            if e[0] in TABLE_MUTATIONS and not (e[0] == "insert" and e[1] == "mcp_audit_log")
            or e[0] == "http"
            or (e[0] == "rpc" and e[1] not in READ_RPCS)
        ]
        assert not writes, f"{tool.name} wrote via /mcp/read: {writes}"


# ── 2. audit coverage ───────────────────────────────────────────────


def _audit_rows(log):
    return [e[2] for e in log if e[0] == "insert" and e[1] == "mcp_audit_log"]


def test_every_registered_tool_is_wrapped_for_audit(server):
    for mcp in (server.mcp_server, server.read_server):
        for tool in mcp._tool_manager.list_tools():
            assert getattr(tool.fn, "__cp_audit_guaranteed__", False), tool.name


@pytest.mark.parametrize("endpoint", ["/mcp", "/mcp/read"])
def test_every_tool_call_writes_an_audit_row(server, harness, endpoint):
    """Every tool on both endpoints, called with dummy arguments that drive it
    down its early-return / error paths, writes at least one audit row — and
    none of them carries body content."""
    mcp = server.mcp_server if endpoint == "/mcp" else server.read_server
    unaudited = []
    for tool in mcp._tool_manager.list_tools():
        harness.clear()
        _call(mcp, tool.name, _dummy_args(tool))
        rows = _audit_rows(harness)
        if not rows:
            unaudited.append(tool.name)
            continue
        for row in rows:
            assert row["tool"] == tool.name
            assert set(row["args"]) <= (
                server._AUDIT_SAFE_ARGS
                | {f"{k}_len" for k in server._AUDIT_REDACTED_ARGS}
                | {"path_neutralized", "rel_path_neutralized"}
            ), (tool.name, row["args"])
    assert not unaudited, f"calls that wrote no audit row: {sorted(unaudited)}"


def test_a_tool_that_audits_itself_writes_one_row_not_two(server, harness):
    """The fallback fires only when the tool did not audit."""
    monkey_rows_before = len(_audit_rows(harness))
    tool = server.mcp_server._tool_manager.get_tool("wrap_bundle")
    # wrap_bundle audits on its own success path; with the recording client
    # its project lookup misses, so what matters is the COUNT, not the path.
    _call(server.mcp_server, "wrap_bundle", _dummy_args(tool))
    assert len(_audit_rows(harness)) - monkey_rows_before == 1


def test_fallback_row_records_the_outcome_never_the_message(server, harness):
    tool = server.mcp_server._tool_manager.get_tool("get_service")
    _call(server.mcp_server, "get_service", {"ref_id": "SECRET-PROSE-141"})
    row = _audit_rows(harness)[-1]
    assert row["tool"] == "get_service"
    assert "SECRET-PROSE-141" not in json.dumps(row)
    assert tool is not None


# ── 3. the calling app ──────────────────────────────────────────────


def test_audit_row_records_endpoint_and_oauth_client(server, harness):
    for mcp, endpoint in ((server.mcp_server, "/mcp"), (server.read_server, "/mcp/read")):
        harness.clear()
        # Run the call through the endpoint's own middleware chain is what
        # the HTTP test below does; here the contextvar is set as it sets it.
        tok = server._CALL_ENDPOINT.set(endpoint)
        try:
            _call(mcp, "whoami", {})
        finally:
            server._CALL_ENDPOINT.reset(tok)
        client = _audit_rows(harness)[-1]["client"]
        assert client.startswith(server.SERVER_VERSION + ";"), client
        assert f";endpoint={endpoint}" in client
        assert ";oauth_client=c1a7c1a7-0000-4000-8000-000000000141" in client


def test_client_label_sanitizes_self_asserted_app_names(server):
    tok = server._CALL_APP.set({"name": server._clean_ident("Evil;app=x/../../etc"),
                               "version": "1"})
    try:
        label = server.audit_client_label()
    finally:
        server._CALL_APP.reset(tok)
    assert label.count(";app=") == 1
    assert "../" not in label or "Evil_app_x" in label
    assert "Evil_app_x" in label


def test_app_identity_from_envelope_and_user_agent(server):
    from types import SimpleNamespace

    ctx = SimpleNamespace(
        session=SimpleNamespace(client_params=None),
        meta={"io.modelcontextprotocol/clientInfo": {"name": "openai-mcp", "version": "1.0.0"}},
        request=SimpleNamespace(headers={"user-agent": "openai-mcp/1.0.0 (ChatGPT)"}),
    )
    app = server._app_from_context(ctx)
    assert app["name"] == "openai-mcp" and app["version"] == "1.0.0"
    assert app["ua"].startswith("openai-mcp/1.0.0")


# ── the served app: two doors, one port ─────────────────────────────


@pytest.fixture
def http(server, monkeypatch):
    from mcp.server.auth.provider import AccessToken
    from starlette.testclient import TestClient

    log: list = []
    monkeypatch.setattr(server, "user_client", lambda: RecordingClient(log))

    async def verify(token):
        if token != "good":
            return None
        return AccessToken(
            token=token, client_id="u-141", scopes=[], expires_at=None,
            subject="00000000-0000-4000-8000-000000000141",
            claims={"sub": "00000000-0000-4000-8000-000000000141",
                    "client_id": "test-oauth-client"},
        )

    monkeypatch.setattr(server.mcp_server._token_verifier, "verify_token", verify)
    # HOST is 127.0.0.1 here, so the SDK's DNS-rebinding guard is on (as in a
    # local run); a request must name an allowed Host.
    with TestClient(server.build_app(), base_url="http://127.0.0.1:8788") as client:
        yield client, log


def _rpc(client, path, method, params=None, token="good", ua="openai-mcp/1.0.0"):
    from mcp_types import (
        CLIENT_CAPABILITIES_META_KEY,
        CLIENT_INFO_META_KEY,
        LATEST_PROTOCOL_VERSION,
        PROTOCOL_VERSION_META_KEY,
    )

    params = dict(params or {})
    params["_meta"] = {
        PROTOCOL_VERSION_META_KEY: LATEST_PROTOCOL_VERSION,
        CLIENT_CAPABILITIES_META_KEY: {},
        CLIENT_INFO_META_KEY: {"name": "openai-mcp", "version": "1.0.0"},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
        "Mcp-Method": method,
        "User-Agent": ua,
    }
    if method == "tools/call":
        headers["Mcp-Name"] = params["name"]
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(path, json={"jsonrpc": "2.0", "id": 1, "method": method,
                                   "params": params}, headers=headers)


def test_both_endpoints_challenge_with_their_own_metadata(server, http):
    client, _ = http
    for path in ("/mcp", "/mcp/read"):
        resp = _rpc(client, path, "tools/list", token=None)
        assert resp.status_code == 401
        assert (f'resource_metadata="{server.RESOURCE_URL.rsplit("/mcp", 1)[0]}'
                f'/.well-known/oauth-protected-resource{path}"'
                ) in resp.headers["www-authenticate"]
    doc = client.get("/.well-known/oauth-protected-resource/mcp/read").json()
    assert doc["resource"] == server.READ_RESOURCE_URL
    assert doc["authorization_servers"] == [server.ISSUER]
    main_doc = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert main_doc["resource"].rstrip("/") == server.RESOURCE_URL.rstrip("/")


def test_read_path_serves_only_read_tools_over_http(server, http):
    client, _ = http
    read = _rpc(client, "/mcp/read", "tools/list").json()["result"]["tools"]
    main = _rpc(client, "/mcp", "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in read} == set(server.READ_ONLY_TOOLS)
    assert {t["name"] for t in main} == set(server.READ_ONLY_TOOLS) | set(server.MAIN_ONLY_TOOLS)
    resp = _rpc(client, "/mcp/read", "tools/call",
                {"name": "create_note", "arguments": {"project_code": "x", "body": "y"}})
    body = resp.json()
    assert "error" in body or body["result"].get("isError"), body


def test_http_call_records_endpoint_app_and_oauth_client(server, http):
    client, log = http
    for path in ("/mcp/read", "/mcp"):
        log.clear()
        resp = _rpc(client, path, "tools/call", {"name": "whoami", "arguments": {}})
        assert resp.status_code == 200, resp.text
        payload = resp.json()["result"]
        rows = _audit_rows(log)
        assert len(rows) == 1, rows
        label = rows[0]["client"]
        assert f";endpoint={path}" in label, label
        assert ";oauth_client=test-oauth-client" in label
        assert ";app=openai-mcp/1.0.0" in label
        # whoami shows the caller what the auditor sees.
        text = json.dumps(payload)
        assert path in text and "test-oauth-client" in text


def test_health_counts_the_read_endpoint(server, http):
    client, _ = http
    body = client.get("/health").json()
    assert body["read_endpoint"] == {"path": "/mcp/read",
                                     "tool_count": len(server.READ_ONLY_TOOLS)}


def test_listed_read_only_client_is_refused_on_the_full_endpoint(server, http, monkeypatch):
    """Optional hardening: a token minted for a READ_ONLY_OAUTH_CLIENT_IDS
    client works on /mcp/read and is refused (and audited) on /mcp."""
    client, log = http
    monkeypatch.setattr(server, "READ_ONLY_OAUTH_CLIENT_IDS", frozenset({"test-oauth-client"}))
    log.clear()
    resp = _rpc(client, "/mcp", "tools/call", {"name": "whoami", "arguments": {}})
    body = resp.json()
    assert "error" in body and "read-only" in body["error"]["message"], body
    rows = _audit_rows(log)
    assert [r["tool"] for r in rows] == ["refused_read_only_client"]
    assert ";endpoint=/mcp;" in rows[0]["client"]
    ok = _rpc(client, "/mcp/read", "tools/call", {"name": "whoami", "arguments": {}})
    assert "result" in ok.json(), ok.text


def test_empty_read_only_client_list_leaves_the_full_endpoint_open(server, http, monkeypatch):
    """The control for the test above: default config changes nothing on /mcp."""
    client, _ = http
    monkeypatch.setattr(server, "READ_ONLY_OAUTH_CLIENT_IDS", frozenset())
    resp = _rpc(client, "/mcp", "tools/call", {"name": "whoami", "arguments": {}})
    assert "result" in resp.json(), resp.text
