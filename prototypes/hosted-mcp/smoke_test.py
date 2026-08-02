#!/usr/bin/env python
"""Smoke test for the hosted-cp OAuth spike (cp-engine #137).

Exercises a RUNNING server over plain HTTP (no MCP client library — the point
is to see the wire behaviour, including the 401 headers an SDK would swallow).

    # terminal 1
    SUPABASE_URL=... SUPABASE_ANON_KEY=... ALLOW_HS256=1 SUPABASE_JWT_SECRET=... \
        .venv/bin/python prototypes/hosted-mcp/server.py

    # terminal 2
    TEST_JWT="$(cat /path/to/jwt)" PROJECT_CODE=ibx-5153 \
        .venv/bin/python prototypes/hosted-mcp/smoke_test.py

The token is read from $TEST_JWT, or from the file at $TEST_JWT_FILE.

Cases:
  A. no token          -> 401 + WWW-Authenticate carrying resource_metadata=
  B. garbage token      -> 401
  C. wrong-alg token    -> 401   (the only way the ES256 path is exercised today)
  D. RFC 9728 metadata  -> 200, names Supabase as the authorization server
  E. tools/list         -> the four tools, with a valid token
  F. list_spine_elements-> rows, under the caller's identity
  G. list_commitments   -> the RLS deny-all sentinel

NOTE ON ES256: this project still signs session tokens with HS256 (legacy
shared secret; the ES256 JWKS key is in standby). So the valid-token cases run
against an HS256 token with the server's interim fallback enabled, and the
ES256 path is verified only NEGATIVELY — case C proves an unexpected algorithm
is rejected rather than confused into another key. Once the ES256 key is
promoted to current, re-run this unchanged with ALLOW_HS256 unset: cases E–G
should pass identically against a real ES256 token.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

BASE = os.environ.get("SERVER_BASE", "http://127.0.0.1:8788").rstrip("/")
MCP_URL = f"{BASE}/mcp"
PROJECT_CODE = os.environ.get("PROJECT_CODE", "ibx-5153")
PROTOCOL_VERSION = "2026-07-28"

# A syntactically valid JWT signed with the WRONG algorithm family for the
# primary path (alg=none), used for case C.
WRONG_ALG_TOKEN = (
    "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0."
    "eyJzdWIiOiJhdHRhY2tlciIsImlzcyI6Imh0dHBzOi8vbWdoZXltc2xrc2Z5aHV2aG12bWou"
    "c3VwYWJhc2UuY28vYXV0aC92MSIsImV4cCI6NDEwMjQ0NDgwMH0."
)

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"[{PASS if ok else FAIL}] {name}\n       {detail}\n")


def load_token() -> str | None:
    token = os.environ.get("TEST_JWT", "").strip()
    if token:
        return token
    path = os.environ.get("TEST_JWT_FILE", "").strip()
    if path and os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    return None


# 2026-07-28 per-request envelope keys (mcp_types._types).
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
# Methods that must mirror a body param into the Mcp-Name header.
NAME_BEARING_METHODS = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}


def rpc(method: str, params: dict[str, Any] | None, token: str | None, rid: int = 1) -> httpx.Response:
    """One JSON-RPC call over streamable HTTP, 2026-07-28 initialize-less form.

    No `initialize` handshake is needed: each request is self-contained. But
    "self-contained" is a real contract, and the SDK enforces it as a ladder —

      * `params._meta` MUST carry both the protocol version and the client
        capabilities (the negotiation an `initialize` would otherwise have
        done, folded into every request);
      * the `MCP-Protocol-Version` header MUST equal the envelope's version,
        and `Mcp-Method` MUST equal the body's method — a client that
        disagrees with itself is rejected before version support is even
        considered;
      * for name-bearing methods (`tools/call`), `Mcp-Name` MUST mirror the
        named body param, so a proxy can route on headers alone.

    Accept must name BOTH json and event-stream — the transport rejects a
    request that cannot receive either shape.
    """
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
    params = dict(params or {})
    params["_meta"] = {
        PROTOCOL_VERSION_META_KEY: PROTOCOL_VERSION,
        CLIENT_CAPABILITIES_META_KEY: {},
        CLIENT_INFO_META_KEY: {"name": "hosted-cp-smoke-test", "version": "0.0.1"},
    }
    body["params"] = params

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    name_key = NAME_BEARING_METHODS.get(method)
    if name_key and params.get(name_key) is not None:
        headers["Mcp-Name"] = str(params[name_key])
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.post(MCP_URL, json=body, headers=headers, timeout=45)


def parse_rpc(resp: httpx.Response) -> dict[str, Any]:
    """Decode a JSON-RPC reply from either a JSON body or an SSE frame."""
    text = resp.text
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise ValueError(f"no data frame in SSE response: {text[:300]}")
    return json.loads(text)


def tool_payload(resp: httpx.Response) -> dict[str, Any]:
    """Pull a tool's structured result out of a tools/call reply."""
    msg = parse_rpc(resp)
    if "error" in msg:
        raise ValueError(f"JSON-RPC error: {msg['error']}")
    result = msg.get("result", {})
    if result.get("structuredContent"):
        return result["structuredContent"]
    for block in result.get("content", []):
        if block.get("type") == "text":
            try:
                return json.loads(block["text"])
            except json.JSONDecodeError:
                return {"text": block["text"]}
    return result


