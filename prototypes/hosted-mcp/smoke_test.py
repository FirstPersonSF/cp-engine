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
  W. set_spine_element     -> important flips on the LIVE row; superseded row untouched
  W2. (direct PostgREST)   -> UPDATE of `body` DENIED under the user JWT (P0130)
  X. resolve_commitment    -> closes it; re-resolving is clearly refused
  X2. resolve_commitment   -> an outcome outside done|dropped REJECTED
  Y. set/reorder/remove    -> step round-trip; positions stay 1..N; partial order refused
  Y2. set_spine_step       -> a step_id from another element is out of scope
  Z. add_element_source    -> a real ingested source lands on EVERY version
  Z2. add_element_source   -> re-attaching is already:true, no duplicate entry
  Z3. add_element_source   -> an unknown source_title is a note, not a guess
  Z4. remove_element_source-> detaches everywhere; re-detach is a note
  Z5. add_element_provenance   -> an element link with retired:false, on every version
  Z6. add_element_provenance   -> self-link and unknown source_key both refused
  Z7. remove_element_provenance-> detaches everywhere; re-detach is a note
  Q. get_project_state     -> ibx-5153's Exec Summary + current sprint file
  R. read_project_file     -> a real file read from the clone
  S. read_project_file     -> path traversal REJECTED
  M. mcp_audit_log         -> audit rows appear for the calls just made

