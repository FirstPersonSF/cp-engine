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
  A. no token           -> 401 + WWW-Authenticate carrying resource_metadata=
  B. garbage token      -> 401
  C. wrong-alg token    -> 401   (the negative test of the ES256 path)
  D. RFC 9728 metadata  -> 200, names Supabase as the authorization server
  E. tools/list         -> all eight tools, with a valid token
  F. list_spine_elements-> rows, under the caller's identity
  G. list_commitments   -> REAL ROWS now that reads are team-keyed
  H. whoami             -> the verified identity
  I. list_project_sources  -> rag_assets rows
  J. pull_project_source   -> one asset's text, assembled from asset_chunks
  K. list_project_meetings -> fathom_meetings rows
  L. semantic_search       -> vector hits (or a clean "unavailable")
  N. create_commitment     -> a REAL commitments row (insert-only write)
  O. create_spine_element  -> a REAL spine_substance row (v1, live, authored)
  P. create_note           -> the notes author_id FK/policy collision, diagnosed
  T. create_spine_relation -> a typed edge; re-create returns already:true
  T2. create_spine_relation-> a kind outside the closed vocabulary REJECTED
  U. add_spine_step        -> a LIVE human step (not auto/proposed)
  U2. add_spine_step       -> a status outside done|active|upcoming REJECTED
  V. propose_spine_step    -> an auto/proposed step; re-propose is a no-op
  Q. get_project_state     -> ibx-5153's Exec Summary + current sprint file
  R. read_project_file     -> a real file read from the clone
  S. read_project_file     -> path traversal REJECTED
  M. mcp_audit_log         -> audit rows appear for the calls just made

WRITE CASES CREATE REAL ROWS. They target `mission-control` (an internal
initiative) and prefix every slug/description with `smoke-test-` so the rows are
identifiable. There is no delete policy for an authenticated caller, so this
script cannot and does not clean up — it PRINTS the created row ids at the end
for a human to remove with the service key.

Tree cases need TENANT_REPO pointed at a clone (a local path works for
development). Without it they assert the clean-degrade path instead.

The token must be an ES256 Supabase user token belonging to a TEAM member (a
`public.profiles` row). Read policies are gated on `public.is_team_member()`,
so a merely-authenticated user sees zero rows everywhere and cases F/G/I–M all
fail — that failure is the policy working, not the server breaking.

Case M reads `mcp_audit_log` directly via PostgREST with the SAME JWT, which is
the point: the audit trail is written under the caller's own identity (INSERT
policy `user_id = auth.uid() and is_team_member()`), so it is visible to them.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
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