# ──────────────────────────────────────────────────────────────────────
#  Cases
# ──────────────────────────────────────────────────────────────────────


def case_a_no_token() -> None:
    resp = rpc("tools/list", {}, token=None)
    www = resp.headers.get("www-authenticate", "")
    ok = resp.status_code == 401 and "resource_metadata=" in www
    record(
        "A. no token -> 401 + WWW-Authenticate w/ resource_metadata",
        ok,
        f"status={resp.status_code} www-authenticate={www or '(absent)'}",
    )


def case_b_garbage_token() -> None:
    resp = rpc("tools/list", {}, token="not-a-jwt-at-all")
    ok = resp.status_code == 401
    record("B. garbage token -> 401", ok, f"status={resp.status_code}")


def case_c_wrong_alg() -> None:
    resp = rpc("tools/list", {}, token=WRONG_ALG_TOKEN)
    ok = resp.status_code == 401
    record(
        "C. alg=none token -> 401 (negative test of the ES256 path)",
        ok,
        f"status={resp.status_code} — an unexpected alg is rejected, never "
        f"confused into another key",
    )


def case_d_resource_metadata() -> None:
    url = f"{BASE}/.well-known/oauth-protected-resource/mcp"
    resp = httpx.get(url, timeout=20)
    try:
        doc = resp.json()
    except Exception:
        doc = {}
    servers = doc.get("authorization_servers", [])
    ok = resp.status_code == 200 and bool(servers)
    record(
        "D. RFC 9728 protected-resource metadata",
        ok,
        f"status={resp.status_code} resource={doc.get('resource')} "
        f"authorization_servers={servers}",
    )


def case_e_tools_list(token: str) -> None:
    resp = rpc("tools/list", {}, token=token)
    if resp.status_code != 200:
        record("E. tools/list with valid token", False, f"status={resp.status_code} body={resp.text[:300]}")
        return
    msg = parse_rpc(resp)
    names = sorted(t["name"] for t in msg.get("result", {}).get("tools", []))
    ok = "list_spine_elements" in names
    record("E. tools/list with valid token", ok, f"tools={names}")


def case_f_spine(token: str) -> None:
    resp = rpc("tools/call", {"name": "list_spine_elements", "arguments": {"project_code": PROJECT_CODE}}, token=token)
    if resp.status_code != 200:
        record("F. list_spine_elements returns rows", False, f"status={resp.status_code} body={resp.text[:300]}")
        return
    try:
        payload = tool_payload(resp)
    except ValueError as exc:
        record("F. list_spine_elements returns rows", False, str(exc))
        return
    count = payload.get("count", 0)
    if payload.get("error"):
        record("F. list_spine_elements returns rows", False, f"tool error: {payload['error']}")
        return
    sample = [e.get("slug") for e in payload.get("elements", [])[:3]]
    record(
        f"F. list_spine_elements({PROJECT_CODE}) returns rows under caller identity",
        count > 0,
        f"count={count} caller={payload.get('caller')} sample={sample}",
    )


def case_g_commitments(token: str) -> None:
    resp = rpc("tools/call", {"name": "list_commitments", "arguments": {"project_code": PROJECT_CODE}}, token=token)
    if resp.status_code != 200:
        record("G. list_commitments shows RLS sentinel", False, f"status={resp.status_code} body={resp.text[:300]}")
        return
    try:
        payload = tool_payload(resp)
    except ValueError as exc:
        record("G. list_commitments shows RLS sentinel", False, str(exc))
        return
    note = payload.get("rls_note", "")
    ok = payload.get("count", 0) == 0 and "RLS denies access" in note
    record(
        "G. list_commitments surfaces the deny-all sentinel",
        ok,
        f"count={payload.get('count')} rls_note={note[:110]}...",
    )


def case_h_whoami(token: str) -> None:
    resp = rpc("tools/call", {"name": "whoami", "arguments": {}}, token=token)
    if resp.status_code != 200:
        record("H. whoami echoes verified identity", False, f"status={resp.status_code}")
        return
    payload = tool_payload(resp)
    ok = bool(payload.get("authenticated")) and bool(payload.get("sub"))
    sub = str(payload.get("sub") or "")
    record(
        "H. whoami echoes verified identity",
        ok,
        f"authenticated={payload.get('authenticated')} sub={sub[:8]}... "
        f"role={payload.get('role')}",
    )


def main() -> int:
    print(f"hosted-cp spike smoke test -> {MCP_URL}\n")
    try:
        httpx.get(f"{BASE}/.well-known/oauth-protected-resource/mcp", timeout=5)
    except httpx.ConnectError:
        print(f"ERROR: nothing listening at {BASE}. Start server.py first.")
        return 2

    case_a_no_token()
    case_b_garbage_token()
    case_c_wrong_alg()
    case_d_resource_metadata()

    token = load_token()
    if not token:
        print("SKIPPED: cases E-H need a real user JWT in $TEST_JWT or $TEST_JWT_FILE.\n")
    else:
        case_e_tools_list(token)
        case_f_spine(token)
        case_g_commitments(token)
        case_h_whoami(token)

    failed = [r for r in results if r[0] == FAIL]
    print("─" * 68)
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    for status, name, _ in results:
        print(f"  {status}  {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
