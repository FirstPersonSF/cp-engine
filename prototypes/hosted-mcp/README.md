# hosted-cp OAuth spike — prototype hosted MCP server

Hands-on half of cp-engine #137. Proves that a streamable-HTTP MCP server can
validate Supabase-issued **user** JWTs and serve read tools under the caller's
own identity, with Postgres RLS as the authorization boundary and **no
service-role key anywhere**.

This is a prototype. It is not wired into the CLI, not deployed, and not on the
`cp` import path. Nothing under `src/cp_engine/` was modified.

---

## Run it

```bash
# from the cp-engine repo root
export SUPABASE_URL=https://<ref>.supabase.co
export SUPABASE_ANON_KEY=<the anon/publishable key>          # NEVER the service key

.venv/bin/python prototypes/hosted-mcp/server.py
```

Then, in a second shell:

```bash
export TEST_JWT="$(cat /path/to/a/user/access-token.txt)"   # or TEST_JWT_FILE=...
export PROJECT_CODE=ibx-5153
.venv/bin/python prototypes/hosted-mcp/smoke_test.py
```

### Environment

| Var | Required | Default | Notes |
|---|---|---|---|
| `SUPABASE_URL` | yes | — | Project URL. Issuer is derived as `<url>/auth/v1`. |
| `SUPABASE_ANON_KEY` | yes | — | Anon/publishable key. **Never the service key.** |
| `PORT` | no | `8788` | |
| `HOST` | no | `127.0.0.1` | |
| `RESOURCE_URL` | no | `http://<host>:<port>/mcp` | This server's public identity (RFC 8707/9728 `resource`). Set to the real https URL when deployed. |
| `EXPECTED_AUDIENCE` | no | `authenticated` | Supabase user tokens carry this `aud`. |

Credentials for a local run can be sourced from
`/Users/drewf/Documents/Python/mc-2/backend/.env` (`set -a && . .env && set +a`).
Note that file also contains `SUPABASE_KEY` (the service key) — this prototype
never reads it.

**Packages installed: none.** Everything needed was already in the repo venv:
`mcp` 2.0.0, `PyJWT` 2.12.1 (with `cryptography` 48.0.0 for ES256), `supabase`
2.30.0, `httpx` 0.28.1, `starlette`, `uvicorn`. Python 3.14.

---

## What is proven

Verified live against the real MC-2 project — 8/8 smoke cases pass:

| # | Case | Result |
|---|---|---|
| A | No token → 401 | `WWW-Authenticate: Bearer error="invalid_token", …, resource_metadata="http://…/.well-known/oauth-protected-resource/mcp"` |
| B | Garbage token → 401 | ✓ |
| C | `alg=none` token → 401 | ✓ — negative test of the ES256 path |
| D | RFC 9728 metadata | 200, `authorization_servers: ["https://<ref>.supabase.co/auth/v1"]` |
| E | `tools/list` with valid token | 4 tools |
| F | `list_spine_elements(ibx-5153)` | **57 live elements**, under the caller's `sub` |
| G | `list_commitments(ibx-5153)` | 0 rows + explicit RLS deny-all sentinel |
| H | `whoami` | verified `sub`, `role=authenticated` |

The load-bearing control, run separately: the **anon key alone** returns **0**
rows from `spine_substance`; the **anon key plus the user's JWT** returns **57**.
Identity comes from the token, not the key — RLS is genuinely doing the work.

Confirmed against the live schema, and matching the spike's premise exactly:

- `spine_substance`, `projects`, `initiatives` — RLS on, with `authenticated`
  read policies → readable under a user JWT.
- `commitments` — **RLS on with ZERO policies** → deny-all for `authenticated`.
  `list_commitments` returns a named sentinel rather than a bare `[]`, so this
  known gap stays visible instead of reading as "no commitments".

### The SDK API used

From `mcp` 2.0.0 (`LATEST_PROTOCOL_VERSION = 2026-07-28`), read from installed
source rather than recalled:

- **`mcp.server.auth.provider.TokenVerifier`** — a bare Protocol; one method,
  `async verify_token(token) -> AccessToken | None`. Implemented here as
  `SupabaseJWTVerifier`.
- **`mcp.server.auth.provider.AccessToken`** — the returned model. `token`
  carries the raw compact JWT (this is what the tools forward to PostgREST);
  `subject`, `claims`, `expires_at`, `resource` carry the verified identity.
- **`mcp.server.auth.settings.AuthSettings`** — `issuer_url` +
  `resource_server_url`. Supplying `resource_server_url` is what enables both
  the RFC 9728 route and the `resource_metadata=` hint in 401s.