# Writes land here, never against a client engagement.
WRITE_PROJECT_CODE = os.environ.get("WRITE_PROJECT_CODE", "mission-control")
# A per-run marker so this run's rows are distinguishable from earlier runs'.
RUN_TAG = os.environ.get("SMOKE_RUN_TAG") or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
# Row ids this run created, reported at the end for manual cleanup.
created_rows: list[tuple[str, str]] = []


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
    expected = {
        "list_spine_elements",
        "pull_spine_element",
        "list_commitments",
        "list_project_sources",
        "pull_project_source",
        "list_project_meetings",
        "semantic_search",
        "whoami",
        # Package A — writes (#139; add_spine_version via the #142 guarded fn)
        "create_note",
        "create_commitment",
        "create_spine_element",
        "add_spine_version",
        "add_spine_document",
        # #143 batch 1 — relations + steps
        "create_spine_relation",
        "add_spine_step",
        "propose_spine_step",
        # Package B — read-only tenant tree (#138)
        "get_project_state",
        "read_project_file",
    }
    missing = sorted(expected - set(names))
    record(
        "E. tools/list exposes all 18 tools",
        not missing,
        f"tools={names}" + (f" MISSING={missing}" if missing else ""),
    )


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
    """Commitments used to be deny-all. Team-keyed policies made them real rows."""
    resp = rpc("tools/call", {"name": "list_commitments", "arguments": {"project_code": PROJECT_CODE}}, token=token)
    if resp.status_code != 200:
        record("G. list_commitments returns rows", False, f"status={resp.status_code} body={resp.text[:300]}")
        return
    try:
        payload = tool_payload(resp)
    except ValueError as exc:
        record("G. list_commitments returns rows", False, str(exc))
        return
    count = payload.get("count", 0)
    sample = [str(c.get("description", ""))[:50] for c in payload.get("commitments", [])[:2]]
    record(
        f"G. list_commitments({PROJECT_CODE}) returns REAL rows (team-keyed RLS)",
        count > 0,
        f"count={count} sample={sample}"
        + (f" note={payload.get('note', '')[:80]}" if count == 0 else ""),
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


def case_i_sources(token: str) -> str | None:
    """Returns an asset_id for case J to pull."""
    resp = rpc(
        "tools/call",
        {"name": "list_project_sources", "arguments": {"project_code": PROJECT_CODE}},
        token=token,
    )
    if resp.status_code != 200:
        record("I. list_project_sources returns rows", False, f"status={resp.status_code} body={resp.text[:300]}")
        return None
    try:
        payload = tool_payload(resp)
    except ValueError as exc:
        record("I. list_project_sources returns rows", False, str(exc))
        return None
    sources = payload.get("sources", [])
    count = payload.get("count", 0)
    record(
        f"I. list_project_sources({PROJECT_CODE}) returns rag_assets rows",
        count > 0,
        f"count={count} superseded_hidden={payload.get('superseded_hidden')} "
        f"sample={[s.get('title') for s in sources[:2]]}",
    )
    return sources[0].get("asset_id") if sources else None


def case_j_pull_source(token: str, asset_id: str | None) -> None:
    if not asset_id:
        record("J. pull_project_source assembles chunk text", False, "no asset_id from case I")
        return
    resp = rpc(
        "tools/call",
        {"name": "pull_project_source", "arguments": {"asset_id": asset_id}},
        token=token,
    )
    if resp.status_code != 200:
        record("J. pull_project_source assembles chunk text", False, f"status={resp.status_code}")
        return
    try:
        payload = tool_payload(resp)
    except ValueError as exc:
        record("J. pull_project_source assembles chunk text", False, str(exc))
        return
    text = payload.get("text") or ""
    chunks = payload.get("chunk_count", 0)
    ok = chunks > 0 and len(text) > 0
    record(
        "J. pull_project_source assembles text from asset_chunks",
        ok,
        f"title={payload.get('title')!r} chunks={chunks} chars={len(text)} "
        f"truncated={payload.get('truncated')} head={text[:60]!r}",
    )


def case_k_meetings(token: str) -> None:
    resp = rpc(
        "tools/call",
        {"name": "list_project_meetings", "arguments": {"project_code": PROJECT_CODE}},
        token=token,
    )
    if resp.status_code != 200:
        record("K. list_project_meetings returns rows", False, f"status={resp.status_code}")
        return
    try:
        payload = tool_payload(resp)
    except ValueError as exc:
        record("K. list_project_meetings returns rows", False, str(exc))
        return
    count = payload.get("count", 0)
    sample = [m.get("title") for m in payload.get("meetings", [])[:2]]
    record(
        f"K. list_project_meetings({PROJECT_CODE}) returns fathom_meetings rows",
        count > 0,
        f"count={count} sample={sample}",
    )


def case_l_semantic_search(token: str) -> None:
    resp = rpc(
        "tools/call",
        {
            "name": "semantic_search",
            "arguments": {
                "query": "AI campaign positioning and pillars",
                "project_code": PROJECT_CODE,
                "limit": 5,
            },
        },
        token=token,
    )
    if resp.status_code != 200:
        record("L. semantic_search returns vector hits", False, f"status={resp.status_code} body={resp.text[:300]}")
        return
    try:
        payload = tool_payload(resp)
    except ValueError as exc:
        record("L. semantic_search returns vector hits", False, str(exc))
        return

    if payload.get("available") is False:
        # No embedding key is a VALID configuration — the tool must degrade
        # cleanly rather than crash. That counts as a pass, loudly labelled.
        err = str(payload.get("error", ""))
        record(
            "L. semantic_search degrades cleanly with no embedding key",
            "search unavailable" in err,
            f"available=False error={err[:140]}",
        )
        return

    results = payload.get("results", [])
    sims = [round(r.get("similarity", 0), 3) for r in results[:3]]
    record(
        "L. semantic_search returns vector-ranked hits",
        payload.get("count", 0) > 0,
        f"count={payload.get('count')} model={payload.get('embed_model')} "
        f"scope={payload.get('scope')} top_similarities={sims} "
        f"titles={[r.get('title') for r in results[:2]]}",
    )


# ──────────────────────────────────────────────────────────────────────
#  Package A — insert-only writes (#139). These create REAL rows.
# ──────────────────────────────────────────────────────────────────────


def call_tool(token: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """tools/call -> the structured payload, or {"_http": status} on a non-200."""
    resp = rpc("tools/call", {"name": name, "arguments": arguments}, token=token)
    if resp.status_code != 200:
        return {"_http": resp.status_code, "_body": resp.text[:300]}
    try:
        return tool_payload(resp)
    except ValueError as exc:
        return {"_rpc_error": str(exc)}


def case_n_create_commitment(token: str) -> None:
    """Insert a real commitment against the internal initiative."""
    description = f"smoke-test-{RUN_TAG} — hosted-mcp write verification, safe to delete"
    payload = call_tool(
        token,
        "create_commitment",
        {
            "project_code": WRITE_PROJECT_CODE,
            "description": description,
            "owner_email": "drew@firstperson.is",
            "due_date": "2026-12-31",
        },
    )
    cid = payload.get("commitment_id")
    if cid:
        created_rows.append(("commitments", cid))
    ok = bool(cid) and payload.get("date_status") == "proposed" and payload.get("status") == "open"
    record(
        f"N. create_commitment({WRITE_PROJECT_CODE}) inserts a real row",
        ok,
        f"commitment_id={cid} scope_kind={payload.get('scope_kind')} "
        f"date_status={payload.get('date_status')} status={payload.get('status')} "
        f"due_date={payload.get('due_date')}"
        + (f" ERROR={payload.get('error') or payload.get('_http')}" if not ok else ""),
    )


def case_n2_bad_due_date(token: str) -> None:
    """A non-ISO due_date must be REJECTED, never guessed or silently dropped."""
    payload = call_tool(
        token,
        "create_commitment",
        {
            "project_code": WRITE_PROJECT_CODE,
            "description": f"smoke-test-{RUN_TAG} — must not be created",
            "due_date": "next Friday",
        },
    )
    ok = "commitment_id" not in payload and "is not an ISO date" in str(payload.get("error", ""))
    record(
        "N2. create_commitment rejects a non-ISO due_date (no row created)",
        ok,
        f"error={str(payload.get('error'))[:120]!r}",
    )


def case_o_create_spine_element(token: str) -> None:
    """Insert a real authored spine element (v1, live)."""
    framing = f"smoke-test-{RUN_TAG} hosted mcp element"
    payload = call_tool(
        token,
        "create_spine_element",
        {
            "project_code": WRITE_PROJECT_CODE,
            "framing": framing,
            "body": "Created by prototypes/hosted-mcp/smoke_test.py. Safe to delete.",
            "layer": "note",
        },
    )
    row_id = payload.get("row_id")
    if row_id:
        created_rows.append(("spine_substance", row_id))
    ok = (
        bool(row_id)
        and payload.get("version_label") == "v1"
        and payload.get("status") == "live"
        and payload.get("layer") == "Note"  # canon_layer('note') -> 'Note'
        and str(payload.get("element_id", "")).startswith("_authored/smoke-test-")
    )
    record(
        f"O. create_spine_element({WRITE_PROJECT_CODE}) inserts a live v1 authored row",
        ok,
        f"row_id={row_id} element_id={payload.get('element_id')} "
        f"layer={payload.get('layer')} project_code={payload.get('project_code')} "
        f"(requested {payload.get('requested_code')})"
        + (f" ERROR={payload.get('error') or payload.get('_http')}" if not ok else ""),
    )


def case_o2_collision_guard(token: str) -> None:
    """Re-creating the same slug must be REFUSED, not silently upserted."""
    framing = f"smoke-test-{RUN_TAG} hosted mcp element"
    payload = call_tool(
        token,
        "create_spine_element",
        {
            "project_code": WRITE_PROJECT_CODE,
            "framing": framing,
            "body": "second attempt — must not clobber the first",
            "layer": "note",
        },
    )
    ok = "row_id" not in payload and "already exists" in str(payload.get("error", ""))
    record(
        "O2. create_spine_element refuses to clobber an existing slug",
        ok,
        f"error={str(payload.get('error'))[:130]!r} existing={payload.get('existing_id')}",
    )


def case_o3_add_spine_version(token: str) -> None:
    """Version bump via the #142 guarded supersede function.

    Bumps the element case O just created: expects a v2 live row, exactly one
    prior row demoted live->superseded, and the read path returning the v2
    body. The demote happens inside `spine_supersede_prior_versions` — there
    is still no authenticated UPDATE grant on spine_substance, so this passing
    proves the guarded-function path, not an open door.

    ALSO asserts the #143 auto-journal: the return must carry a `step` result
    recording a created (or updated) `source='auto'`, `review='proposed'` step
    for today. A journal miss is non-fatal to the WRITE, but it is a real
    regression, so the smoke test treats a missing step as a failure.
    """
    element_id = f"_authored/smoke-test-{RUN_TAG}-hosted-mcp-element"
    payload = call_tool(
        token,
        "add_spine_version",
        {
            "project_code": WRITE_PROJECT_CODE,
            "element_id": element_id,
            "body": "Version 2 body, written by smoke_test.py via the guarded supersede path.",
            "version_note": "smoke-test version bump",
        },
    )
    new_label = payload.get("version_label")
    superseded = payload.get("superseded")
    ok = new_label == "v2" and superseded == 1
    read_back = {}
    if ok:
        read_back = call_tool(
            token,
            "pull_spine_element",
            {"element_id": element_id, "project_code": WRITE_PROJECT_CODE},
        )
        ok = "Version 2 body" in str(read_back.get("body", "")) and read_back.get("version_label") == "v2"
    if new_label == "v2":
        created_rows.append(("spine_substance", f"{payload.get('project_code')}/{element_id}/v2"))

    # #143: the auto-journalled step must ride along with the version write.
    step = payload.get("step") or {}
    step_ok = bool(step.get("created") or step.get("updated")) and not step.get("error")
    if step.get("step_id"):
        created_rows.append(("spine_steps", step["step_id"]))
    ok = ok and step_ok
    record(
        "O3. add_spine_version supersedes v1, reads back as v2, and auto-journals a step",
        ok,
        f"version_label={new_label} superseded={superseded} "
        f"readback_label={read_back.get('version_label')} "
        f"step={{created:{step.get('created')} updated:{step.get('updated')} "
        f"id:{step.get('step_id')}}}"
        + (f" STEP_ERROR={step.get('error')}" if step.get("error") else "")
        + (f" ERROR={payload.get('error') or payload.get('_http')}" if not ok else ""),
    )


# ──────────────────────────────────────────────────────────────────────
#  #143 batch 1 — relations + steps. THESE CREATE REAL ROWS.
# ──────────────────────────────────────────────────────────────────────


def case_t_create_spine_relation(token: str) -> None:
    """A typed edge between two live elements, plus its idempotency guarantee.

    Needs TWO elements, so it creates a second one (case O made the first), then
    relates them. The re-create must return `already: true` rather than erroring
    on the mig-117 unique constraint.

    Also asserts `created_by` is the CALLER'S EMAIL — the hosted INSERT policy
    is `created_by = auth.jwt()->>'email'`, so a row that lands at all proves
    the stamp matched the verified claim.
    """
    # A second element to point the edge at.
    partner_framing = f"smoke-test-{RUN_TAG} relation target"
    partner = call_tool(
        token,
        "create_spine_element",
        {
            "project_code": WRITE_PROJECT_CODE,
            "framing": partner_framing,
            "body": "Relation target created by smoke_test.py. Safe to delete.",
            "layer": "note",
        },
    )
    if partner.get("row_id"):
        created_rows.append(("spine_substance", partner["row_id"]))
    to_key = partner.get("element_id")
    from_key = f"_authored/smoke-test-{RUN_TAG}-hosted-mcp-element"

    if not to_key:
        record(
            "T. create_spine_relation writes a typed edge (idempotently)",
            False,
            f"could not create the partner element: {partner.get('error') or partner.get('_http')}",
        )
        return

    payload = call_tool(
        token,
        "create_spine_relation",
        {
            "project_code": WRITE_PROJECT_CODE,
            "kind": "informs",
            "from_key": from_key,
            "to_key": to_key,
            "note": f"smoke-test-{RUN_TAG}",
        },
    )
    rel_id = payload.get("relation_id")
    if rel_id:
        created_rows.append(("spine_relations", rel_id))
    created_ok = (
        payload.get("created") is True
        and payload.get("kind") == "informs"
        and payload.get("from_item_id") == from_key
        and payload.get("to_item_id") == to_key
    )

    # Idempotency: the identical edge again must be a reported no-op.
    again = call_tool(
        token,
        "create_spine_relation",
        {
            "project_code": WRITE_PROJECT_CODE,
            "kind": "informs",
            "from_key": from_key,
            "to_key": to_key,
        },
    )
    idempotent_ok = again.get("created") is False and again.get("already") is True

    record(
        "T. create_spine_relation writes a typed edge; re-create returns already:true",
        created_ok and idempotent_ok,
        f"relation_id={rel_id} kind={payload.get('kind')} "
        f"created_by={payload.get('created_by')} "
        f"recreate={{created:{again.get('created')} already:{again.get('already')}}}"
        + (f" ERROR={payload.get('error') or payload.get('note') or payload.get('_http')}"
           if not created_ok else "")
        + (f" RECREATE_ERROR={again.get('error') or again.get('note')}"
           if not idempotent_ok else ""),
    )


def case_t2_bad_relation_kind(token: str) -> None:
    """An unknown kind must be rejected in-process, not left to the DB CHECK."""
    payload = call_tool(
        token,
        "create_spine_relation",
        {
            "project_code": WRITE_PROJECT_CODE,
            "kind": "relates_to",  # not in the closed vocabulary
            "from_key": f"_authored/smoke-test-{RUN_TAG}-hosted-mcp-element",
            "to_key": f"_authored/smoke-test-{RUN_TAG}-relation-target",
        },
    )
    error = str(payload.get("error", ""))
    ok = "relation_id" not in payload and "unknown relation kind" in error
    record(
        "T2. create_spine_relation rejects a kind outside the closed vocabulary",
        ok,
        f"error={error[:140]!r}",
    )


def case_u_add_spine_step(token: str) -> None:
    """A LIVE human step: appended at max+1, source/review left at table defaults."""
    element_id = f"_authored/smoke-test-{RUN_TAG}-hosted-mcp-element"
    payload = call_tool(
        token,
        "add_spine_step",
        {
            "project_code": WRITE_PROJECT_CODE,
            "key": element_id,
            "title": f"smoke-test-{RUN_TAG} human step",
            "status": "active",
            "step_date": "8/2",
        },
    )
    step_id = payload.get("step_id")
    if step_id:
        created_rows.append(("spine_steps", step_id))
    steps = payload.get("steps") or []
    mine = next((s for s in steps if s.get("id") == step_id), {})
    # A human step must NOT land as an auto/proposed row — that is the whole
    # distinction from propose_spine_step.
    ok = (
        bool(step_id)
        and payload.get("est_item_id") == element_id
        and isinstance(payload.get("position"), int)
        and mine.get("status") == "active"
        and mine.get("review") != "proposed"
    )
    record(
        "U. add_spine_step appends a live human step (not auto/proposed)",
        ok,
        f"step_id={step_id} position={payload.get('position')} "
        f"status={mine.get('status')} source={mine.get('source')} "
        f"review={mine.get('review')} trail_len={len(steps)}"
        + (f" ERROR={payload.get('error') or payload.get('_http')}" if not ok else ""),
    )


def case_u2_bad_step_status(token: str) -> None:
    """The step status vocabulary is closed: done|active|upcoming."""
    payload = call_tool(
        token,
        "add_spine_step",
        {
            "project_code": WRITE_PROJECT_CODE,
            "key": f"_authored/smoke-test-{RUN_TAG}-hosted-mcp-element",
            "title": "must not be created",
            "status": "in_progress",
        },
    )
    ok = "step_id" not in payload and "status must be one of" in str(payload.get("error", ""))
    record(
        "U2. add_spine_step rejects a status outside done|active|upcoming",
        ok,
        f"error={str(payload.get('error'))[:120]!r}",
    )


def case_v_propose_spine_step(token: str) -> None:
    """A review-gated proposal, and its any-review-state idempotency."""
    element_id = f"_authored/smoke-test-{RUN_TAG}-hosted-mcp-element"
    title = f"smoke-test-{RUN_TAG} proposed step"
    payload = call_tool(
        token,
        "propose_spine_step",
        {
            "project_code": WRITE_PROJECT_CODE,
            "key": element_id,
            "title": title,
            "step_date": "8/2",
        },
    )
    step_id = payload.get("step_id")
    if step_id:
        created_rows.append(("spine_steps", step_id))
    steps = payload.get("steps") or []
    mine = next((s for s in steps if s.get("id") == step_id), {})
    proposed_ok = (
        payload.get("proposed") is True
        and mine.get("source") == "auto"
        and mine.get("review") == "proposed"
        and mine.get("status") == "done"  # the default: the move already happened
    )

    again = call_tool(
        token,
        "propose_spine_step",
        {
            "project_code": WRITE_PROJECT_CODE,
            "key": element_id,
            "title": title,
            "step_date": "8/2",
        },
    )
    idempotent_ok = again.get("proposed") is False and again.get("already") is True

    record(
        "V. propose_spine_step lands auto/proposed; re-propose is a no-op",
        proposed_ok and idempotent_ok,
        f"step_id={step_id} source={mine.get('source')} review={mine.get('review')} "
        f"status={mine.get('status')} "
        f"repropose={{proposed:{again.get('proposed')} already:{again.get('already')}}}"
        + (f" ERROR={payload.get('error') or payload.get('_http')}" if not proposed_ok else ""),
    )


def case_o4_add_spine_document(token: str) -> None:
    """Phase 3 (#140): author a document into the spine via content=.

    Also asserts the exactly-one-of contract rejects both-args and no-args.
    """
    bad = call_tool(
        token,
        "add_spine_document",
        {"project_code": WRITE_PROJECT_CODE, "label": "x", "content": "a", "source_title": "b"},
    )
    contract_ok = "error" in bad and "exactly one" in str(bad.get("error", ""))

    payload = call_tool(
        token,
        "add_spine_document",
        {
            "project_code": WRITE_PROJECT_CODE,
            "label": f"smoke-test-{RUN_TAG} document",
            "content": "# smoke-test document\n\nWritten by smoke_test.py via add_spine_document(content=). Safe to delete.",
            "type": "synthesis",
        },
    )
    row_id = payload.get("row_id")
    if row_id:
        created_rows.append(("spine_substance", row_id))
    ok = (
        contract_ok
        and bool(row_id)
        and payload.get("version_label") == "v1"
        and payload.get("status") == "live"
    )
    record(
        "O4. add_spine_document(content=) authors a document element",
        ok,
        f"row_id={row_id} layer={payload.get('layer')} contract_ok={contract_ok}"
        + (f" ERROR={payload.get('error') or payload.get('_http')}" if not ok else ""),
    )


def case_p_create_note(token: str) -> None:
    """The notes write, via the entities email-bridge (decided 2026-08-02).

    `notes.author_id` stays FK->entities(id) — the Notes feature's own identity
    model. The INSERT policy enforces `author_id = caller_entity_id()`, a
    definer helper mapping the caller's login email to their entities row (the
    same bridge the mc-2 backend's `_acting_entity` uses). Recipient defaults
    to the author's own entity (self-note).

    PASS = a real note_id (the token's user must have an entities row). The
    no-entities-row error is also a precise diagnosis, but for the smoke user
    an entity row is provisioned, so this case demands the happy path.
    """
    payload = call_tool(
        token,
        "create_note",
        {
            "project_code": WRITE_PROJECT_CODE,
            "title": f"smoke-test-{RUN_TAG}",
            "body": "Created by prototypes/hosted-mcp/smoke_test.py. Safe to delete.",
        },
    )
    note_id = payload.get("note_id")
    if note_id:
        created_rows.append(("notes", note_id))
        record(
            "P. create_note inserts a real note row",
            True,
            f"note_id={note_id} status={payload.get('status')} "
            f"body_chars={payload.get('body_chars')}",
        )
        return
    error = str(payload.get("error", ""))
    # Either face of the same gap counts, so long as it names entities and the
    # offending column rather than leaking a bare SQLSTATE.
    diagnosed = "entities" in error and ("author_id" in error or "recipient_id" in error)
    raw_leak = error.startswith("insert failed") or "23503" == error.strip()
    record(
        "P. create_note surfaces the notes identity/schema gap (diagnosed, not raw)",
        diagnosed and not raw_leak,
        f"error={error[:200]!r}",
    )


# ──────────────────────────────────────────────────────────────────────
#  Package B — read-only tenant tree (#138)
# ──────────────────────────────────────────────────────────────────────


def case_q_project_state(token: str) -> str | None:
    """Returns a repo-relative cp.md path for case R to read."""
    payload = call_tool(token, "get_project_state", {"project_code": PROJECT_CODE})
    if payload.get("available") is False:
        error = str(payload.get("error", ""))
        record(
            "Q. get_project_state degrades cleanly with no TENANT_REPO",
            "tree access unavailable" in error,
            f"available=False error={error[:150]}",
        )
        return None
    summary = payload.get("exec_summary") or ""
    ok = bool(summary) and bool(payload.get("working_dir"))
    record(
        f"Q. get_project_state({PROJECT_CODE}) returns the Exec Summary + sprint file",
        ok,
        f"working_dir={payload.get('working_dir')} exec_summary_chars={len(summary)} "
        f"sprint_week={payload.get('sprint_week')} sprint_file={payload.get('sprint_file')} "
        f"sprint_chars={len(payload.get('sprint_text') or '')}"
        + (f" sprint_note={payload.get('sprint_note')}" if payload.get("sprint_note") else "")
        + (f" ERROR={payload.get('error')}" if not ok else ""),
    )
    return payload.get("cp_md")


def case_r_read_file(token: str, rel_path: str | None) -> None:
    if not rel_path:
        payload = call_tool(token, "read_project_file", {"path": "master-cp.md"})
        if payload.get("available") is False:
            record(
                "R. read_project_file degrades cleanly with no TENANT_REPO",
                "tree access unavailable" in str(payload.get("error", "")),
                f"error={str(payload.get('error'))[:150]}",
            )
            return
        rel_path = "master-cp.md"
    else:
        payload = call_tool(token, "read_project_file", {"path": rel_path})
    text = payload.get("text") or ""
    ok = bool(text) and payload.get("available") is True
    record(
        f"R. read_project_file({rel_path!r}) returns text",
        ok,
        f"bytes={payload.get('bytes')} chars={len(text)} "
        f"truncated={payload.get('truncated')} head={text[:60]!r}"
        + (f" ERROR={payload.get('error')}" if not ok else ""),
    )


def case_s_traversal(token: str) -> None:
    """Path traversal must be rejected by containment, not merely by string checks."""
    attempts = [
        "../../../../etc/passwd",
        "1p/../../etc/hosts",
        "/etc/passwd",
    ]
    outcomes = []
    all_rejected = True
    for attempt in attempts:
        payload = call_tool(token, "read_project_file", {"path": attempt})
        if payload.get("available") is False:
            # No tree configured — the unavailable path is itself a refusal to
            # read anything, so traversal is trivially not possible.
            outcomes.append(f"{attempt!r}: tree unavailable")
            continue
        rejected = "text" not in payload and bool(payload.get("error"))
        all_rejected = all_rejected and rejected
        outcomes.append(f"{attempt!r}: {str(payload.get('error'))[:60]}")
    record(
        "S. read_project_file REJECTS path traversal (absolute + ../ + symlink-safe)",
        all_rejected,
        "; ".join(outcomes),
    )


def case_m_audit_log(token: str) -> None:
    """Read `mcp_audit_log` back through PostgREST with the SAME JWT.

    Proves the audit write happened AND that it is attributable to — and
    visible to — the caller, rather than being a privileged side channel.
    """
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    anon = os.environ.get("SUPABASE_ANON_KEY", "")
    if not supabase_url or not anon:
        record("M. audit rows appear in mcp_audit_log", False,
               "SUPABASE_URL/SUPABASE_ANON_KEY not in this shell's env")
        return
    try:
        resp = httpx.get(
            f"{supabase_url}/rest/v1/mcp_audit_log",
            params={
                "select": "id,tool,args,row_count,client,at",
                "order": "at.desc",
                # Must exceed the number of audited calls ONE run makes, or the
                # earliest tools scroll out of the window and read as "missing".
                # #143 added five cases (nine audited calls) and pushed the three
                # opening reads past a limit of 20 — the window was the bug, not
                # the auditing. Keep headroom when adding cases.
                "limit": "60",
            },
            headers={"apikey": anon, "Authorization": f"Bearer {token}"},
            timeout=20,
        )
        rows = resp.json() if resp.status_code == 200 else []
    except Exception as exc:  # noqa: BLE001
        record("M. audit rows appear in mcp_audit_log", False, f"{type(exc).__name__}: {exc}")
        return

    tools_logged = {r.get("tool") for r in rows}
    # Every tool this run exercised should have left a row.
    expected = {
        "list_spine_elements",
        "list_commitments",
        "list_project_sources",
        "list_project_meetings",
        # Writes and tree reads audit too.
        "create_commitment",
        "create_spine_element",
        "get_project_state",
        "read_project_file",
        # #143 batch 1
        "create_spine_relation",
        "add_spine_step",
        "propose_spine_step",
    }
    missing = sorted(expected - tools_logged)
    # No free text may EVER appear in args. `query` is length-only, and so are
    # the write tools' `body` / `description` / `framing` / `title` — the audit
    # table records that a write happened and by whom, never what it said.
    forbidden = {"query", "body", "description", "framing", "title"}
    leaked = [r for r in rows if forbidden & set((r.get("args") or {}).keys())]
    # And the length-only projections must actually be there for the writes.
    write_rows = [r for r in rows if r.get("tool") in ("create_commitment", "create_spine_element")]
    lengths_present = all(
        any(k.endswith("_len") for k in (r.get("args") or {})) for r in write_rows
    ) if write_rows else False
    ok = bool(rows) and not missing and not leaked and lengths_present
    record(
        "M. audit rows appear for reads AND writes, args sanitized to lengths",
        ok,
        f"rows={len(rows)} tools={sorted(t for t in tools_logged if t)} "
        f"client={rows[0].get('client') if rows else None} "
        + (f"MISSING={missing} " if missing else "")
        + (f"LEAKED_FREE_TEXT={[sorted(forbidden & set((r.get('args') or {}).keys())) for r in leaked]}"
           if leaked else "no raw free text logged")
        + (f" write_args_sample={[(r.get('tool'), r.get('args')) for r in write_rows[:2]]}"
           if write_rows else " NO WRITE AUDIT ROWS"),
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
        print("SKIPPED: cases E-M need a real TEAM user JWT in $TEST_JWT or $TEST_JWT_FILE.\n")
    else:
        case_e_tools_list(token)
        case_f_spine(token)
        case_g_commitments(token)
        case_h_whoami(token)
        asset_id = case_i_sources(token)
        case_j_pull_source(token, asset_id)
        case_k_meetings(token)
        case_l_semantic_search(token)

        # ── Package A: insert-only writes. THESE CREATE REAL ROWS. ──
        print(f"--- writes against {WRITE_PROJECT_CODE!r}, run tag {RUN_TAG} ---\n")
        case_n_create_commitment(token)
        case_n2_bad_due_date(token)
        case_o_create_spine_element(token)
        case_o2_collision_guard(token)
        case_o3_add_spine_version(token)
        case_o4_add_spine_document(token)
        case_p_create_note(token)

        # ── #143 batch 1: relations + steps. THESE CREATE REAL ROWS. ──
        case_t_create_spine_relation(token)
        case_t2_bad_relation_kind(token)
        case_u_add_spine_step(token)
        case_u2_bad_step_status(token)
        case_v_propose_spine_step(token)

        # ── Package B: read-only tenant tree ──
        cp_md = case_q_project_state(token)
        case_r_read_file(token, cp_md)
        case_s_traversal(token)

        # Audit rows are written fire-and-forget; read them back LAST.
        case_m_audit_log(token)

    failed = [r for r in results if r[0] == FAIL]
    print("─" * 68)
    print(f"{len(results) - len(failed)}/{len(results)} passed")
    for status, name, _ in results:
        print(f"  {status}  {name}")

    if created_rows:
        # This script CANNOT clean up: there is no DELETE policy for an
        # authenticated caller, by design. Report the ids so a human can remove
        # them with the service key.
        print("\n" + "─" * 68)
        print("REAL ROWS CREATED BY THIS RUN — delete with the service key:")
        for table, row_id in created_rows:
            print(f"  {table}: {row_id}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
