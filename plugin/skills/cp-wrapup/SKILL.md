---
name: cp-wrapup
description: Run the per-session `wrap up` ritual in a cp tenant — refresh each touched project's Exec Summary, sweep cross-cutting decisions and open commitments, run spine + word-count checks, then commit and push. Carries a hosted-session branch for sessions that reach the tenant only through the `cp-hosted` MCP server, with no `cxp` and no file access. Use whenever the user says "wrap up", "wrap this up", "let's close out the session", or asks to finalize a cp working session. NOT for closing out a finished engagement — that is `/cp-wrap`.
---

# Wrap up a cp session

The per-session close-out ritual. Everything here is procedure that only
applies once, at the end of a session — which is why it lives in a skill
rather than in the tenant's resident `CLAUDE.md`.

**This is not `/cp-wrap`.** `/cp-wrap` authors the nine-section close-out
*report* for a finished engagement. This is the routine end-of-session
sweep you run whenever work touched a project.

**Scope:** every project **the session actually touched**. A session that
touched one project refreshes one Exec Summary. Do not sweep the tenant.

---

## 0 — Which path are you on?

**Check before step 1, not when a command fails.** The steps below assume a
checkout: they edit `cp.md` with the Edit tool and shell out to `cxp`. A
session reaching this tenant only through the `cp-hosted` MCP server has
neither, and discovering that at step 3 means the Exec Summary was already
half-written by a path that cannot finish.

You are on the **hosted path** if you have `cp-hosted` MCP tools and no
working `cxp` and no Edit access to the tenant's files. Do not infer this
from a tool's name — `cxp --version` answers it, and a hosted session's
`whoami` tells you who the writes will be attributed to.

If you are on the hosted path, **stop here and follow "The hosted path"
below.** Otherwise continue to step 1.

---

## 1 — Refresh each touched project's Exec Summary

Each project `cp.md` carries a model-authored `## Exec Summary` region
(between `<!-- cp-engine:start exec-summary -->` and
`<!-- cp-engine:end exec-summary -->`) with six fields — **Objective /
Status / Where it stands / Next up / Blockers / Updates** — plus a
`**Last session:**` line. **You author the six fields; the engine does
not.** The engine only scaffolds the region, migrates the old Quick Resume
into it on `cxp sync`, and reads it for `/cp-prep`.

> **The `**Last session:**` line is DERIVED, not authored** — a projection
> of the newest file under `sessions/`, recomputed by `cxp capture-session`
> and re-converged on every `cxp sync`. Don't hand-edit it; on a merge
> conflict keep either side and run `cxp sync` — it self-heals.

Auto-ingest never writes project `cp.md` state — per-meeting truth lands in
the sprint file; you refresh the Exec Summary here.

Edit directly between the `exec-summary` markers (Edit tool). It is a
**merge, not a regenerate** — a session that touched one aspect must not
wipe the rest:

1. **Read the prior Exec Summary** — its six fields and the full Updates
   history.
2. **Read this session's changes** — the sprint-file edits, spine updates,
   and any recent meeting ingests for this project.
3. **Rewrite the six fields against current reality**, carrying forward
   everything that's still true and revising only what changed.
4. **Append ONE dated Update** capturing this session's delta:
   `- <today> — <what changed>`.
5. **Roll off Updates older than ~4 weeks** so the history stays tight.
6. **Stamp `· updated <today>`** on the `## Exec Summary` heading line —
   `/cp-prep` flags an unstamped or old summary as STALE in the planning
   bundle.

This is the durable project-state surface; transient weekly material
belongs in the sprint file, not the Exec Summary.

Field budgets are warn-only and enforced by `cxp exec-lint <code>`
(Status ≤ 100 words; Where it stands ≤ 5 bullets and ≤ 40 words/bullet;
Next up ≤ 6 bullets; Blockers ≤ 5 bullets).

## 2 — Sweep the cross-cutting decisions

Cross-cutting decisions live on the parent that owns them: the hand-written
`## Decisions` of each touched project's **account or program `cp.md`**, and
`master-cp.md`'s `## Decisions (cross-cutting, hand-written)` for the ones
that belong to no company. Auto-ingest appends there and nothing expires
the entries, and they feed sprint planning. Once per wrap up, for each
touched project's ancestors plus `master-cp.md`: append
`[resolved: <today> — <outcome>]` to entries that are done or expired
(decision made, event passed, date behind us) so the planner drops them.
Ask when the outcome isn't obvious. A decision recorded on a job that
belongs to its account or program: `cxp promote-uphill <code> --decision
<hash or text>`.