- **`mcp.server.mcpserver.MCPServer(token_verifier=…, auth=…)`** — passing a
  verifier (not an `auth_server_provider`) puts the server in **resource-server
  only** mode, correct here since Supabase is the authorization server.

`MCPServer.streamable_http_app()` then assembles, in order:
`AuthenticationMiddleware(backend=BearerAuthBackend(verifier))` →
`AuthContextMiddleware` (stashes the user in a contextvar) →
`RequireAuthMiddleware(app, required_scopes, resource_metadata_url)` wrapping the
`/mcp` mount, plus `create_protected_resource_routes(...)` for the metadata
document. Inside a tool,
**`mcp.server.auth.middleware.auth_context.get_access_token()`** returns the
current caller's `AccessToken` — this is the seam that makes per-request
identity possible.

### The 2026-07-28 initialize-less call shape

Worth recording, because it is stricter than "just POST a tools/call" and cost a
debugging round-trip. A stateless request must be self-contained, and the SDK
enforces that as a ladder (`mcp/shared/inbound.py:classify_inbound_request`):

- `params._meta` MUST carry both `io.modelcontextprotocol/protocolVersion` and
  `io.modelcontextprotocol/clientCapabilities` — the negotiation an `initialize`
  would have done, folded into every request;
- the `MCP-Protocol-Version` header MUST equal the envelope's version, and
  `Mcp-Method` MUST equal the body's method;
- for name-bearing methods (`tools/call`), `Mcp-Name` MUST mirror the named
  param, so a proxy can route on headers alone;
- `Accept` must list **both** `application/json` and `text/event-stream`.

Omitting `_meta` yields `-32602`, *after* auth has already succeeded — an easy
error to misread as an auth failure. `smoke_test.py:rpc()` is a working
reference implementation.

---

## Key rotation (done 2026-08-02)

When this spike started, the project still signed session tokens with HS256
against the legacy shared secret; the ES256 key in the JWKS was parked in
"previously used". On 2026-08-02 the ES256 key (`kid 743a6bb6-…`) was rotated
to **current** in the dashboard (Project Settings → JWT Keys), and the interim
HS256 fallback this server briefly carried was deleted. Verification is now
**ES256-via-JWKS only** — the server holds public keys and can verify tokens
but never mint them. The 8/8 smoke run was repeated in this posture with a
real ES256 token.

Do NOT revoke the legacy JWT secret: the `anon`/`service_role` API keys are
themselves HS256 JWTs signed by it, and cp-engine, the webhook, and mc-2 still
authenticate with them. Retiring those keys (migration to `sb_publishable_` /
`sb_secret_`) is a separate work item.

Related pre-rotation fix: three mc-2 Edge Functions (`get-approval-snapshot`,
`submit-approval`, `clickup-export`) had the gateway `verify_jwt` flag on,
which only understood the legacy secret and would have 401'd ES256 sessions.
They were redeployed with `verify_jwt=false` — their real protection is hashed
share tokens / RLS respectively, matching `create-approval-snapshot`.

---

## Deliberately out of scope

- **Writes.** Read tools only. Write verbs need their own RLS policy design.
- **Deployment.** No Dockerfile, no Railway config, no TLS. Binds localhost.
- **Semantic search.** No embeddings or `asset_chunks`.
- **The full authorize/DCR dance.** Supabase is the AS; this server is a
  resource server only and issues no tokens. It advertises where to get one.
- **Session persistence.** Runs `stateless_http=True`; each request stands
  alone.
- **The complete code resolver.** `resolve_project_id` handles initiative
  codes, the dir-slug, and the `<prefix>-<number>` short form — not the engine's
  full `full_job_name` / companies-number bridge.

## Known gaps this surfaced

1. **`commitments` is deny-all under user JWTs** (RLS on, no policies). Needs a
   policy before hosted-cp can serve commitments at all.
2. **`spine_substance` has no `updated_at` column.** The spec asked for one; the
   table carries `synced_at`, `version_date`, and `confirmed_at`. The tools
   return `synced_at` as the closest analogue.
3. **Three strings name one project** — `ibx-5153` (short), `ibx-5153-ai-campaign`
   (`spine_substance.project_code`, the cp-tree dir-slug), and `IBX-ai-campaign`
   (`projects.code`). A hosted server needs one shared resolver, or every tool
   re-invents this. Compare the memory note on spine slug drift.
4. **`spine_substance` read policy is `USING (true)` for `authenticated`** — any
   logged-in user reads every project's spine. RLS is enforced, but it is not yet
   *per-user scoped*. Real multi-tenant hosting needs per-user/per-project
   policies; this spike proves the mechanism, not the policy set.
