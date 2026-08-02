# hosted-cp OAuth spike — prototype hosted MCP server

Hands-on half of cp-engine #137. Proves that a streamable-HTTP MCP server can
validate Supabase-issued **user** JWTs and serve tools under the caller's own
identity, with Postgres RLS as the authorization boundary and **no service-role
key anywhere**.

This is a prototype. It is not wired into the CLI and not on the `cp` import
path. Nothing under `src/cp_engine/` was modified.

**0.0.3** adds two work packages on top of the 8 read tools:

- **A — narrow, insert-only writes** (#139): `create_note`,
  `create_commitment`, `create_spine_element`. `add_spine_version` is
  **deferred by design** — see [Deferred: `add_spine_version`](#deferred-add_spine_version).
- **B — read-only tenant-tree tools** (#138, review finding 1):
  `get_project_state`, `read_project_file`, served from a shallow clone of
  `TENANT_REPO`. **The tree has no per-user RLS** — see
  [The tenant tree has no RLS](#the-tenant-tree-has-no-rls).

13 tools, **21/21 smoke cases pass**.

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

The token must belong to a **team member** (a `public.profiles` row) — see the
policy-model section. The smoke test's case M also reads `mcp_audit_log` back
through PostgREST, so `SUPABASE_URL` / `SUPABASE_ANON_KEY` must be in the test
shell's environment too.

### Environment

| Var | Required | Default | Notes |
|---|---|---|---|
| `SUPABASE_URL` | yes | — | Project URL. Issuer is derived as `<url>/auth/v1`. |
| `SUPABASE_ANON_KEY` | yes | — | Anon/publishable key. **Never the service key.** |
| `PORT` | no | `8788` | |
| `HOST` | no | `127.0.0.1` | |
| `RESOURCE_URL` | no | `http://<host>:<port>/mcp` | This server's public identity (RFC 8707/9728 `resource`). Set to the real https URL when deployed. |
| `EXPECTED_AUDIENCE` | no | `authenticated` | Supabase user tokens carry this `aud`. |
| `VOYAGE_API_KEY` | for search | — | Embeds `semantic_search` queries. **This is the key that makes search work** — see below. Absent, the tool still exists and returns a clean "search unavailable". |
| `OPENAI_API_KEY` | no | — | Read and reported, but **not** used for embeddings. See the note below. |
| `INGEST_EMBEDDING_MODEL` | no | `voyage-3-large` | Must match what ingest used. |
| `TENANT_REPO` | for tree tools | — | The cp tenant repo: `git@github.com:FirstPersonSF/cp.git` in the deployment, or a **local clone path** for development. Absent, the tree tools still exist and return a clean "tree access unavailable". |
| `GIT_SSH_KEY` | for an ssh remote | — | Read-only deploy key material. Required when `TENANT_REPO` is an ssh remote; **optional when it is a local path**, because a local clone needs no ssh at all. |
| `TREE_PULL_DEBOUNCE_SECONDS` | no | `60` | Skip `git pull` if the last one was this recent. |
| `TREE_MAX_FILE_BYTES` | no | `204800` | `read_project_file` size cap (200 KiB). |

#### On `OPENAI_API_KEY` vs `VOYAGE_API_KEY`

The #138 brief said to embed search queries with OpenAI. **The live corpus says
otherwise, and the corpus wins.** `cp_engine.asset_ingest` embeds with
**Voyage `voyage-3-large`** (`asset_ingest.py:1070`,
`asset_ingest_settings.INGEST_EMBEDDING_MODEL`), and `match_chunks_simple`
accepts a **1024-dim** vector — confirmed live by probing the RPC with a
1024-float vector.

An OpenAI query embedding would be wrong twice over: the wrong **dimension**
(1536/3072 vs 1024 — a hard Postgres error), and, more fundamentally, from a
different **vector space**. Cosine distance between a Voyage-embedded corpus and
an OpenAI-embedded query is noise even where the arithmetic happens to line up —
it would return confidently-ranked garbage rather than fail. Query embeddings
must come from the same model as the stored ones, so this server uses Voyage.

`OPENAI_API_KEY` is still read, and if it is set while `VOYAGE_API_KEY` is not,
the unavailable-message says exactly that rather than quietly doing the wrong
thing.

Credentials for a local run can be sourced from
`/Users/drewf/Documents/Python/mc-2/backend/.env` (`set -a && . .env && set +a`).
Note that file also contains `SUPABASE_KEY` (the service key) — this prototype
never reads it.

**Packages installed: none.** Everything needed was already in the repo venv:
`mcp` 2.0.0, `PyJWT` 2.12.1 (with `cryptography` 48.0.0 for ES256), `supabase`
2.30.0, `httpx` 0.28.1, `voyageai`, `starlette`, `uvicorn`. Python 3.14.

---

## Tools

All thirteen run under the caller's identity, select **explicit columns only**,
and write an audit row on success.

**Reads:**

| Tool | Source | Notes |
|---|---|---|
| `list_spine_elements(project_code)` | `spine_substance` | Live, non-archived elements. |
| `pull_spine_element(element_id, project_code?)` | `spine_substance` | Body + metadata; newest version wins. |
| `list_commitments(project_code)` | `commitments` | Real rows since the team-keyed policy pass. |
| `list_project_sources(project_code)` | `rag_assets` | Manifest shape; drops superseded assets. |
| `pull_project_source(asset_id, max_chars=40000)` | `asset_chunks` | Assembles document text — see below. |
| `list_project_meetings(project_code)` | `fathom_meetings` | List shape; excludes `transcript`/`summary`. |
| `semantic_search(query, project_code?, limit=10)` | `match_chunks_simple` RPC | Voyage-embedded query. |
| `whoami()` | token claims | Diagnostic. |

**Writes (cp-engine #139 + #140 — inserts, plus one guarded transition):**

| Tool | Target | Notes |
|---|---|---|
| `create_note(project_code, body, title?, recipient_email?)` | `notes` | Via the entities email-bridge; policy enforces author = caller's own entity. |
| `create_commitment(project_code, description, owner_email?, due_date?)` | `commitments` | ISO-validated due date. |
| `create_spine_element(project_code, framing, body, ...)` | `spine_substance` | Version-1 row, `_authored/` convention, author stamped. |
| `add_spine_version(project_code, element_id, body, version_note?)` | `spine_substance` | New vN+1 row (insert), then live→superseded on the prior row via the #142 guarded function. |
| `add_spine_document(project_code, label, content= OR source_title=, type?)` | `spine_substance` | Phase 3 (#140): author a whole document from chat, or spine-card an ingested source with provenance attached at insert. |

**There is still no authenticated UPDATE grant or policy on
`spine_substance`.** The one status transition that versioning needs
(live→superseded on prior siblings of an element whose new live row exists)
lives in `spine_supersede_prior_versions(new_id)` — a SECURITY DEFINER
function that requires team membership, validates the new row, satisfies the
column-guard via a transaction-local `app.spine_writer`, and can do nothing
else. Decided on cp-engine #142 (option 1); team-wide supersede per the same
trust model as a Claude Code session. Not yet mirrored from the engine verb:
the auto-journal step (`spine_steps` has no authenticated INSERT policy).

**Tree (read-only — cp-engine #138, review finding 1):**

| Tool | Source | Notes |
|---|---|---|
| `get_project_state(project_code)` | tenant clone | Exec Summary region of `cp.md` + current (or latest) sprint file. |
| `read_project_file(path)` | tenant clone | Traversal-rejected, size-capped, binary-rejected. |

The clone is shallow, pull-on-read with a 60s debounce, authenticated by a
**read-only** deploy key (`GIT_SSH_KEY` + `TENANT_REPO` env; absent, both tools
degrade to a clean "tree access unavailable"). Note the tree has no per-user
RLS: any team member with a valid JWT reads the whole tenant tree — same
posture as the spine today.

### Column choices, and why

- **`rag_assets` has NO extracted-text column.** Verified against the live
  schema: `id, scope, company_id, project_id, archived_at, promoted_at,
  source_type, title, url, file_path, file_hash, meta, prev_asset_id, status,
  created_at, updated_at, source_provider, source_file_id, source_path,
  initiative_id, author_id`. Document text lives **only** in `asset_chunks.text`,
  so `pull_project_source` concatenates the asset's chunks. `meta` (JSONB, up to
  megabytes per row) is never selected — this is the table the "never `SELECT *`"
  rule was written about.
- **`asset_chunks` has no `chunk_index`.** Its columns are `id, asset_id,
  start_seconds, end_seconds, text, meta, content_hash`. Ordering uses
  `start_seconds` where populated (time-based media) and otherwise falls back to
  the table's natural insertion order, which is the order ingest wrote them.
  Correct in practice for documents, but **not a guarantee the schema makes** —
  a real `chunk_index` (or an ordinal in `meta`) would close this.
- **`fathom_meetings`** mirrors `mc2_db.FATHOM_LIST_COLUMNS`; `transcript` and
  `summary` are the big columns and stay out of the list shape.
- Each `list_*` tool queries **both** `project_id` and `initiative_id` and
  dedupes, because one resolved uuid lands in a different column depending on
  whether the code names an engagement or an initiative.

### `semantic_search` and the broken RPC

`match_chunks_simple` works and is **SECURITY INVOKER**, so PostgREST runs it as
the caller and RLS applies inside it — the same authorization boundary as a
plain table read. That is what makes vector search safe to expose here.

`match_chunks_by_documents` — the RPC that would filter by asset server-side —
is **broken on the live database**. Calling it fails with Postgres `42P01`:

```
relation "assets" does not exist
```

It references a table named `assets`; the real table is `rag_assets`. Until it is
fixed, `project_code` is applied as a **post-filter**: search runs corpus-wide
with an over-fetch (`limit × 20`), then results are intersected with that
project's asset ids. Consequences: the limit applies to the pre-filter candidate
set, so a narrow project can return fewer than `limit` rows, and a deeply-buried
hit can fall outside the window. Fixing the RPC makes this exact and cheap.

---

## Audit logging

Every successful tool call fires a best-effort INSERT into
`public.mcp_audit_log` (`user_id, tool, args, row_count, client, at`), written
**under the caller's own JWT**. The INSERT policy is
`with check (user_id = auth.uid() and is_team_member())`, so a row can only be
written by the caller, about the caller — the audit trail is the user's own
attributable action, not a privileged side channel. `client` is the server
version string (`hosted-cp-spike/0.0.2`).

Two rules hold it in place:

1. **Never log body content.** `args` is passed through an allow-list
   (`sanitize_audit_args`): identifiers and codes (`project_code`, `element_id`,
   `asset_id`, `limit`, `max_chars`) are recorded verbatim; free text
   (`query`) is recorded as a **length only** (`query_len`); anything not
   allow-listed is **dropped**. A future tool that adds a free-text param
   therefore cannot silently start logging user content.
2. **A logging failure must never fail the tool call.** Every path is wrapped
   and downgraded to `log.warning`.

Live sample, read back through PostgREST with the same JWT:

```json
{"tool": "semantic_search",   "args": {"limit": 5, "query_len": 35, "project_code": "ibx-5153"}, "row_count": 5,   "client": "hosted-cp-spike/0.0.2"}
{"tool": "list_project_sources","args": {"project_code": "ibx-5153"},                             "row_count": 116, "client": "hosted-cp-spike/0.0.2"}
```

Note `query_len: 35` where the query text would have been.

---

## The policy model: `auth.users` ≠ team

As of the 2026-08-01 policy pass, read access is **team-keyed**. SELECT on
`spine_substance`, `spine_context`, `asset_chunks`, `asset_embeddings`,
`companies`, `initiatives`, `fathom_meetings`, `commitments`, `notes`,
`rag_assets`, `spine_relations`, and `spine_steps` is gated on
**`public.is_team_member()`** — which is true iff the caller has a row in
`public.profiles`.

The distinction that matters for a hosted server: **being an authenticated
Supabase user is not the same as being a team member.** Anyone who can sign up
gets a valid `authenticated` JWT and will pass token verification cleanly; they
will then read **zero rows from every table**. That is the policy working, not
the server breaking.

Because that failure is silent and uniform, each tool attaches a hint when it
returns empty — if `whoami` succeeds but every read is empty, suspect a non-team
caller. This replaces the old per-table sentinel: `commitments` was previously
**RLS-on-with-zero-policies** (deny-all), and `list_commitments` returned a named
sentinel saying so. That gap is **closed** — a team JWT now gets real rows (31
for `ibx-5153`, of 324 total), and an empty result is a plain "0 rows".

---

## What is proven

Verified live against the real MC-2 project — **13/13 smoke cases pass**, with an
ES256 token for a team user:

| # | Case | Result |
|---|---|---|
| A | No token → 401 | `WWW-Authenticate: … resource_metadata="http://…/.well-known/oauth-protected-resource/mcp"` |
| B | Garbage token → 401 | ✓ |
| C | `alg=none` token → 401 | ✓ — negative test of the ES256 path |
| D | RFC 9728 metadata | 200, `authorization_servers: ["https://<ref>.supabase.co/auth/v1"]` |
| E | `tools/list` | all **8 tools** |
| F | `list_spine_elements(ibx-5153)` | **57 live elements** under the caller's `sub` |
| G | `list_commitments(ibx-5153)` | **31 rows** — team-keyed RLS, no longer deny-all |
| H | `whoami` | verified `sub`, `role=authenticated` |
| I | `list_project_sources(ibx-5153)` | **116 assets**, 6 superseded hidden |
| J | `pull_project_source(...)` | 6 chunks → 7,286 chars of assembled text |
| K | `list_project_meetings(ibx-5153)` | **22 meetings** |
| L | `semantic_search(...)` | **5 hits**, `voyage-3-large`, top similarity 0.637 |
| M | `mcp_audit_log` | 6 rows for the 6 tools called; **no raw query text** |

Case L was additionally run with `VOYAGE_API_KEY` unset: the server starts
normally, logs the reason at boot, and the tool returns
`available: false` with the explanatory message — it degrades rather than
crashing.

The load-bearing control, run separately: the **anon key alone** returns **0**
rows from `spine_substance`; the **anon key plus the user's JWT** returns **57**.
Identity comes from the token, not the key — RLS is genuinely doing the work.

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

- **Spine version supersede** (`add_spine_version`) — see the write-tools
  section; blocked on the guarded-transition decision in cp-engine #139.
- **The full authorize/DCR dance.** Supabase is the AS; this server is a
  resource server only and issues no tokens. It advertises where to get one.
- **Session persistence.** Runs `stateless_http=True`; each request stands
  alone.
- **The complete code resolver.** `resolve_project_id` handles initiative
  codes, the dir-slug, and the `<prefix>-<number>` short form — not the engine's
  full `full_job_name` / companies-number bridge.

## Known gaps this surfaced

1. ~~**`commitments` is deny-all under user JWTs**~~ — **CLOSED** by the
   2026-08-01 team-keyed policy pass. A team JWT now reads real rows.
2. **`match_chunks_by_documents` is broken** — Postgres `42P01`,
   `relation "assets" does not exist` (it should reference `rag_assets`). Forces
   `semantic_search` to post-filter by project instead of scoping in the RPC,
   which makes `limit` approximate for narrow projects.
3. **`asset_chunks` has no `chunk_index`.** Document text can only be reassembled
   in insertion order, which is right in practice but unguaranteed. A
   `chunk_index` column would make `pull_project_source` deterministic.
4. **Team membership is all-or-nothing.** `is_team_member()` is a single
   boolean: every team member reads every project's spine, sources, meetings,
   and commitments. RLS is enforced, but it is not yet *per-project* scoped —
   this spike proves the mechanism, not a multi-tenant policy set.
5. **`spine_substance` has no `updated_at` column.** The spec asked for one; the
   table carries `synced_at`, `version_date`, and `confirmed_at`. The tools
   return `synced_at` as the closest analogue.
6. **Three strings name one project** — `ibx-5153` (short), `ibx-5153-ai-campaign`
   (`spine_substance.project_code`, the cp-tree dir-slug), and `IBX-ai-campaign`
   (`projects.code`). A hosted server needs one shared resolver, or every tool
   re-invents this. Compare the memory note on spine slug drift.
7. **Ingest embeds with Voyage, but the brief assumed OpenAI.** Both keys sit in
   the same `.env`, and only one of them produces meaningful vectors against this
   corpus. Nothing in the schema records which model wrote `asset_embeddings`, so
   the answer had to be recovered by reading `asset_ingest.py` and probing the
   RPC's accepted dimension. A `model` column on the embeddings table would make
   this checkable rather than archaeological.