**Never delete — the resolved marker IS the archive.**

## 3 — Sweep the touched projects' open commitments

`cxp commitments-sweep <code>` per touched project:

- resolve what the session completed
- drop what it made moot
- question undated rows ≥2 weeks old — the TTL expires them otherwise
- read the `≈ likely duplicate` pairs: when both rows are one obligation
  (typically your hand-logged row and the one auto-ingest wrote for the same
  meeting), resolve the redundant one

## 4 — Spine checks

For each project touched:

- **`cxp spine-lint <code>`** — WARN-ONLY: important-yet-unbound elements,
  Agreements missing their source (close via `add_element_source` on
  `cp-hosted`), scaffold placeholders in `cp.md`. Surface findings; fix
  only what the user confirms.
- **`cxp seal-sweep <code>`** — for each deliverable that shipped a version,
  what fed it plus the `seal_to_deliverable` call. Absorbing a round's
  inputs keeps the spine distilled. Read its output carefully: `/cp-tools`.

## 5 — Canon agreement check

For each project touched **where this session authored or revised a
document that asserts a framework** — a territory set, pillar structure,
naming system, campaign architecture, messaging hierarchy:

- **`cxp brief <code>`** — WARN-ONLY. Its **Canon — current truth** section
  lists each canon member with a one-line gist of what it actually says,
  and flags any member carrying an unsettled question. Compare the document
  you wrote against it and report **one line per document**: *agrees* /
  *diverges* / *asserts no framework*.

**Diverging is not automatically wrong.** Canon evolves, and a deliberate
departure is legitimate work. What is never acceptable is a silent one —
so name it to the user, and let them decide whether the document changes
or the canon does. Ask when it isn't obvious which.

**Then the question that costs the most when skipped:** did anything
authored this session **close a decision that canon still flags as open**?
A downstream document can settle an open question purely by omission —
picking one branch, never mentioning the other — and nothing downstream
ever notices. The `⚠ NOT settled` lines in `cxp brief` are exactly the
questions at risk. Surface any you closed; **never resolve one silently.**

Skip this step entirely when the session wrote no framework-asserting
document — most sessions don't.

## 6 — Propose journey steps (rarely)

Spine content-writes journal themselves (one review-gated auto-step per
element per day). Hand-propose a step (`propose_spine_step`) only for a
move the auto-step doesn't capture, **≤2 per session**. Authoring runs on
`cp-hosted`, so the write carries your identity. Full discipline:
`/cp-tools`.

## 7 — Word-count discipline

Per bootstrap v2:

- **>2,500 words** on a project `cp.md` → duplication audit on next wrap-up
- **>3,500 words** → archive rotation

Authored text counts: hand-written prose plus the Exec Summary (it sits in a
`cp-engine` region but you write it, so trim it too). Every other
`cp-engine:start/end` region is excluded. Sprint files are not measured.

`cxp render` warns on both thresholds (warn-only — it never blocks a commit
and never edits). Acting on the warning is yours: the audit is manual; for
the rotation, `rotate_word_count` (on `cp-hosted`) moves Updates older than
28 days to `cp-archive-<YYYY-MM>.md` in one commit — pull before your own
commit, since it lands upstream. Trimming hand-written sections stays manual.

**Exempt:** per-meeting artifacts under any `meetings/` directory (fixed
per-meeting records — synthesis + verbatim transcript — legitimately long)
and `spine/Retrospective/meeting-history.md`; do not audit or rotate them.

## 8 — Improvements sweep

Friction is logged to `improvements.md` **at the moment it happens**, not
here. At wrap up, sweep for anything that went unlogged — a workaround you
reached for, a surface that fought you — and add it. Real bugs still go to
GitHub issues. Never delete entries.

## 9 — Commit and push

Commit the entire `sprints/<YYYY-W##>/` directory alongside the master
roll-up and each touched project's `cp.md`. Then push.

The auto-ingest webhook pushes to the same remote while you work, so the
push usually needs a merge first: `git fetch`, then `git merge origin/main`
(not a rebase — `--ours`/`--theirs` swap meaning mid-rebase). `cxp sync`
warns when the clone is behind its upstream; heed it.