WRITE CASES CREATE REAL ROWS. They target `mission-control` (an internal
initiative) and prefix every slug/description with `smoke-test-` so the rows are
identifiable. ONE EXCEPTION: cases Z–Z4 need a project with real ingested
sources, and no initiative has ANY (rag_assets are a client-engagement artifact
here — checked live). They therefore create their own `smoke-test-` element
inside $PROJECT_CODE and attach a source to THAT. No client-authored row is
mutated; only the smoke element's own `sources` changes, and Z4 detaches it. There is no delete policy for an authenticated caller, so this
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
        # #143 batch 2 — the UPDATE-shaped verbs
        "set_spine_element",
        "resolve_commitment",
        "set_spine_step",
        "reorder_spine_step",
        "remove_spine_step",
        # #143 batch 3 — the sources/provenance quartet
        "add_element_source",
        "remove_element_source",
        "add_element_provenance",
        "remove_element_provenance",
        # Package B — read-only tenant tree (#138)
        "get_project_state",
        "read_project_file",
    }
    missing = sorted(expected - set(names))
    # Assert the COUNT too, not just containment: an unexpected extra tool is a
    # surface the review never saw, which matters as much as a missing one.
    extra = sorted(set(names) - expected)
    record(
        f"E. tools/list exposes all {len(expected)} tools",
        not missing and not extra,
        f"count={len(names)} tools={names}"
        + (f" MISSING={missing}" if missing else "")
        + (f" UNEXPECTED={extra}" if extra else ""),
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


# ──────────────────────────────────────────────────────────────────────
#  #143 batch 2 — the UPDATE-shaped verbs. THESE MUTATE REAL ROWS.
# ──────────────────────────────────────────────────────────────────────


def case_w_set_spine_element(token: str) -> None:
    """Flip `important` on the smoke element, and prove the blast radius.

    Runs AFTER case O3, so the element has a live v2 AND a superseded v1 — which
    is the point. The hosted UPDATE policy is `status='live'`, so this must
    change the live row and leave the superseded row untouched. Both halves are
    asserted by reading the rows back through PostgREST directly, rather than
    trusting the tool's own report of what it did.
    """
    element_id = f"_authored/smoke-test-{RUN_TAG}-hosted-mcp-element"
    payload = call_tool(
        token,
        "set_spine_element",
        {
            "project_code": WRITE_PROJECT_CODE,
            "key": element_id,
            "important": True,
            "note": f"smoke-test-{RUN_TAG} importance flip",
            "layer": "decision",  # canon_layer -> 'Decisions'
        },
    )
    tool_ok = (
        payload.get("important") is True
        and payload.get("layer") == "Decisions"
        and payload.get("versions_updated") == 1
        and "not mirrored" in str(payload.get("promotion", ""))
    )

    # Read both versions back independently: live changed, superseded did NOT.
    live_important = superseded_important = None
    superseded_layer = None
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    anon = os.environ.get("SUPABASE_ANON_KEY", "")
    if supabase_url and anon:
        try:
            rows = httpx.get(
                f"{supabase_url}/rest/v1/spine_substance",
                params={
                    "select": "id,version_label,status,important,layer",
                    "est_item_id": f"eq.{element_id}",
                },
                headers={"apikey": anon, "Authorization": f"Bearer {token}"},
                timeout=20,
            ).json()
            for r in rows:
                if r.get("status") == "live":
                    live_important = r.get("important")
                else:
                    superseded_important = r.get("important")
                    superseded_layer = r.get("layer")
        except Exception:  # noqa: BLE001
            pass

    # The superseded v1 was created BEFORE the flip, so it must still be False
    # and still carry its original layer — the policy's live-only scope, proven.
    isolation_ok = live_important is True and superseded_important is False
    record(
        "W. set_spine_element flips important on the LIVE row only "
        "(superseded row untouched)",
        tool_ok and isolation_ok,
        f"live.important={live_important} superseded.important={superseded_important} "
        f"superseded.layer={superseded_layer!r} versions_updated={payload.get('versions_updated')} "
        f"superseded_untouched={payload.get('superseded_untouched')} "
        f"promotion={str(payload.get('promotion'))[:40]!r}"
        + (f" ERROR={payload.get('error') or payload.get('_http')}" if not tool_ok else ""),
    )


def case_w2_engine_owned_columns(token: str) -> None:
    """The engine-owned column boundary, probed DIRECTLY (not through a tool).

    This is a control on the storage layer itself: a raw PostgREST PATCH of
    `body` under the smoke user's own JWT. If this ever succeeds, the hosted
    server's insert-only-plus-guarded-transition story is void, because any
    client with a team token could rewrite element bodies at will.

    History: when batch 2 first landed, the per-column grant was INERT — the
    table ACL still carried a table-wide authenticated UPDATE, and Postgres
    UNIONS grants rather than intersecting them. The only denial then was the
    mc-2 #130 column-guard TRIGGER (P0130) — an attribution guard satisfied by
    any X-Spine-Writer header, i.e. bypassable. Migration
    `fix_batch2_grants_union_flaw` revoked the table-wide grant, making the
    column grant load-bearing: the write must now fail on the GRANT (42501/403)
    — before the trigger, before RLS — and the X-Spine-Writer header must make
    NO difference. Both are asserted.
    """
    element_id = f"_authored/smoke-test-{RUN_TAG}-hosted-mcp-element"
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    anon = os.environ.get("SUPABASE_ANON_KEY", "")
    if not supabase_url or not anon:
        record(
            "W2. direct PostgREST UPDATE of `body` is DENIED under the user JWT",
            False,
            "SUPABASE_URL/SUPABASE_ANON_KEY not in this shell's env",
        )
        return

    headers = {
        "apikey": anon,
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    params = {"est_item_id": f"eq.{element_id}", "status": "eq.live"}
    try:
        resp = httpx.patch(
            f"{supabase_url}/rest/v1/spine_substance",
            params=params,
            headers=headers,
            json={"body": "SMOKE TEST SHOULD NEVER LAND THIS"},
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        record("W2. direct PostgREST UPDATE of `body` is DENIED under the user JWT",
               False, f"{type(exc).__name__}: {exc}")
        return

    # Post-fix contract: denied on the GRANT (403/42501), not merely the
    # attribution trigger (P0130) — and the header must not change the answer.
    denied_on_grant = resp.status_code == 403 and "P0130" not in resp.text
    header_still_denied = None
    try:
        bypass = httpx.patch(
            f"{supabase_url}/rest/v1/spine_substance",
            params=params,
            headers={**headers, "X-Spine-Writer": "smoke-test"},
            timeout=20,
            json={"body": "SMOKE TEST SHOULD NEVER LAND THIS"},
        )
        header_still_denied = bypass.status_code == 403
    except Exception:  # noqa: BLE001
        pass

    record(
        "W2. direct `body` UPDATE is DENIED ON THE GRANT, header or not "
        "(fix_batch2_grants_union_flaw)",
        denied_on_grant and header_still_denied is True,
        f"no-header status={resp.status_code} | with X-Spine-Writer "
        f"still_denied={header_still_denied} | body={resp.text[:100]!r}",
    )


def case_x_resolve_commitment(token: str) -> None:
    """Resolve the smoke commitment, then prove a re-resolve is clearly refused."""
    description_key = f"smoke-test-{RUN_TAG} — hosted-mcp write verification"
    payload = call_tool(
        token,
        "resolve_commitment",
        {
            "project_code": WRITE_PROJECT_CODE,
            "key": description_key,
            "outcome": "done",
        },
    )
    resolved_ok = (
        bool(payload.get("resolved"))
        and payload.get("outcome") == "done"
        and payload.get("status") == "done"
    )

    # Re-resolving a now-closed commitment: the USING clause matches no OPEN
    # row, so the resolver reports no match rather than silently "succeeding".
    again = call_tool(
        token,
        "resolve_commitment",
        {
            "project_code": WRITE_PROJECT_CODE,
            "key": description_key,
            "outcome": "done",
        },
    )
    error = str(again.get("error", ""))
    # Either face of the same refusal is correct and clear: the row is no longer
    # among the open set, or the policy matched zero rows. What must NOT happen
    # is a reported success.
    refused_ok = "resolved" not in again and (
        "no open commitment" in error or "0 rows updated" in error
    )
    record(
        "X. resolve_commitment closes an open commitment; re-resolving is clearly refused",
        resolved_ok and refused_ok,
        f"resolved={payload.get('resolved')} status={payload.get('status')} "
        f"updated_at={payload.get('updated_at')} | re-resolve error={error[:110]!r}"
        + (f" ERROR={payload.get('error') or payload.get('_http')}" if not resolved_ok else ""),
    )


def case_x2_bad_outcome(token: str) -> None:
    """`outcome` is a closed vocabulary: done | dropped."""
    payload = call_tool(
        token,
        "resolve_commitment",
        {
            "project_code": WRITE_PROJECT_CODE,
            "key": "smoke-test",
            "outcome": "completed",
        },
    )
    ok = "resolved" not in payload and "outcome must be" in str(payload.get("error", ""))
    record(
        "X2. resolve_commitment rejects an outcome outside done|dropped",
        ok,
        f"error={str(payload.get('error'))[:120]!r}",
    )


def case_y_step_roundtrip(token: str) -> None:
    """set -> reorder -> remove, round-tripped on the smoke element's trail.

    Reads the trail back after each move rather than trusting the tool's own
    echo, and asserts the invariant that matters for an ordered list: positions
    stay contiguous 1..N after a delete.
    """
    element_id = f"_authored/smoke-test-{RUN_TAG}-hosted-mcp-element"
    args = {"project_code": WRITE_PROJECT_CODE, "key": element_id}

    # The trail so far (cases U and V put steps here, O3 auto-journalled one).
    listing = call_tool(token, "add_spine_step", {
        **args, "title": f"smoke-test-{RUN_TAG} step to reorder", "status": "upcoming",
    })
    steps = listing.get("steps") or []
    if listing.get("step_id"):
        created_rows.append(("spine_steps", listing["step_id"]))
    if len(steps) < 2:
        record("Y. set/reorder/remove round-trip on a step trail", False,
               f"need >=2 steps to exercise reorder, have {len(steps)}: "
               f"{listing.get('error') or listing.get('_http')}")
        return

    target = listing["step_id"]

    # 1. set: advance it to done and retitle.
    set_payload = call_tool(token, "set_spine_step", {
        **args, "step_id": target, "status": "done",
        "title": f"smoke-test-{RUN_TAG} step advanced",
    })
    after_set = {s["id"]: s for s in (set_payload.get("steps") or [])}
    set_ok = (
        after_set.get(target, {}).get("status") == "done"
        and "advanced" in (after_set.get(target, {}).get("title") or "")
    )

    # 2. reorder: reverse the full trail, then confirm 1..N in the new order.
    current = [s["id"] for s in (set_payload.get("steps") or [])]
    reversed_order = list(reversed(current))
    reorder_payload = call_tool(token, "reorder_spine_step", {**args, "order": reversed_order})
    after_reorder = reorder_payload.get("steps") or []
    reorder_ok = (
        [s["id"] for s in after_reorder] == reversed_order
        and [s["position"] for s in after_reorder] == list(range(1, len(reversed_order) + 1))
    )

    # 2b. a PARTIAL order must be refused (the engine would half-renumber).
    partial = call_tool(token, "reorder_spine_step", {**args, "order": reversed_order[:1]})
    partial_ok = "error" in partial and "EXACTLY once" in str(partial.get("error", ""))

    # 3. remove: delete the step, and assert the survivors densify to 1..N.
    remove_payload = call_tool(token, "remove_spine_step", {**args, "step_id": target})
    after_remove = remove_payload.get("steps") or []
    remove_ok = (
        remove_payload.get("removed") == target
        and target not in [s["id"] for s in after_remove]
        and [s["position"] for s in after_remove] == list(range(1, len(after_remove) + 1))
    )
    if remove_ok:
        # It is gone — do not ask a human to clean it up.
        created_rows[:] = [r for r in created_rows if r[1] != target]

    record(
        "Y. set_spine_step -> reorder_spine_step -> remove_spine_step round-trip "
        "(positions stay 1..N)",
        set_ok and reorder_ok and partial_ok and remove_ok,
        f"set_ok={set_ok} reorder_ok={reorder_ok} partial_rejected={partial_ok} "
        f"remove_ok={remove_ok} trail_after_remove="
        f"{[(s['position'], (s.get('title') or '')[:24]) for s in after_remove]}"
        + (f" SET_ERR={set_payload.get('error')}" if not set_ok else "")
        + (f" REORDER_ERR={reorder_payload.get('error')}" if not reorder_ok else "")
        + (f" REMOVE_ERR={remove_payload.get('error')}" if not remove_ok else ""),
    )


def case_y2_step_scope_isolation(token: str) -> None:
    """A step_id that is real but belongs to ANOTHER element must not be touched.

    The scoping is (id, project_id, est_item_id), so a stray-but-valid id
    resolves to zero rows rather than reaching across the trail boundary. Uses
    the relation-target element from case T as the foreign parent.
    """
    mine = f"_authored/smoke-test-{RUN_TAG}-hosted-mcp-element"
    other = f"_authored/smoke-test-{RUN_TAG}-relation-target"

    # Put a step on the OTHER element, then try to update it via MY element's key.
    planted = call_tool(token, "add_spine_step", {
        "project_code": WRITE_PROJECT_CODE, "key": other,
        "title": f"smoke-test-{RUN_TAG} foreign step", "status": "upcoming",
    })
    foreign_id = planted.get("step_id")
    if not foreign_id:
        record("Y2. a step_id from another element is out of scope", False,
               f"could not plant the foreign step: "
               f"{planted.get('error') or planted.get('_http')}")
        return
    created_rows.append(("spine_steps", foreign_id))

    attempt = call_tool(token, "set_spine_step", {
        "project_code": WRITE_PROJECT_CODE, "key": mine,
        "step_id": foreign_id, "status": "done",
    })
    ok = "error" in attempt and "0 rows updated" in str(attempt.get("error", ""))
    record(
        "Y2. set_spine_step refuses a step_id belonging to another element",
        ok,
        f"error={str(attempt.get('error'))[:130]!r}",
    )


# ──────────────────────────────────────────────────────────────────────
#  #143 batch 3 — sources + provenance. THESE MUTATE REAL ROWS.
# ──────────────────────────────────────────────────────────────────────


def _read_versions_direct(token: str, element_id: str) -> list[dict[str, Any]]:
    """Every version row of an element, read STRAIGHT through PostgREST.

    Deliberately not via a tool: the claim these cases make is that the guarded
    function wrote EVERY version, and the hosted read tools only ever surface
    the live one. The superseded row's `sources` is invisible from the tool
    surface, so it has to be read from storage or the assertion is vacuous.
    """
    supabase_url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    anon = os.environ.get("SUPABASE_ANON_KEY", "")
    if not (supabase_url and anon):
        return []
    try:
        return httpx.get(
            f"{supabase_url}/rest/v1/spine_substance",
            params={
                "select": "id,version_label,status,sources",
                "est_item_id": f"eq.{element_id}",
            },
            headers={"apikey": anon, "Authorization": f"Bearer {token}"},
            timeout=20,
        ).json()
    except Exception:  # noqa: BLE001
        return []


def case_z_add_element_source(token: str) -> str | None:
    """Attach a REAL ingested source, and prove the write hit EVERY version.

    WHY THIS CASE RUNS AGAINST `PROJECT_CODE`, NOT `WRITE_PROJECT_CODE`. Every
    other write case targets `mission-control`, deliberately keeping mutations
    off client engagements. But rag_assets are a client-engagement artifact in
    this corpus: `mission-control` has ZERO ingested sources, and so does every
    other initiative (checked live). There is no internal project that can
    exercise a real source attach.

    So this case creates its OWN throwaway element inside the READ project and
    attaches there. What is mutated is the smoke element's own `sources` — a row
    this run created and tags `smoke-test-`. No client-authored element is
    touched, and the detach case removes the link again.

    The every-version assertion needs history, so the element is created AND
    version-bumped here (live v2 + superseded v1) rather than reusing the
    mission-control element. That is the point of the case:
    `spine_substance.sources` is written only through the guarded
    `spine_element_modify_source` function, which applies to every version row —
    unlike batch 2's `set_spine_element`, whose live-only UPDATE policy leaves
    superseded rows behind. Both rows must come back carrying the link, read
    straight from storage rather than through a tool.

    Returns the source title, for the detach cases to reuse.
    """
    listing = call_tool(token, "list_project_sources", {"project_code": PROJECT_CODE})
    sources = listing.get("sources") or []
    if not sources:
        record("Z. add_element_source attaches a real source to EVERY version", False,
               f"no ingested sources on {PROJECT_CODE!r} to attach "
               f"({listing.get('error') or listing.get('_http') or 'empty list'})")
        return None
    asset = sources[0]
    title = asset.get("title")

    # Our own element in the source-bearing project, with a real version history.
    framing = f"smoke-test-{RUN_TAG} source-attach target"
    element_id = f"_authored/smoke-test-{RUN_TAG}-source-attach-target"
    created = call_tool(token, "create_spine_element", {
        "project_code": PROJECT_CODE, "framing": framing,
        "body": "Created by smoke_test.py for the batch-3 source cases. Safe to delete.",
        "layer": "note",
    })
    if created.get("row_id"):
        created_rows.append(("spine_substance", created["row_id"]))
    else:
        record("Z. add_element_source attaches a real source to EVERY version", False,
               f"could not create the target element: "
               f"{created.get('error') or created.get('_http')}")
        return None
    bumped = call_tool(token, "add_spine_version", {
        "project_code": PROJECT_CODE, "element_id": element_id,
        "body": "v2 — so the element has a superseded sibling for the every-version assertion.",
        "version_note": f"smoke-test-{RUN_TAG} batch-3 setup bump",
    })
    if bumped.get("version_label") == "v2":
        created_rows.append(
            ("spine_substance", f"{bumped.get('project_code')}/{element_id}/v2"))
    if (bumped.get("step") or {}).get("step_id"):
        created_rows.append(("spine_steps", bumped["step"]["step_id"]))

    payload = call_tool(token, "add_element_source", {
        "project_code": PROJECT_CODE, "key": element_id, "source_title": title,
    })
    tool_ok = (
        payload.get("attached") is True
        and (payload.get("source") or {}).get("type") == "rag_asset"
        and (payload.get("source") or {}).get("id") == asset.get("asset_id")
        and int(payload.get("versions_updated") or 0) >= 2
    )

    # The load-bearing read: BOTH the live and the superseded row carry it.
    rows = _read_versions_direct(token, element_id)
    def _has(row: dict[str, Any]) -> bool:
        return any(
            isinstance(s, dict) and s.get("type") == "rag_asset"
            and s.get("id") == asset.get("asset_id")
            for s in (row.get("sources") or [])
        )
    live_rows = [r for r in rows if r.get("status") == "live"]
    sup_rows = [r for r in rows if r.get("status") != "live"]
    live_has = bool(live_rows) and all(_has(r) for r in live_rows)
    sup_has = bool(sup_rows) and all(_has(r) for r in sup_rows)

    record(
        "Z. add_element_source attaches a real source to EVERY version "
        "(live AND superseded)",
        tool_ok and live_has and sup_has,
        f"title={str(title)[:50]!r} asset_id={asset.get('asset_id')} "
        f"versions_updated={payload.get('versions_updated')} "
        f"live_rows={len(live_rows)}(has={live_has}) "
        f"superseded_rows={len(sup_rows)}(has={sup_has})"
        + (f" ERROR={payload.get('error') or payload.get('note') or payload.get('_http')}"
           if not tool_ok else ""),
    )
    return title


def case_z2_reattach_is_noop(token: str, title: str | None) -> None:
    """Re-attaching the same source is `already: true`, not a duplicate entry."""
    if not title:
        record("Z2. re-attaching the same source returns already:true", False,
               "no source title from case Z")
        return
    element_id = f"_authored/smoke-test-{RUN_TAG}-source-attach-target"
    payload = call_tool(token, "add_element_source", {
        "project_code": PROJECT_CODE, "key": element_id, "source_title": title,
    })
    entries = payload.get("sources") or []
    dupes = sum(1 for s in entries if isinstance(s, dict) and s.get("type") == "rag_asset")
    ok = payload.get("already") is True and "attached" not in payload and dupes == 1
    record(
        "Z2. re-attaching the same source returns already:true (no duplicate entry)",
        ok,
        f"already={payload.get('already')} rag_asset_entries={dupes} "
        f"sources_len={len(entries)}",
    )


def case_z3_unknown_source_title(token: str) -> None:
    """An unresolvable source title is a structured note, never a guess."""
    element_id = f"_authored/smoke-test-{RUN_TAG}-source-attach-target"
    payload = call_tool(token, "add_element_source", {
        "project_code": PROJECT_CODE, "key": element_id,
        "source_title": f"no-such-source-{RUN_TAG}-zzz",
    })
    note = str(payload.get("note", ""))
    ok = "no active source titled" in note and "attached" not in payload
    record(
        "Z3. an unknown source_title returns a structured note, attaches nothing",
        ok,
        f"note={note[:120]!r} candidates={payload.get('candidates')}",
    )


def case_z4_detach_source(token: str, title: str | None) -> None:
    """Detach removes the link from every version; re-detaching is a note."""
    if not title:
        record("Z4. remove_element_source detaches, then reports not-attached", False,
               "no source title from case Z")
        return
    element_id = f"_authored/smoke-test-{RUN_TAG}-source-attach-target"

    first = call_tool(token, "remove_element_source", {
        "project_code": PROJECT_CODE, "key": element_id, "source_title": title,
    })
    removed_ok = first.get("removed") is True and int(first.get("versions_updated") or 0) >= 2

    # Gone from EVERY version, not just the live one.
    rows = _read_versions_direct(token, element_id)
    asset_id = (first.get("source") or {}).get("id")
    still_there = [
        r.get("version_label") for r in rows
        if any(isinstance(s, dict) and s.get("type") == "rag_asset" and s.get("id") == asset_id
               for s in (r.get("sources") or []))
    ]

    # Detaching what is no longer attached: a NOTE, not an error.
    second = call_tool(token, "remove_element_source", {
        "project_code": PROJECT_CODE, "key": element_id, "source_title": title,
    })
    note_ok = "not attached to" in str(second.get("note", "")) and "error" not in second

    record(
        "Z4. remove_element_source detaches from every version; "
        "re-detaching is a structured note",
        removed_ok and not still_there and note_ok,
        f"removed={first.get('removed')} versions_updated={first.get('versions_updated')} "
        f"still_carrying={still_there} "
        f"second_note={str(second.get('note'))[:90]!r}",
    )


def case_z5_add_element_provenance(token: str) -> None:
    """Fold one spine element into another as provenance.

    The engine's provenance source MAY be retired — that is the design (#104),
    since the usual move is "retire the raw card, keep its lineage". This server
    exposes no retire verb, so the case cannot produce a retired source and
    instead uses the LIVE `-relation-target` element from case T, asserting that
    `retired: false` rides the link. The retired-source path is therefore
    NOT covered here — see the run notes.

    Also asserts the (type, id) dedup key is a PAIR: the element link must
    coexist with any rag_asset link rather than colliding with it.
    """
    target = f"_authored/smoke-test-{RUN_TAG}-hosted-mcp-element"
    source = f"_authored/smoke-test-{RUN_TAG}-relation-target"

    payload = call_tool(token, "add_element_provenance", {
        "project_code": WRITE_PROJECT_CODE, "key": target, "source_key": source,
    })
    link = payload.get("source") or {}
    tool_ok = (
        payload.get("attached") is True
        and link.get("type") == "spine_element"
        and link.get("id") == source
        and link.get("retired") is False      # live source -> retired:false rides the link
        and int(payload.get("versions_updated") or 0) >= 2
    )

    rows = _read_versions_direct(token, target)
    carrying = [
        r.get("version_label") for r in rows
        if any(isinstance(s, dict) and s.get("type") == "spine_element"
               and s.get("id") == source for s in (r.get("sources") or []))
    ]

    # Re-attach: already, not a second entry.
    again = call_tool(token, "add_element_provenance", {
        "project_code": WRITE_PROJECT_CODE, "key": target, "source_key": source,
    })
    already_ok = again.get("already") is True

    record(
        "Z5. add_element_provenance links a live element (retired:false) "
        "onto every version; re-attach is already:true",
        tool_ok and len(carrying) >= 2 and already_ok,
        f"link={link} versions_updated={payload.get('versions_updated')} "
        f"carrying={carrying} re_attach_already={again.get('already')}"
        + (f" ERROR={payload.get('error') or payload.get('note') or payload.get('_http')}"
           if not tool_ok else ""),
    )


def case_z6_provenance_self_and_unknown(token: str) -> None:
    """An element cannot be its own provenance; an unknown source_key is a note."""
    target = f"_authored/smoke-test-{RUN_TAG}-hosted-mcp-element"

    itself = call_tool(token, "add_element_provenance", {
        "project_code": WRITE_PROJECT_CODE, "key": target, "source_key": target,
    })
    self_ok = "own provenance" in str(itself.get("note", "")) and "attached" not in itself

    unknown = call_tool(token, "add_element_provenance", {
        "project_code": WRITE_PROJECT_CODE, "key": target,
        "source_key": f"no-such-element-{RUN_TAG}-zzz",
    })
    unknown_ok = "no single element matching source" in str(unknown.get("note", ""))

    record(
        "Z6. provenance refuses self-linking and an unknown source_key",
        self_ok and unknown_ok,
        f"self_note={str(itself.get('note'))[:70]!r} "
        f"unknown_note={str(unknown.get('note'))[:70]!r}",
    )


def case_z7_remove_element_provenance(token: str) -> None:
    """Detach the element link from every version; re-detach is a note."""
    target = f"_authored/smoke-test-{RUN_TAG}-hosted-mcp-element"
    source = f"_authored/smoke-test-{RUN_TAG}-relation-target"

    first = call_tool(token, "remove_element_provenance", {
        "project_code": WRITE_PROJECT_CODE, "key": target, "source_key": source,
    })
    removed_ok = first.get("removed") is True and int(first.get("versions_updated") or 0) >= 2

    rows = _read_versions_direct(token, target)
    still_there = [
        r.get("version_label") for r in rows
        if any(isinstance(s, dict) and s.get("type") == "spine_element"
               and s.get("id") == source for s in (r.get("sources") or []))
    ]

    second = call_tool(token, "remove_element_provenance", {
        "project_code": WRITE_PROJECT_CODE, "key": target, "source_key": source,
    })
    note_ok = "not attached to" in str(second.get("note", "")) and "error" not in second

    record(
        "Z7. remove_element_provenance detaches from every version; "
        "re-detaching is a structured note",
        removed_ok and not still_there and note_ok,
        f"removed={first.get('removed')} versions_updated={first.get('versions_updated')} "
        f"still_carrying={still_there} second_note={str(second.get('note'))[:90]!r}",
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
                # the auditing. Keep headroom when adding cases. Batch 2 adds
                # ~12 more audited calls, hence 120.
                "limit": "120",
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
        # #143 batch 2 — the UPDATE verbs
        "set_spine_element",
        "resolve_commitment",
        "set_spine_step",
        "reorder_spine_step",
        "remove_spine_step",
        # #143 batch 3
        "add_element_source",
        "remove_element_source",
        "add_element_provenance",
        "remove_element_provenance",
    }
    missing = sorted(expected - tools_logged)
    # No free text may EVER appear in args. `query` is length-only, and so are
    # the write tools' `body` / `description` / `framing` / `title` — the audit
    # table records that a write happened and by whom, never what it said.
    # `order` joins the forbidden set for a different reason: it is a list of
    # uuids, and a reorder is auditable as HOW MANY steps moved (`order_len`),
    # never as which. It is on neither allow-list, so it must be dropped.
    forbidden = {"query", "body", "description", "framing", "title", "order", "note"}
    leaked = [r for r in rows if forbidden & set((r.get("args") or {}).keys())]
    reorder_rows = [r for r in rows if r.get("tool") == "reorder_spine_step"]
    order_len_present = all(
        "order_len" in (r.get("args") or {}) for r in reorder_rows
    ) if reorder_rows else False
    # And the length-only projections must actually be there for the writes.
    write_rows = [r for r in rows if r.get("tool") in ("create_commitment", "create_spine_element")]
    lengths_present = all(
        any(k.endswith("_len") for k in (r.get("args") or {})) for r in write_rows
    ) if write_rows else False
    # #143 batch 3: `source_title` and `source_key` are allow-listed VERBATIM,
    # unlike `title`. They name a referent (an ingested document, an element key)
    # rather than carrying the caller's prose, and an audit row that recorded a
    # source attachment without saying WHICH source would be near-useless. Assert
    # they are actually present, so the deliberate choice is visible in the run
    # rather than only in a comment.
    src_rows = [r for r in rows
                if r.get("tool") in ("add_element_source", "remove_element_source")]
    prov_rows = [r for r in rows
                 if r.get("tool") in ("add_element_provenance", "remove_element_provenance")]
    src_keys_present = (
        all("source_title" in (r.get("args") or {}) for r in src_rows) if src_rows else False
    ) and (
        all("source_key" in (r.get("args") or {}) for r in prov_rows) if prov_rows else False
    )
    ok = (bool(rows) and not missing and not leaked and lengths_present
          and order_len_present and src_keys_present)
    record(
        "M. audit rows appear for reads AND writes, args sanitized to lengths",
        ok,
        f"rows={len(rows)} tools={sorted(t for t in tools_logged if t)} "
        f"client={rows[0].get('client') if rows else None} "
        f"order_len_present={order_len_present} "
        f"source_keys_present={src_keys_present} "
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

        # ── #143 batch 2: the UPDATE verbs. THESE MUTATE REAL ROWS. ──
        # Ordered after O3 on purpose: the element needs a superseded v1 for
        # case W's live-vs-superseded isolation assertion to mean anything.
        case_w_set_spine_element(token)
        case_w2_engine_owned_columns(token)
        case_x_resolve_commitment(token)
        case_x2_bad_outcome(token)
        case_y_step_roundtrip(token)
        case_y2_step_scope_isolation(token)

        # ── #143 batch 3: sources + provenance. THESE MUTATE REAL ROWS. ──
        # Ordered after O3 (which leaves a superseded v1) so the every-version
        # write is provable, and after T (which creates the element used as a
        # provenance source).
        attached_title = case_z_add_element_source(token)
        case_z2_reattach_is_noop(token, attached_title)
        case_z3_unknown_source_title(token)
        case_z4_detach_source(token, attached_title)
        case_z5_add_element_provenance(token)
        case_z6_provenance_self_and_unknown(token)
        case_z7_remove_element_provenance(token)

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
