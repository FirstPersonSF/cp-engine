---
Project: Context Protocol Engine
Provenance: Version 01 amendments | 2026-05-06
Filename: cp-engine-spec-v01-amendments.md
Author: Drew + Claude (for Tony's review)
---

# CP Engine Spec v01 — Proposed Amendments

> Three amendments to `cp-engine-spec-v01.md` that surfaced while Drew and Claude were brainstorming the implementation. Each is scoped, defensible on its own, and intended to land before v1 build starts. Tony — please review and either accept, reject, or push back per amendment.

## Context

Drew already has a working MC-2 → GitHub sync (`backend/src/active_jobs_sync.py` + `github_context_client.py`) that pushes a flat `active-jobs.md` to `FirstPersonSF/1p-job-context`. The plan is to evolve that code in place — repoint it at `context-protocol`, expand it to write the spec's `master-cp.md` schema, and migrate the PAT to a GitHub App as a separate task. This amendment draft sits in front of that work.

The brainstorm raised three concerns with v01 as written. Each becomes one amendment below.

---

## Amendment 1 — Status enum & active-subset definition

### What v01 says

§Q1 resolves the status vocabulary as `Potential | Open | Closed | Archived | Internal`, with active subset = `Potential ∪ Open`. It claims this aligns with `FirstPersonSF/mc-2#17`.

### What's actually in MC-2

- Today (`frontend/src/lib/api.ts:55`): `Deal | Active | Holding | Complete | Archived`. Plus `is_internal` boolean (orthogonal axis).
- PR #17 (open, not merged): renames `Active → Open`, `Complete → Closed`. Doesn't add `Potential`, doesn't drop `Holding`, doesn't add `Internal`.

So the v01 resolution describes a vocabulary that doesn't exist anywhere yet — neither in MC-2 today nor in PR #17.

### Proposed amendment

Final vocabulary, agreed by Drew and Tony 2026-05-06:

| Status | Active? | Notes |
|---|---|---|
| `Deal` | yes | Replaces v01's proposed `Potential`. Matches MC-2's existing term. Sub-vocabulary `deal_stage` (`Inquiry / Negotiation / Contract / Won / Lost`) stays in MC-2 only — not surfaced in master CP. |
| `Open` | yes | Renamed from MC-2's `Active`. |
| `Holding` | no | Live state (deal could resume). Not in v01's resolution but present in MC-2 today. Worth preserving — see Amendment 2 for master-CP placement. |
| `Closed` | no | Renamed from MC-2's `Complete`. |
| `Archived` | no | Same as today. |

`Internal` is **dropped** from the status vocabulary. It was conflating two orthogonal axes (lifecycle vs. ownership). MC-2's existing `is_internal` boolean stays, used as an additional filter on the master-CP sync (`is_internal=false` AND `is_active_status(mc_status)`). Internal projects with active lifecycle status (e.g. Canonic's own work) keep their real status and stay surfaced in sprint-planning UIs that need them — they just don't appear in `master-cp.md`.

**Active subset = `Deal ∪ Open` AND `is_internal=false`.**

### Per-status active/inactive helper (Tony's request)

So adding a future status doesn't require touching every consumer that asks "is this active?", introduce a single source of truth:

**Frontend** (`frontend/src/lib/api.ts`):

```ts
export const MC_STATUSES = ["Deal", "Open", "Holding", "Closed", "Archived"] as const;
export type McStatus = (typeof MC_STATUSES)[number];

export const MC_STATUS_ACTIVE: Record<McStatus, boolean> = {
  Deal: true,
  Open: true,
  Holding: false,
  Closed: false,
  Archived: false,
};

export const isActiveStatus = (s: McStatus): boolean => MC_STATUS_ACTIVE[s];
```

**Backend** (`backend/src/mc_status.py`, new — mirrors the frontend):

```python
MC_STATUSES = ("Deal", "Open", "Holding", "Closed", "Archived")
_ACTIVE = {"Deal": True, "Open": True, "Holding": False, "Closed": False, "Archived": False}

def is_active_status(s: str) -> bool:
    return _ACTIVE.get(s, False)
```

Every consumer (jobs page tabs, financials dashboard, sprint workload grid, the active-jobs sync, the future master-CP sync) imports the helper instead of hardcoding `"Active"` or `["Deal","Open"]`. Adding a status = update the constant + the boolean map; everything downstream picks it up.