**Resolve by region, not by file.** Conflicts **inside** `cp-engine:start` /
`cp-engine:end` markers resolve as ours — sync regenerates them. **Inspect
everything else.** Never loop `git checkout --ours` over conflicted files: a
sprint file is hand-written outside its marked regions, and that is where
auto-ingest writes its `cp:hash` bullets. A blanket `--ours` on 2026-08-25
deleted a whole meeting's 20 bullets, and the file still looked right.

**After any merge, run `cxp merge-check` before committing the
resolution.** It checks that every `cp:hash` on the remote side survives in
the working tree. Mid-merge the default `--ref MERGE_HEAD` is right; once
the merge is committed, pass `--ref origin/main`. Exit 0 means nothing was
lost. Exit 1 lists each missing bullet with its file: restore it from the
ref, then run it again. Push only on exit 0.

---

## The hosted path

Every verb below is on `cp-hosted`. The order matters: the Exec Summary is
written first because the checks in 2–5 read what you wrote.

### h1 — Refresh each touched project's Exec Summary

**`get_project_state <code>` first, then `capture_project_state`.** The three
bulleted fields (`where_it_stands`, `next_up`, `blockers`) each replace ALL
existing bullets — a partial rewrite silently drops the ones you did not
resend, which is why you read before you write.

**Pass every field you mean to be current, not just `status`.** The verb takes
`status`, `objective`, `where_it_stands`, `next_up` and `blockers` — the five
it replaces — plus `updates_append` for the sixth, below. Omitted fields
are left exactly as they are — deliberate, because most real rewrites touch one
field and a whole-region write would make the common case the destructive one.
But the failure it enables is the one worth naming: a Status-only refresh
advances the `· updated` stamp while Next up and Blockers go stale, and every
staleness check in the system reads that stamp. **That is worse than not
refreshing at all**, because it converts "obviously old" into "looks current."

> Measured on ggl-5136, 2026-09-17: a current Status and a two-day-old stamp
> sat above a `Next up` listing three July deadlines and naming a collaborator
> the Status line said had left.

**On a summary already stamped 14+ days ago, a partial refresh is refused.**
The verb names the fields you omitted (`stale_fields`) and writes nothing:
nobody has touched them since that stamp, so they are at least that old.
Pass each one's current text, or — having read it and found it still true —
name it in `still_current`. Never list a field there you did not read; that
is the Status-only refresh again with an extra step.

Re-sending a field's existing value is a no-op — it neither commits nor moves
the stamp — so a retry after a timeout is safe.

**`capture_project_state` commits for you.** The write is delegated upstream
and lands under your own identity; there is no separate commit step on this
path.

**`Updates` appends rather than replaces: `updates_append`.** Pass ONE entry —
the session's delta — and it lands dated (server-side, so it cannot be
backdated) at the top of the log; the old entries stay. The CLI path also
rolls off entries older than ~4 weeks; here the verb only REPORTS them
(`roll_off`, under `backend`). When `roll_off.count` is non-zero, h5's
`rotate_word_count` performs the trim.

### h2 — Spine checks

- **`spine_lint <code>`** — WARN-ONLY. Important-yet-unbound elements,
  Agreements missing their source (close via `add_element_source`), scaffold
  placeholders, and the Exec Summary field budgets. Surface findings; fix only
  what the user confirms.
- **`seal_sweep <code>`** — for each deliverable that shipped a version, what
  fed it. Read the `via` on each candidate: an empty `via` is a source bound
  directly to the deliverable, a non-empty one reached it *through* an
  activity. Both are real; the indirect one is weaker evidence, and sealing it
  asserts a provenance nobody recorded. Act with `seal_to_deliverable`.

### h3 — Sweep open commitments

**`commitments_sweep <code>`** per touched project — resolve what the session
completed (`resolve_commitment`), drop what it made moot, and question undated
rows ≥2 weeks old. An undated commitment expires at 14 days; the `ttl` field is
where that gets noticed in time rather than after. The likely-duplicate pairs name
rows that read as one obligation — resolve the redundant row when they match.

### h4 — Canon agreement check

Only when this session authored or revised a document asserting a framework —
a territory set, pillar structure, naming system, messaging hierarchy. Most
sessions did not; skip it then.

