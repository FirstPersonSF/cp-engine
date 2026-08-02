# Hosted cp — shared team memory across LLM clients

**Date:** 2026-08-01
**Status:** design, not started
**Goal:** one shared memory + project space the team reaches from Claude Code,
claude.ai, Claude mobile, ChatGPT and Codex.

---

## What this is not

An earlier framing of this was "host `cp mcp`" — port the stdio server to HTTP and
teach a hosted service to edit the git tree so every client could do what Claude
Code does. That plan was ~3–4 weeks and its centrepiece was a tree-editing service
with a concurrency model, conflict handling and a path allowlist.

**That plan solved the wrong problem.** Drew's actual use case, stated plainly:

> "I don't think we would do a lot of authoring on the phone, but it would be great
> to be able to add information or notes directly to the spine and to get
> information or draft documents for a simple use-case. Not an in-depth synthesis or
> strategy session. However the key thing would be to have continuity of the
> information on mobile to quickly look up or ask about something."

Continuity of information — reads — is the key thing. Narrow writes second. Long-form
authoring stays in Claude Code where diffs and review work. No tree-editing service
is required, and the riskiest phase of the old plan drops out entirely.

## What already exists

The substrate is largely built. Live counts as of 2026-08-01:

| Store | Rows |
|---|---|
| `spine_substance` (substantive, >200 chars) | 247 of 371 |
| `commitments` | 324 |
| `rag_assets` | 465 (435 chunked) |
| `asset_chunks` / `asset_embeddings` | 4,727 |
| `projects` | 45 |
| `auth.users` | 13, Google SSO, active |

**The read path for the primary use case is already built.** Semantic search over
4,727 embedded chunks, 247 substantive spine elements, and the full commitments
store all exist. The only reason a phone cannot reach them is that `cp mcp` speaks
stdio and requires a local checkout.

**This is a transport + auth project, not an architecture project.**

## Architecture: split by content type

Structured state lives in MC-2 and is client-agnostic. Long-form authored documents
stay files, edited in Claude Code.

Of 1,466 tenant markdown files:

- 263 `spine/` files are already MC-2 mirrors
- 73 are generated (`cp.md`, `master-cp.md`)
- 456 sprint files are semi-structured
- 50 session captures are append-only records
- 43 transcripts are blobs (Fathom's already in Supabase)
- **430 are genuinely handwritten long-form** — the irreducible tree

The tenant is ~60% already-mirrored, generated or schema-shaped. The stubborn 30%
is prose, and prose is what Claude Code is good at.

### The habit this implies

Today's most valuable output (`board-record-companion.md`) is a *file* — so it is
invisible from mobile until authored into the spine. `add_spine_document` already
does this, and was used at wrap-up on 2026-08-01.

**Rule: if it matters later, it goes in the spine.** Not just the tree. This is a
working practice, not a build item, and it is what makes mobile continuity real.

---

## Authorship (decided)

**Decision (Drew, 2026-08-01): authorship on spine elements going forward. No
blanket backfill — attribute history project-by-project only where it matters.**

### The gap

`spine_substance` has **no authorship column**. Not `author_id`, not `created_by`.
371 elements, no record of who wrote any of them. Same for `rag_assets`.
`commitments` has `owner_email`/`owner_name` (who *owes*, not who *recorded*), and
`notes` has `author_id`.

This is the real blocker for *shared team* memory — not "can we identify users"
(Google SSO already works) but "does the memory know who contributed what."

Why it matters, from today's session: the polished vision statement on the SAP-5174
Our Story board turned out to be **Marcello's synthesis, not the client's words** —
load-bearing enough to flag as a client-facing risk. That had to be reconstructed
from a transcript and confirmed with Drew. Had it been authored into the spine with
an author, it would have been a property of the data.

### Migration

`spine_substance` already carries `confirmed_by:text` — a precedent for identity on
these rows. Versions live *in* `spine_substance` (each version is a row), so an
author column gives per-version authorship for free.

```sql
alter table spine_substance add column author_id uuid references auth.users(id);
alter table rag_assets     add column author_id uuid references auth.users(id);
```

History stays NULL. **"We don't know who wrote this" is honest; a plausible
backfill is not.** Per-project backfill is available later where provenance is
worth reconstructing.

---

## Plan

### Phase 1 — hosted read server + auth (~1.5 weeks)

The phase that delivers the stated goal.

**Transport.** `mcp.run(transport="stdio")` → HTTP/SSE. The `@mcp.tool()`
decorators are unchanged; 28 of 37 tools are pure MC-2/network calls and port with
no logic change. Deploy on Railway beside `cp-engine-webhook` (shares Supabase
creds, deploy key, and an already-proven deployment path).

**Tool surface.** Read-only to start: `list_spine_elements`, `pull_spine_element`,
`list_commitments`, `list_project_sources`, `pull_project_source`,
`list_project_meetings`, plus semantic search over `asset_embeddings`.

**Auth — the substantive work.**

1. Add `author_id` per the migration above.
2. **The server takes the user's JWT, not the service-role key.** This is the real
   change. Today `cp mcp` connects with service-role and bypasses RLS entirely.
   Hosted, it must proxy a user session so policies apply. Expect to audit every
   code path that assumes it can read anything.
3. OAuth in the MCP handshake — claude.ai and ChatGPT both support it; `.1p.is`
   SSO plugs in here.
4. RLS policies for reads — **permissive within the team, but on a
   `project_members` schema** so freelancer restriction is a later policy change,
   not a migration. Account-scoped elements need a separate grant from
   project-scoped ones (see Resolved question 1).

**`_tenant_root()` must go.** It walks up from cwd to find `.cp-engine.toml`;
server-side there is no cwd. Read tools that need config resolve it from app state.

### Phase 2 — narrow writes (~0.5 week)

`create_spine_element`, `add_spine_version`, `create_note`, `create_commitment`.
All already in the clean 28 — no filesystem coupling. Each write stamps `author_id`
from the JWT. This is "add a note or observation to the spine from my phone."

### Phase 3 — simple drafting (~0.5 week)

Read spine context → draft → write back. Easy once 1 and 2 land. Requires
`add_spine_document` to grow a `content=` form (today it takes `file_path`, and
there is no file on a phone).

Explicitly **not** in scope: in-depth synthesis, strategy sessions, long-form
deliverables. Those stay in Claude Code.

### Phase 4 — ChatGPT / Codex (days, gated on a decision)

Spec-compliant remote MCP mostly works across clients once auth is settled.

**Governance gate, not a technical one.** The tenant holds SAP, Google, Infoblox
and Salesloft confidential material. Exposing it to a second vendor's chat client
is a deliberate decision. Reads are a softer question than writes, but it is still
a decision — make it explicitly rather than discovering it after wiring.

## Sizing

| Phase | Estimate | Unlocks |
|---|---|---|
| 1 — hosted reads + auth | ~1.5 wk | **Mobile continuity — the stated goal** |
| 2 — narrow writes | ~0.5 wk | Add notes/observations from anywhere |
| 3 — simple drafting | ~0.5 wk | Quick drafts from spine context |
| 4 — ChatGPT/Codex | days | Second/third client (gated) |

**~2.5 weeks**, versus 3–4 for the tree-editing plan — and without its riskiest
component.

## Resolved questions (2026-08-01)

### 1. Read scope — everyone reads everything, but model restriction now

**Decision (Drew): everyone can read for now; freelancers may need restricted
access later.**

Ship permissive policies, but land the *schema* in Phase 1. Retrofitting access
control means auditing every existing row for what a restricted user must not see;
modelling it up front costs a `project_members` table and policies keyed on it, and
tightening later becomes a policy change rather than a migration.

**The distinction worth getting right now: per-project vs per-account.**
Freelancers (Jack, Derek, Kyle) work on specific engagements — but the spine has
**account-scoped** elements that span a company's projects (`scope='account'`,
promoted stakeholder dossiers, `set_element_account_scope`). A freelancer on
`ggl-5177` must not automatically inherit every Google account dossier. Model the
grant as per-project, and make account-scoped elements require a separate grant.
Hard to add later, cheap now.

### 2. Codex — not a requirement, do not design for it

**Decision (Drew): "might not be necessary, but nice to sometimes have additional
tools for synthesis or analysis."**

That is a want, not a requirement, and it should not shape the design. A
spec-compliant remote MCP server works with any compliant client — so once Phase 1
ships, pointing Codex at it is configuration, not a phase. Try it afterwards.

Keep it separate from the ChatGPT decision: both are governance calls about a
second vendor, but the pull toward Codex is much weaker than the pull toward mobile
continuity, and it must not gate anything.

### 3. Service-role audit — smaller than first assessed

**Not a Dropbox/Google question.** Those are separate credential paths
(`_load_dropbox_creds`, `_load_ingest_creds`) used to fetch source documents.
Service-role is purely the *Supabase* connection.

`SUPABASE_SERVICE_KEY` bypasses Row Level Security entirely — every table, every
row, regardless of policy. That is correct today because invoking it requires the
key on your laptop.

Hosted, it breaks: if the server holds the service key and the user only
authenticates *to the server*, RLS never runs and the policies are decorative —
access control collapses into application code. The server must instead take the
user's JWT and pass it to Supabase so Postgres enforces policy.

**Good news on sizing.** Every MC-2 call funnels through one client constructor in
`mc2_db.py` (`create_client`, 3 call sites). There is no scattered service-role
usage to untangle.

**One wrinkle.** The constructor stamps `X-Spine-Writer: cp-engine` on every
client — a DB trigger (mc-2 #130) rejects UPDATEs to engine-owned columns
(`body`/`status`/`origin`) unless that header names an authorised writer. Per-user
connections must carry *both* identities on the same request: the acting user (for
RLS and `author_id`) and the engine (for the column guard). Solvable, but design it
rather than discover it.

**Revised estimate: 2–3 days, not a week.**

## What this deliberately does not do

- No tree-editing service, no clone-per-session, no concurrency model.
- No migration of the tree into MC-2. Git keeps history, review and offline work
  for long-form prose.
- No backfill of historical authorship.