Code-only by design (not a database table) — status vocabulary changes rarely and every new value needs UI affordances anyway (label, color, tab placement). Surfacing it as code keeps the work visible.

### Spec changes implied

- §Q1 resolution table → rewrite per above.
- §3.3 "Active subset rule" → update to `Deal ∪ Open` AND `is_internal=false`.
- §3.1 master CP schema → see Amendment 2 for the `Holding` subtable.
- §5 sync → no logical change; just uses the new helper.

### MC-2 changes implied (PR #17 grows)

PR #17's current scope (two renames) expands to:
1. Rename `Active → Open`, `Complete → Closed` (already in PR).
2. Add `MC_STATUS_ACTIVE` map + `isActiveStatus` helper to `frontend/src/lib/api.ts`.
3. Create `backend/src/mc_status.py` with the equivalent Python helper.
4. Refactor hardcoded `"Active"` literals to use the helper. Drew counted ~6 sites: `frontend/src/app/(app)/jobs/page.tsx:264,286`, `frontend/src/components/sprint/WorkloadGrid.tsx:279`, `backend/src/active_jobs_sync.py:44`, `backend/src/airtable_client.py:152`, plus comment updates in `JobOutlookCard.tsx` and `PipelineOutlookCard.tsx`.
5. Database migration: rename `Active` rows to `Open`, `Complete` rows to `Closed`. If any rows currently sit at `mc_status="Internal"`, repoint them to a real lifecycle status (`Open` or `Holding`, case-by-case) and ensure their `is_internal` boolean is true.
6. Confirm Airtable filter (`airtable_client.py:152` — uses `"Active"`) — does Airtable's status vocabulary need to follow MC-2's, or is it a separate system with its own terms? If the former, it gets renamed too.

---

## Amendment 2 — Reading discipline & file split

### What v01 says

§6.1 on-session-start protocol: read `master-cp.md`, then optionally one per-project CP. §1.2 marks `master-cp.md` as Warm tier. §3.1 has the master CP carrying Quick Resume, Decisions (cross-cutting, last 4 weeks), Active research, Closed-recent — alongside the project index.

### Concern

The v01 model has only one default loading mode and one place where everything lives. Two consequences:

1. **Any session that opens Claude in the repo loads the full master CP**, including the week's themes and four weeks of cross-cutting decisions, even if the user only wants to update one project's CP.
2. **The Monday meeting's `run weekly review` (§7.1) walks every project CP in sequence** — at six clients × multiple projects each, that's potentially 15–25 CPs loaded into one session, plus the master CP, plus a 60-min Zoom transcript for deepening. Workable but expensive, and the spec doesn't acknowledge the cost or push back against ad-hoc auto-globbing.

There's no enforced isolation. Nothing in v01 stops a session from glob-loading `1P/**/*.md` for "context."

### Proposed amendment — four explicit loading modes

CLAUDE.md defines four reading modes. It is the gatekeeper, not just a routing reference. Each trigger phrase or session shape maps to exactly one mode.

| Mode | Files loaded | Files explicitly NOT loaded | Triggered by |
|---|---|---|---|
| **1. Index-only (default)** | `master-cp.md` | All of `1P/`, `canonic/`, `weekly-cp.md` | Any session opened in the repo without a scoping trigger phrase. |
| **2. Single-project** | `master-cp.md` + `1P/<client>/<code>-cp.md` | Other per-project CPs, `weekly-cp.md` | `update <client> <code>`, `check status <client> <code>` |
| **3. Sprint** | `master-cp.md` + `canonic/sprint-cp.md` | All of `1P/`, `weekly-cp.md` | `update sprint`, `check status sprint` |
| **4. Weekly review** | `master-cp.md` + `weekly-cp.md` + every active per-project CP + `canonic/sprint-cp.md` | (none — this mode is the heavyweight one) | `run weekly review` (and only this phrase) |

CLAUDE.md explicitly forbids auto-globbing `1P/**/*.md` outside mode 4. If a session needs broader context than its current mode loaded, the user must escalate explicitly (e.g. "also load ggl-5168").

### Proposed amendment — split master-cp.md