`cxp brief` is CLI-only. The hosted equivalent is to read canon directly:
`list_spine_elements <code>` and `pull_spine_element` on the canon members.
Report one line per document — *agrees* / *diverges* / *asserts no framework*.

**Diverging is not automatically wrong; diverging silently is.** Name it and
let the user decide whether the document changes or the canon does. And the
question that costs most when skipped: did anything authored this session close
a question canon still flags as open? A document can settle one purely by
omission — picking a branch, never mentioning the other. **Never resolve one
silently.**

### h5 — Word count

**`word_count_check <code>`** — REPORTING ONLY. It tells you a file's
authored text (hand-written plus the Exec Summary) crossed 2,500 words (duplication audit) or 3,500 (archive
rotation).

**`rotate_word_count <code>`** acts on it — run it when the check trips or
h1 reported a non-zero `roll_off`. It moves Updates entries older than 28
days, verbatim, from `cp.md` to `cp-archive-<YYYY-MM>.md` beside it, in ONE
commit under your name; the `· updated` stamp does not move. Nothing past
age is a no-op (`changed: false`, no commit), so it is safe to call. It
refuses — writing nothing — if the move would lose a single line
(`refused: true`); report that to the user, do not retry around it.

It never touches hand-written sections. If `over_audit_threshold` or
`over_rotation_threshold` is still true afterwards, the remaining words are
Project Notes, Decisions and the like — which of them is resolved is a
reader's judgement. Name the biggest contributors (`word_count_check`'s
`contributors`) and hand the trim to the user.

### h6 — Capture the session

**`capture_session <code> <summary>`** — the one verb here that writes a FILE
rather than a row, because `**Last session:**` is a projection of the
`sessions/` directory and a row would never move it.

Write what a wrap-up would say: what you set out to do, what you decided and
why, what you left open. Not a diff — the commits carry that. **You cannot set
the author**; the name on the file is derived from your token, which is the
whole of its provenance value.

The session record and the h1 `updates_append` entry are different reads: the
entry is one line in the project's history, the record is the whole session.

### What the hosted path cannot do

- **Commit and push the tree.** `capture_project_state` and `capture_session`
  commit their own writes upstream; nothing else on this path touches the repo,
  so there is no tree to push.
- **Hand-written word-count rotation** (h5) — `rotate_word_count` moves aged
  Updates only; trimming Project Notes or Decisions is a reader's call.
- **The account / program `cp.md` cross-cutting decisions sweep** (step 2 of
  the CLI path) — marking an entry `[resolved: …]` is a hand-edit of tenant
  files, and deliberately has no verb (#282): the entries run to thousands of
  characters with nested markers, so resolving one is a judgement a human
  makes by reading it. You CAN read them (`read_project_file`) and you CAN
  move a job's decision up a level (`promote_uphill`, `item_kind='decision'`);
  you cannot mark one resolved.

For the decisions sweep: surface the entries you believe are done or expired,
with the outcome you would have written, and hand them to the user rather
than skipping it silently.

**The improvements sweep (CLI step 8) IS runnable here:** `log_improvement
<area> <observation>` appends one entry, and a duplicate is a no-op. Sweep
the session for friction nobody logged and log it. A step nobody knows was skipped is the same
failure as a stamp nobody knows is stale.

**None of this is a permission problem to route around.** The server holds the
caller's token and an anon key, never a service key — so every write it makes
is performed upstream under the identity of whoever asked for it. A write key
here would unblock the list above and destroy the attribution that makes the
session record worth reading.

---

## Notes

- **Engine-managed regions are off-limits.** Anything between
  `cp-engine:start` / `cp-engine:end` markers belongs to sync — the one
  exception is the `exec-summary` region, which you author (step 1). A
  `PreToolUse` guard blocks edits to the others and exempts `exec-summary`
  by name, so step 1 works with no ceremony. If the guard ever blocks an
  edit you believe is legitimate, that is a bug in the guard — say so
  rather than working around it.
- **Weekly review.** `wrap up` also closes a `run weekly review` block; the
  prior mode persists if the session continues.
- **After a cp-engine release**, `cxp mcp` keeps serving old bytecode.
  Restart the MCP connection (`/mcp`) before assuming a spine tool is
  broken.
