# Brief: turn the hosted-cp design into an implementation plan

**For:** whoever writes the implementation plan (next session, likely Fable)
**Date:** 2026-08-01
**Status:** the design is settled; what's missing is a buildable plan

---

## What you're being asked to produce

An implementation plan for hosting cp-engine's MCP server so the team can reach
shared memory from Claude Code, claude.ai, Claude mobile, and (gated) ChatGPT.

**Not** another design. The architecture is decided. What's needed is the thing an
engineer picks up on Monday: ordered work items, explicit dependencies, the
decisions that must be made before code, and where the plan is allowed to fail.

## Read first

**`docs/designs/2026-08-01-hosted-cp-shared-memory.md`** — the full design. Read it
end to end before anything else.

**Important: it is two documents stacked.** The first half is the original plan;
the second half ("Review findings") is an independent architecture review that
**corrects and contradicts parts of the first half**. The review is authoritative
wherever they disagree. Specifically, these first-half claims are superseded:

| First half says | Review corrected it to |
|---|---|
| Phase 1 is read-only MC-2 tools | Read-only **tree** tools also in Phase 1 (finding 1) |
| ~2.5 weeks total | **3.5–4.5 weeks**; Phase 1 alone 2.5–3.5 (finding 8) |
| 3 `create_client` call sites | **1** (finding 3) |
| X-Spine-Writer needs dual-identity design | Trigger has no allowlist; the real risk is the inverse (finding 5) |
| Service-role swap is 2–3 days | Credible for read paths only, not with cache redesign (finding 3) |
| 37 MCP tools | 38 (finding 6) |

**Part of your job is producing a single coherent plan** rather than a plan plus
errata. Do not preserve the two-layer structure.

## Context that is NOT in the design doc

This came out of a working session and hasn't been written down anywhere else.

**The goal in the user's own words** — this is the acceptance criterion:

> "I don't think we would do a lot of authoring on the phone, but it would be great
> to be able to add information or notes directly to the spine and to get
> information or draft documents for a simple use-case. Not an in-depth synthesis
> or strategy session. However the key thing would be to have continuity of the
> information on mobile to quickly look up or ask about something."

**A rejected earlier plan.** The first framing was "host `cp mcp` + build a service
that edits the git tree over HTTP" — ~3–4 weeks, centrepiece a tree-editing service
with concurrency control, conflict handling and a write allowlist. It was rejected
because the use case above doesn't need tree *editing*. Don't re-propose it. (But
note the review's finding 8: the honest revised estimate lands back in that range
anyway, with risk moved from concurrency to auth. Say so plainly rather than hiding
it.)

**Why the user wants this at all** — the recurring failure it addresses is that
Marcello's local CP doesn't push, so his authoring never reaches the spine. Hosting
removes each person's install from the equation. Worth keeping in view when
weighing "does stdio `cp mcp` survive?" (review finding 7 — code drift).

**Team shape:** 13 users in `auth.users`, Google SSO, active. Partners are Drew,
Marcello, Tony, Brandon. Freelancers (Jack, Derek, Kyle) work single engagements
and are the reason `project_members` needs modelling now even though policies ship
permissive.

## Decisions already made — do not reopen

1. **Content-type split.** Structured state in Postgres (client-agnostic); long-form
   prose stays in git, authored in Claude Code. Moving the tree into the DB was
   considered and rejected.
2. **Authorship going forward only.** Add `author_id` to `spine_substance`; history
   stays NULL. Per-project backfill later only where provenance matters. "We don't
   know who wrote this" is honest; a plausible backfill is not.
3. **Everyone reads everything for now** — but land the `project_members` schema in
   Phase 1 so freelancer restriction is later a policy change, not a migration.
   Grants are **per-project**; account-scoped elements need a **separate** grant.
4. **Codex is not a requirement.** A want. Do not design for it; it is configuration
   post-Phase-1.
5. **Read-only tree access is in Phase 1** (review finding 1).

## Open questions the plan must resolve or explicitly defer

- **The OAuth spike.** The recommendation is 2 days before committing a number.
  Decide whether the plan front-loads it or assumes it. Supabase Auth is an IdP,
  not an OAuth 2.1 AS with dynamic client registration — the shim is unnamed work.
- **Does stdio `cp mcp` survive?** If it does and forks from the hosted server,
  every future verb lands twice. Shared tool module, or retire stdio?