Today the master CP carries both an index *and* meeting-state content (Quick Resume, this week's themes, recent decisions, active research). Split these:

**`master-cp.md` becomes the thin index:**
- Active table with columns: `Client | Code | Project | Status | Owner | Last touched | One-line summary | CP-link`.
- The "One-line summary" column is new. It's what makes mode 1 useful — the master CP becomes a *menu*, not just a row of statuses. (See Amendment 3 for who writes it.)
- `Holding` projects: their own subtable below Active, wrapped in HTML `<details>` (collapsed by default). Visible-but-zero-cost when nobody asks. Lets a Monday driver expand it to check "anything to revisit?" without it eating tokens during normal sessions.
- Closed-recent: also `<details>`-wrapped.
- Last automation sync timestamp.

**New `weekly-cp.md` (top-level alongside `master-cp.md`):**
- Quick Resume / "This week's themes."
- Cross-cutting Decisions (last 4 weeks).
- Active research pointers.
- Loaded only in mode 4.

### Spec changes implied

- §1.2 memory tier table — `weekly-cp.md` joins the Warm tier alongside `master-cp.md` and per-project CPs.
- §3.1 master CP schema — strip Quick Resume / Decisions / Active research sections; add `Holding` collapsed subtable; add One-line summary column.
- New §3.5 (or new section §3a) — `weekly-cp.md` schema.
- §3.2 automation-managed table — drop the rows for content moved to `weekly-cp.md`. Note that MC-2 sync never touches `weekly-cp.md`.
- §6.1 on-session-start protocol — replace with the four-mode table above.
- §6.1 step 4 (the "mention if last sync >1 hour during business hours" rule) — **drop**. MC-2's existing "Sync failed" badge (§5.5) catches this at the source; surfacing it again at every session start adds noise without signal.
- §7.1 step 1 — clarify that this is mode 4 specifically, and that `master-cp.md` + `weekly-cp.md` get loaded as the meeting frame before per-project CPs are walked.

### Why one repo is still right

Drew considered splitting per client (`1p-google-cp`, `1p-sentinelone-cp`, etc.) for stronger isolation. We rejected it: the operational cost (six repos × four collaborators × MCP setup × six places to keep `CLAUDE.md` in sync) outweighs the isolation gain for a 5-person company. The four loading modes give us the isolation we actually need — at the *session* level, not the repo level.

---

## Amendment 3 — Auto-generated master CP summaries

### What v01 says

The "One-line summary" column doesn't exist in v01's master CP schema (Amendment 2 introduces it). v01 also doesn't specify who would maintain such a thing.

### Proposed amendment

The summary column is regenerated by Claude during the **deepening pass** (§7.3) only, not on every CP edit.

**Rationale:** the summary is a navigation aid, not a source of truth. Auto-regenerating on every mid-week `update` would force every edit session to load `master-cp.md`, regenerate one row, and commit two files — overhead disproportionate to the benefit. The Monday cadence is fast enough; mid-week summary staleness is acceptable.

### Tradeoff to accept

If a project's CP gets a major update on Wednesday, its master-CP one-liner will be stale until the next Monday deepening pass. Worth it.

### Spec changes implied

- §3.1 — "One-line summary" column documented as deepening-pass-managed.
- §7.3 — add step: after extracting CP edits, regenerate the master-CP one-liner for each touched project from the project CP's Quick Resume.

---

## Implementation sequencing

These amendments don't all need to land at the same moment. Drew's recommended order:

1. **You and Drew lock the final status vocab + `is_internal` retention** (Amendment 1). Done as of 2026-05-06.
2. **PR #17 expands** to cover Amendment 1's full MC-2 scope. Merges. Ships.
3. **Drew's `active_jobs_sync.py` updates** to use `is_active_status` (one-line change). The `1p-job-context` repo keeps working through the transition.
4. **Spec gets revised** to v02 (or v01.1) incorporating the three amendments. Lands as a commit to this repo.
5. **Then** the master-CP writer gets built — by extending `active_jobs_sync.py` or creating a sibling `master_cp_sync.py` — and the `context-protocol` repo gets seeded with the master CP, weekly CP, and per-project CP scaffolding for all six active clients.

Steps 1–3 are MC-2-internal and worth doing on their own merits even if the CP system slips. Step 4 unblocks step 5.

---

## Open questions for Tony

1. Amendment 1: Airtable's status filter (`backend/src/airtable_client.py:152` uses `"Active"`) — same vocabulary as MC-2 or independent? If same, PR #17 covers it; if independent, decide separately.
2. Amendment 2: Anything you'd add to or remove from the four-mode table?
3. Amendment 3: Comfortable with mid-week summary staleness, or do you want a different cadence?
4. Sequencing: any reason to interleave step 4 (spec revision) earlier — e.g. to start drafting CLAUDE.md before MC-2 cleanup lands?
