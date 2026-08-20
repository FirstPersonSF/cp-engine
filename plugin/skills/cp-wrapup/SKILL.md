---
name: cp-wrapup
description: Run the per-session `wrap up` ritual in a cp tenant — refresh each touched project's Exec Summary, sweep cross-cutting decisions and open commitments, run spine + word-count checks, then commit and push. Use whenever the user says "wrap up", "wrap this up", "let's close out the session", or asks to finalize a cp working session. NOT for closing out a finished engagement — that is `/cp-wrap`.
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

## 1 — Refresh each touched project's Exec Summary

Each project `cp.md` carries a model-authored `## Exec Summary` region
(between `<!-- cp-engine:start exec-summary -->` and
`<!-- cp-engine:end exec-summary -->`) with six fields — **Objective /
Status / Where it stands / Next up / Blockers / Updates** — plus a
`**Last session:**` line. **You author the six fields; the engine does
not.** The engine only scaffolds the region, migrates the old Quick Resume
into it on `cp sync`, and reads it for `/cp-prep`.

> **The `**Last session:**` line is DERIVED, not authored** — a projection
> of the newest file under `sessions/`, recomputed by `cp capture-session`
> and re-converged on every `cp sync`. Don't hand-edit it; on a merge
> conflict keep either side and run `cp sync` — it self-heals.

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

Field budgets are warn-only and enforced by `cp exec-lint <code>`
(Status ≤ 100 words; Where it stands ≤ 5 bullets and ≤ 40 words/bullet;
Next up ≤ 6 bullets; Blockers ≤ 5 bullets).

## 2 — Sweep `weekly-cp.md`'s cross-cutting decisions

`## Decisions (cross-cutting, last 4 weeks)` accretes auto-ingested entries
that nothing expires, and it feeds sprint planning. Once per wrap up:
append `[resolved: <today> — <outcome>]` to entries that are done or
expired (decision made, event passed, date behind us) so the planner drops
them. Ask when the outcome isn't obvious.

**Never delete — the resolved marker IS the archive.**

## 3 — Sweep the touched projects' open commitments

`cp commitments-sweep <code>` per touched project:

- resolve what the session completed
- drop what it made moot
- question undated rows ≥2 weeks old — the TTL expires them otherwise

## 4 — Spine checks

For each project touched:

- **`cp spine-lint <code>`** — WARN-ONLY: important-yet-unbound elements,
  Agreements missing their source (close via `add_element_source` on
  `cp-hosted`), scaffold placeholders in `cp.md`. Surface findings; fix
  only what the user confirms.
- **`cp seal-sweep <code>`** — for each deliverable that shipped a version,
  what fed it plus the `seal_to_deliverable` call. Absorbing a round's
  inputs keeps the spine distilled. Read its output carefully: `/cp-tools`.

## 5 — Propose journey steps (rarely)

Spine content-writes journal themselves (one review-gated auto-step per
element per day). Hand-propose a step (`propose_spine_step`) only for a
move the auto-step doesn't capture, **≤2 per session**. Authoring runs on
`cp-hosted`, so the write carries your identity. Full discipline:
`/cp-tools`.

## 6 — Word-count discipline

Per bootstrap v2:

- **>2,500 words** on a CP file → duplication audit on next wrap-up
- **>3,500 words** → archive rotation

`cp render` warns on both thresholds (warn-only — it never blocks a commit
and never edits). Acting on the warning is yours: the audit and the
rotation are manual.

**Exempt:** per-meeting artifacts under any `meetings/` directory (fixed
per-meeting records — synthesis + verbatim transcript — legitimately long)
and `spine/Retrospective/meeting-history.md`; do not audit or rotate them.

## 7 — Improvements sweep

Friction is logged to `improvements.md` **at the moment it happens**, not
here. At wrap up, sweep for anything that went unlogged — a workaround you
reached for, a surface that fought you — and add it. Real bugs still go to
GitHub issues. Never delete entries.

## 8 — Commit and push

Commit the entire `sprints/<YYYY-W##>/` directory alongside the master
roll-up and each touched project's `cp.md`. Then push.

---

## Notes

- **Engine-managed regions are off-limits.** Anything between
  `cp-engine:start` / `cp-engine:end` markers belongs to sync — the one
  exception is the `exec-summary` region, which you author (step 1). A
  `PreToolUse` guard blocks edits to the others.
- **Weekly review.** `wrap up` also closes a `run weekly review` block; the
  prior mode persists if the session continues.
- **After a cp-engine release**, `cp mcp` keeps serving old bytecode.
  Restart the MCP connection (`/mcp`) before assuming a spine tool is
  broken.