- **`commitments` has 49 rows with null `project_id`.** Per-project policies make
  them invisible to restricted users by default. Intended?
- **ChatGPT is a governance decision, not technical.** The tenant holds SAP,
  Google, Infoblox and Salesloft confidential material. The plan should state what
  must be true before wiring it — the review notes an audit log is a precondition
  for even having that conversation.

## Verify, don't trust

Every empirical claim in the design was checked once, but re-verify anything your
plan's sizing depends on.

- **Codebase:** `/Users/drewf/Documents/Python/cp-engine/` — `src/cp_engine/mcp_server.py`
  (38 tools; ~9–11 filesystem-coupled via `_tenant_root()`),
  `src/cp_engine/mc2_db.py:586-636` (the single cached, header-mutating client
  constructor), `webhook/` (working Railway service that clones the tenant with a
  deploy key and pushes back — the deployment precedent).
- **Database:** Supabase project `mgheymslksfyhuvhmvmj` via
  `mcp__plugin_supabase_supabase__execute_sql`. The live-exposure findings
  (`projects` open CRUD, and RLS-on-zero-policies on
  `commitments`/`notes`/`rag_assets`) are worth seeing yourself — they change what
  "flip to JWT-proxying" means operationally.
- **Tenant:** `/Users/drewf/Documents/Python/cp` — ~1,363 markdown files.

## Second-pass verification findings (2026-08-01, after the review)

A second independent pass re-verified every review claim against the code and
live DB. All held. Two things the review UNDERSTATED or missed — fold both into
the security work item:

1. **[CORRECTED 2026-08-02]** The original version of this finding claimed
   `public.projects` had a full open-CRUD policy set. Wrong table: the
   `USING(true)` CRUD set is on **`estimator.projects`** (the Estimator
   extension table — a schema-unfiltered `pg_policies` query conflated the
   two). `public.projects` had exactly the five policies mc-2 #276
   described, and #276's migration (applied 2026-08-02) resolved them:
   partner-roster UPDATE policy + column grant narrowing browser writes to
   `start_date`. Still open: `estimator.projects` `DELETE USING(true)` —
   any authenticated user can delete estimate rows; needs its own decision
   (noted on #276).

2. **[PARTIALLY RESOLVED 2026-08-02: `match_chunks_simple` and
   `match_chunks_by_documents` flipped to SECURITY INVOKER; the other 18
   definer functions still need the audit.]**
   **20 SECURITY DEFINER functions in `public`, unmentioned in the design.**
   Definer functions run with owner privileges and bypass RLS regardless of the
   caller's JWT. Two are on this plan's read path: `match_chunks_simple` and
   `match_chunks_by_documents` — the vector-search RPCs Phase 1's semantic search
   will likely call. Consequence: even after JWT-proxying, semantic search
   ignores RLS — so the promised "freelancer restriction is later a policy
   change, not a migration" is FALSE for search unless these become
   `SECURITY INVOKER` (or take an explicit access filter). Widen the audit line
   item to "every policy AND every SECURITY DEFINER function."

Also: `notes` is in the deny-all group and Phase 2's `create_note` needs an
INSERT policy — the design only discusses that group's read-side implications.
**[RESOLVED 2026-08-02: team-keyed read policies landed on the whole deny-all
set (plus `spine_relations`/`spine_steps`, which the design also missed), and
insert-only write policies landed for Phase 2. See migrations
`hosted_cp_phase1_membership_and_team_reads` and
`hosted_cp_phase2_author_id_and_write_policies`.]**

## What a good plan looks like here

- **Ordered work items with explicit dependencies**, not phases-as-buckets. The
  review found `author_id` sitting in Phase 1 while nothing in Phase 1 stamps it —
  that class of error is what dependency-ordering prevents.
- **A separate security work item.** The `projects` exposure is live and is
  currently inert only because everything uses service-role. It should not be
  buried inside "Phase 1 auth."
- **Explicit failure modes.** `list_commitments` returning *empty rather than
  error* under a user JWT is the kind of thing that must be a test, not a surprise.
- **Honest sizing with named uncertainty.** Where the estimate is a guess, say it
  is a guess and say what would settle it.
- **A first shippable increment.** What is the smallest thing that delivers real
  mobile continuity? That is the thing to build first.
