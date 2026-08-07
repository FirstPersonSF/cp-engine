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
  `TENANT_REPO`. **RLS cannot scope a git clone**, so both are gated on
  `public.is_team_member()` instead — see [Tree](#tree-read-only--cp-engine-138-review-finding-1).

43 tools (the 36 through spec-v04, + the #159 commitments-lifecycle trio, + the #125 read verb `list_spine_relations`, + the #138-ratchet ports `archive_project_source` / `rename_project_source` (mig-134 guarded fns) / `pull_element_from_project`), **53/53 smoke cases pass** as of the last full run before the #159/#125/#138 additions.

---

## Deploy it

Live at **`https://cp.mc-2.1p.is/mcp`**, served by the `hosted-mcp` service in
the **Mission Control** Railway project (not the separate, dead `hosted-cp`
project). **The service has no GitHub connection — pushing to `main` deploys
nothing**; deploys are local-source uploads via `railway up` from this
directory. Full IDs, the deploy command, and the env-var contract are
documented in [`railway.toml`](railway.toml).

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
| `MC2_API_BASE` | for promotion | `https://api-production-a247.up.railway.app` | mc-2's backend, where `promote_spine_transcript` delegates. Already set on the deployment. Absent/blank, the verb still exists and returns a clean `promotion unavailable: MC2_API_BASE not configured`. |
| `MC2_TIMEOUT_SECONDS` | no | `120` | Promote is webhook-proxied inside mc-2, so it is slower than a DB write. Must not be tighter than mc-2's own timeout, or a still-succeeding promotion gets reported as a timeout. |

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

All forty-three run under the caller's identity, select **explicit columns
only**, and write an audit row on success.

**Reads:**

| Tool | Source | Notes |
|---|---|---|
| `list_spine_elements(project_code)` | `spine_substance` | Live, non-archived elements. |
| `list_spine_relations(project_code, element_key?)` | `spine_relations` | Active edges — one element's (both directions, retired-tolerant key) or the whole project's; endpoints annotated with live framings (#125). |
| `pull_spine_element(element_id, project_code?)` | `spine_substance` | Body + metadata; newest version wins. |
| `list_commitments(project_code)` | `commitments` | Real rows since the team-keyed policy pass. |
| `list_project_sources(project_code)` | `rag_assets` | Manifest shape; drops superseded assets. |
| `pull_project_source(asset_id, max_chars=40000)` | `asset_chunks` | Assembles document text — see below. |
| `list_project_meetings(project_code)` | `fathom_meetings` | List shape; excludes `transcript`/`summary`. **Returns the uuid `id` as `meeting_id`, NOT `recording_id`** — see batch 5. |
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

**Updates + deletes (cp-engine #143 batch 2):**

| Tool | Target | Notes |
|---|---|---|
| `set_spine_element(project_code, key, important?, note?, layer?, framing?, serves?)` | `spine_substance` | Partial update of the **live row only**. An `important` false→true flip fires the delegated promotion non-fatally (batch 5). |
| `resolve_commitment(project_code, key, outcome='done')` | `commitments` | `done`\|`dropped`; ambiguous key returns candidates, never guesses. |
| `set_spine_step(project_code, key, step_id, title?, status?, step_date?, note?)` | `spine_steps` | Partial update; scoped by (id, project_id, est_item_id). |
| `reorder_spine_step(project_code, key, order)` | `spine_steps` | `order` is the FULL step_id list; renumbers 1..N. |
| `remove_spine_step(project_code, key, step_id)` | `spine_steps` | Deletes, then densifies positions to 1..N. |

**Sources + provenance (cp-engine #143 batch 3):**

| Tool | Target | Notes |
|---|---|---|
| `add_element_source(project_code, key, source_title)` | `spine_substance.sources` | Attaches `{type: rag_asset, id, title}` to **every version**. |
| `remove_element_source(project_code, key, source_title)` | `spine_substance.sources` | Inverse; detaching an unattached link is a note, not an error. |
| `add_element_provenance(project_code, key, source_key)` | `spine_substance.sources` | Attaches `{type: spine_element, id, title, retired}`. Source **may be retired**. |
| `remove_element_provenance(project_code, key, source_key)` | `spine_substance.sources` | Inverse. |

**Retire + account scope (cp-engine #143 batch 4):**

| Tool | Target | Notes |
|---|---|---|
| `retire_spine_element(project_code, key)` | `spine_substance` + `spine_relations` | Archives **every** version, supersedes the live row, and **deletes** the element's typed edges (#96). Returns `edges_removed`. |
| `retire_spine_elements(project_code, keys)` | same | Batch form (#105). **Per-key results; a bad key does not abort the batch.** |
| `retire_spine_relation(project_code, kind, from_key, to_key)` | `spine_relations` | Direct filtered DELETE. Resolution **tolerates a dead endpoint** — an unresolvable key is used verbatim as an est_item_id. |
| `promote_stakeholder(project_code, key)` | `spine_substance` | → `scope='account'` + `company_id`. Engagements only. Non-Stakeholders layer ⇒ `warning`, promotion still applies. |
| `demote_stakeholder(project_code, key)` | `spine_substance` | Inverse: `scope='project'`, `company_id` cleared, back to the provenance project. |
| `set_element_account_scope(project_code, key, account=True)` | `spine_substance` | Type-agnostic generalization; **delegates** to the two above, minus the layer warning. |

**Transcript promotion (cp-engine #143 batch 5):**

| Tool | Target | Notes |
|---|---|---|
| `promote_spine_transcript(project_code, key)` | mc-2 `POST /api/meetings/{recording_id}/promote-transcript` | **Delegated, not ported.** `key` accepts a recording_id, a meeting uuid, or a spine element key; the return names which matched via `resolved_via`. |
| `set_spine_element(..., important=True)` | same, fired non-fatally | Closes the batch-2 gap: a genuine `important` false→true flip now fires the delegated promotion and reports it under `promotion`. |

#### Why batch 5 delegates instead of porting

The engine's `promote_spine_transcript` is the one verb that **cannot** be
ported. It resolves an element's `rel_path` to a file in a local tenant
checkout, copies it to a stable temp path, and runs the full ingest pipeline —
Voyage embeddings plus a **service-key** write to `rag_assets`. A hosted server
has no authoritative tenant checkout and, by the entire premise of this
prototype, no service key.

mc-2's backend already performs this promotion service-side, and — the fact
that makes delegation clean rather than a workaround — **its auth is the same
Supabase JWT this server verifies.** `mc-2/backend/src/auth.py` validates ES256
against the same project JWKS with `aud=authenticated`. So the hosted verb
forwards the **caller's own bearer token** and the promotion runs as that user
end to end. This server never mints authority it wasn't given, and never
becomes a confused deputy.

Two consequences, both surfaced in the return rather than hidden:

1. **It is a different promotion universe.** The engine promotes a tenant
   **file** (landing `source_provider='spine-promote'`); mc-2 promotes a
   **meeting** (landing `source_provider='fathom'`). Verified live: 2
   spine-promote assets vs 171 fathom ones, and **no** live spine element's
   `rel_path` points at a transcript — every one points at a `spine/<activity>/
   <deliverable>.md`. These are not one operation under two names.
2. **`fathom_meetings` has two ids.** A uuid `id` and a bigint `recording_id`.
   `list_project_meetings` returns the **uuid**; the mc-2 endpoint takes the
   **bigint**. Piping one tool into the other hands over the wrong id and fails
   as a silent upstream 404. `resolve_recording_id` accepts either and
   translates, so the gotcha cannot reach the endpoint.

The element→meeting bridge is deliberately narrow: no column links a spine
element to a `fathom_meetings` row, so the verb follows the element's **cited
sources** — an ingested meeting leaves a `rag_assets` row whose
`source_file_id` is the recording_id as text. That citation is an exact link,
not a guess. An element citing no Fathom source returns a clean note; for most
elements that is the shape of the world, since their substance is
document-derived.

The engagement-only guard is mirrored verbatim from
`spine_promote.promote_transcript`'s Contract A, and it is not academic:
**zero** `fathom_meetings` rows carry an `initiative_id` (verified live), so an
initiative has no meeting to promote even in principle.

Like batch 3, the two batch-4 mutations are guarded SECURITY DEFINER calls
rather than table policies — shipped by `ratchet_batch4_retire_and_scope_fns`:

```
spine_retire_element(p_project_id uuid, p_est_item_id text)
    returns jsonb {versions, edges_removed}
spine_set_element_scope(p_project_id uuid, p_est_item_id text,
                        p_account boolean) returns integer
```

The reason is stronger here than in batch 3. The stdio originals do their work
as **multi-statement sequences** run with the service key — retire is four
statements (archive every version, demote the live row, then a delete per edge
direction). A sequence that fails halfway leaves an element archived-but-live,
or edges dangling from a dead endpoint: exactly the graph corruption #96 was
filed about. Folding each into one function makes it one transaction, and makes
the guards (live-element required, engagements-only, the sibling-twin check)
unbypassable rather than advisory.

The **third** mutation needs no function: batch 4's migration also added a team
DELETE policy on `spine_relations`, so `retire_spine_relation` is a direct
filtered delete. The row is fully identified by `(project_id, kind,
from_item_id, to_item_id)` and RLS is the entire authorization story.

Both functions signal refusals with `RAISE EXCEPTION` (P0001) prefixed by the
function name. `_guarded_fn_error` digs the message out of postgrest-py's
APIError and strips that prefix, so a caller reads *"engagements only — this
project has no company"* rather than a JSON blob. Two refusals are translated
**before** the call instead, because they are not failures: promoting on an
initiative, and demoting something that is not account-scoped (the function
would move zero rows) both return a structured `note`.

Two things worth knowing about the retire semantics:

- **Retiring does not destroy lineage.** `add_element_provenance` writes the link
  into the *surviving* card's `sources`, so retiring the raw card it absorbed
  keeps the trail — and a link attached *after* retirement rides `retired: true`.
  This is the pairing that closes gap 8 below.
- **Edges cascade, versions archive.** Nothing is deleted from `spine_substance`;
  the element is recoverable via a dashboard un-archive. Only `spine_relations`
  rows are actually removed, because an `active` edge from a dead endpoint is a
  graph an agent would still walk.

These do **not** fit a table policy at all — there is no authenticated grant on
`spine_substance.sources`, so a direct PATCH is impossible, not merely
discouraged. The whole mutation is one guarded SECURITY DEFINER call shipped by
`ratchet_batch3_element_sources_fn`:

```
spine_element_modify_source(p_project_id uuid, p_est_item_id text,
                            p_entry jsonb, p_add boolean) returns integer
```

It validates team membership, the entry shape (`type ∈ rag_asset|spine_element`,
`id` present, `title` required on add), that the referent exists (a `rag_asset`
by uuid; a `spine_element` by est_item_id within the project, at **any** status),
dedupes by `(type, id)`, and applies to every version row. The tools do
resolution and reporting; the function is the authorization boundary.

That constraint **closes** a gap batch 2 left open. `set_spine_element` is
live-rows-only (its UPDATE policy says `status='live'`), so element-level fields
diverge across an element's history. These verbs have no such divergence — the
function writes every version, so hosted and stdio agree exactly. Smoke cases Z
and Z5 assert `versions_updated=2` and read **both** the live and the superseded
row back through PostgREST to prove it.

Two resolution asymmetries worth knowing:

- **`source_title` → an ACTIVE rag_asset** — exact title first, else a unique
  case-insensitive substring where the query is a substring of the stored title
  (the engine's direction, `query ⊆ stored`). Superseded assets are dropped, as
  in `list_project_sources`. Ambiguity returns the candidate titles and never
  guesses.
- **`source_key` → an element that MAY BE RETIRED.** This is the one resolver on
  the server that deliberately does not filter to live/unarchived rows, because
  the provenance case (#104) *is* "fold a now-retired raw card into the synthesis
  card that absorbed it". The target `key`, by contrast, must be live. The
  `retired` flag rides the link, so lineage survives the source's retirement.

These fit three policies added by `ratchet_batch2_update_verb_policies`:
`spine_substance` UPDATE `using/with check (is_team_member() AND status='live')`;
`commitments` UPDATE `using (status='open')` / `with check (status IN
('done','dropped'))`; `spine_steps` UPDATE+DELETE `using (is_team_member())`.

Two deliberate semantic gaps versus the stdio verbs, both surfaced in the tools'
own returns so a caller cannot mistake them for success:

1. **`set_spine_element` is live-rows-only.** The engine verb writes
   `layer`/`framing`/`serves` to *every* version of an element, because those are
   element-level facts and a partial write scatters one element's history (#47).
   The hosted UPDATE policy is `status='live'`, so superseded rows are
   unwritable here and keep their prior values. The return reports
   `versions_updated` and `superseded_untouched`. Use the stdio verb when the
   whole history must move.
2. **Transcript promotion is DELEGATED, and usually skips.** *(Closed in batch
   5 — this was an open gap through batch 4.)* Like the engine verb, a genuine
   `important` false→true transition fires a promotion, engagement-only and
   strictly **non-fatal**: its outcome lands under `promotion` as a dict
   carrying `fired`, and can never turn the metadata write into an error.
   What differs is *what* is promoted — the engine embeds the tenant file at
   the element's `rel_path`, while this fires mc-2's promotion for the Fathom
   recording behind the element. An element citing no Fathom source — which is
   **most** of them — gets `{"fired": false, "skipped": "…"}` with the reason,
   not a failure. Use the stdio verb when the tenant **file** is what must be
   embedded.

### Where the engine-owned column boundary actually lives

Worth stating precisely, because it is **not** what #143's brief assumed, and
the difference is load-bearing for anyone reasoning about this server's safety.

The migration's per-column grant `UPDATE(important, note, layer, framing,
serves)` on `spine_substance` is real but **inert**. The table-level ACL already
carries `authenticated=arwdDxtm` — a blanket table-wide UPDATE — and Postgres
**unions** table- and column-level grants rather than intersecting them. The
narrow column grant therefore subsumes into the broad one and denies nothing.
Verified by reading `pg_class.relacl` and `pg_attribute.attacl` directly.

What actually stops a `body`/`status`/`origin` write is the **mc-2 #130
column-guard trigger** (`spine_substance_column_guard`), raising SQLSTATE
`P0130`. Probed live under the smoke user's JWT:

| Direct PostgREST PATCH | Result |
|---|---|
| `body` | **500 `P0130`** — denied |
| `status` | **500 `P0130`** — denied |
| `origin` | **500 `P0130`** — denied |
| `important` | 200, 1 row — allowed |

**But that trigger is an *attribution* guard, not an authorization boundary.**
It only requires the writer to name itself, and an `X-Spine-Writer` header
satisfies it. Confirmed live: the same `body` PATCH that fails with `P0130`
**succeeds (200)** when `X-Spine-Writer: smoke-test` is set. So engine-owned
columns are protected from *accident*, not from *intent* — any client holding a
team JWT can rewrite element bodies by adding one header.

Closing that would mean **revoking the table-wide UPDATE grant** on
`spine_substance` from `authenticated`, which is what makes the column grant
load-bearing. No DB change was made by this batch; smoke case W2 asserts the
`P0130` denial and records the header bypass as a finding.

**There is still no authenticated UPDATE grant or policy on
`spine_substance`** *for engine-owned columns* (`body`/`status`/`origin` — see
the trigger caveat directly above). The one status transition that versioning needs
(live→superseded on prior siblings of an element whose new live row exists)
lives in `spine_supersede_prior_versions(new_id)` — a SECURITY DEFINER
function that requires team membership, validates the new row, satisfies the
column-guard via a transaction-local `app.spine_writer`, and can do nothing
else. Decided on cp-engine #142 (option 1); team-wide supersede per the same
trust model as a Claude Code session. The auto-journal step IS mirrored
(#145): `spine_steps` has authenticated team-member INSERT/UPDATE/DELETE
policies, and the verb upserts the same review-gated one-auto-step-per-
element-per-day record as the engine, with `step_title` parity.

**Tree (read-only — cp-engine #138, review finding 1):**

| Tool | Source | Notes |
|---|---|---|
| `get_project_state(project_code)` | tenant clone | Exec Summary region of `cp.md` + current (or latest) sprint file. |
| `read_project_file(path)` | tenant clone | Traversal-rejected, size-capped, binary-rejected. |

The clone is shallow, pull-on-read with a 60s debounce, authenticated by a
**read-only** deploy key (`GIT_SSH_KEY` + `TENANT_REPO` env; absent, both tools
degrade to a clean "tree access unavailable").

**The tree is gated on team membership, not RLS.** It is a git clone, so
PostgREST is not in the path and RLS cannot scope it the way it scopes every DB
verb here. Both tools therefore call `caller_is_team_member()` first, which
RPCs `public.is_team_member()` under the caller's own JWT — the same predicate
(`exists (select 1 from public.profiles where id = auth.uid())`) the spine,
notes, and commitments policies use. The database renders the verdict; this
server never reimplements membership. A non-member gets "tree access denied";
any error denies (fail-closed).

This gate became load-bearing on 2026-08-02, when Supabase dynamic client
registration was enabled so MCP connectors could self-register (cp-engine
#144). DCR is open registration by design — anyone who can reach GoTrue can
mint a client and authenticate. RLS still zeroes a stranger's DB reads, but
without this gate the tree tools would have served them the whole tenant repo,
client engagement content included.

Within the team the tree remains unscoped: any member reads any file — the same
posture as the spine today (see known gap 4).

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
   therefore cannot silently start logging user content. Batch 2 added only
   identifier-like keys (`step_id`, `outcome`, `order_len`); `note`, `framing`,
   and `title` stay redacted-to-length. **`order` is deliberately absent from
   both lists** — it is a list of uuids, so a reorder is audited as *how many*
   steps moved (`order_len`), never as which.

   Batch 3 added `source_title` and `source_key` **verbatim**, which is worth
   the argument since `title` right beside them is redacted. Neither is the
   caller's prose: `source_title` is a lookup key naming an already-ingested
   document (authored elsewhere, and resolved to an `asset_id` logged next to
   it), and `source_key` is an element key like the already-allowed
   `key`/`from_key`/`to_key`. Redacting them to a length would make the row
   strictly less useful — you would know a source was attached but not which —
   while protecting nothing the `asset_id` does not already reveal. `title`,
   text the caller is *writing*, stays redacted. Smoke case M asserts both keys
   are actually present, so the choice is visible in the run and not only here.
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

Verified live against the real MC-2 project — **53/53 smoke cases pass**, with an
ES256 token for a team user. The table below covers the original read surface;
the write, tree, update, sources/provenance, retire/scope, and promotion cases (N–AK) are
listed in
`smoke_test.py`'s docstring and run in the same suite:

| # | Case | Result |
|---|---|---|
| A | No token → 401 | `WWW-Authenticate: … resource_metadata="http://…/.well-known/oauth-protected-resource/mcp"` |
| B | Garbage token → 401 | ✓ |
| C | `alg=none` token → 401 | ✓ — negative test of the ES256 path |
| D | RFC 9728 metadata | 200, `authorization_servers: ["https://<ref>.supabase.co/auth/v1"]` |
| E | `tools/list` | all **36 tools** (count asserted, so an unexpected extra fails too) |
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
7. **The `spine_substance` column grant is inert, and the trigger guarding
   engine-owned columns is bypassable by a header.** The table-wide
   `authenticated=arwdDxtm` grant subsumes the narrow per-column grant (Postgres
   unions them), so the only thing stopping a `body`/`status`/`origin` write is
   the mc-2 #130 trigger — which an `X-Spine-Writer` header satisfies. Verified
   live in both directions. Fix: revoke the table-wide UPDATE grant so the column
   grant becomes load-bearing. See "Where the engine-owned column boundary
   actually lives" above.
8. ~~**The retired-source provenance path is UNTESTED here.**~~ **CLOSED by
   batch 4.** The gap was that `add_element_provenance` resolves `source_key`
   across retired elements by design (#104) — its main use — but the server
   exposed no retire verb, so the suite could not produce a retired element and
   case Z5 could only assert `retired: false`. `retire_spine_element` now ships,
   and **case AA** exercises the contract end to end: attach provenance while
   the source is live, retire the source, assert the link on the **survivor**
   still exists (surviving retirement is the entire point of storing the link on
   the survivor), then attach the now-retired element to a third element and
   assert `retired: true` rides the new link. Both directions of the flag are
   now covered by the hosted suite rather than only by the stdio verb.
9. **No internal project has ANY ingested sources.** Every other write case
   targets `mission-control` to keep mutations off client engagements, but
   `rag_assets` are a client-engagement artifact in this corpus — checked live,
   `mission-control` and every other initiative have zero. So smoke cases Z–Z4
   create their own `smoke-test-` element inside `$PROJECT_CODE` and attach
   there. No client-authored row is mutated (only the smoke element's own
   `sources`, which Z4 detaches), but the isolation the other write cases enjoy
   is not available for a real source attach. Ingesting one document against an
   initiative would restore it.
10. **Ingest embeds with Voyage, but the brief assumed OpenAI.** Both keys sit in
   the same `.env`, and only one of them produces meaningful vectors against this
   corpus. Nothing in the schema records which model wrote `asset_embeddings`, so
   the answer had to be recovered by reading `asset_ingest.py` and probing the
   RPC's accepted dimension. A `model` column on the embeddings table would make
   this checkable rather than archaeological.
