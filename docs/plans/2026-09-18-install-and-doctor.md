---
Project: cp-engine
Provenance: Version 04 | 2026-09-18
Filename: 2026-09-18-install-and-doctor.md
Author: Claude
---

# One check module, two thin shims, a pin that moves

**Issue:** [cp-engine #296](https://github.com/FirstPersonSF/cp-engine/issues/296)
**Status:** v04 — restructured after an architectural review of v03 found that
Phase 1 shipped its detector inside the component that drifts, and that the plan
as a whole was adding mechanisms while diagnosing that mechanisms had accreted.

> **Lineage.** v01 diagnosed a handoff gap and proposed a five-surface scanner
> plus a human-facing install page; an adversarial review refuted the diagnosis.
> v02 rewrote against Tony's machine audit, which refuted the recipient and one
> factual claim. v03 absorbed Tony's review of v02, which found a third silence
> that would have shipped Phase 1 broken. v04 is a structural rewrite, not a
> patch: v03's phases were correct findings attached to the wrong architecture.
> Evidence base: `2026-09-18-install-audit-report-tony.md` and his
> `feedback-on-install-and-doctor-plan-v01.md`.

---

## 1. What actually failed

A `cxp` twelve releases behind its plugin ran a routine render and rewrote **54
provenance stamps backwards, one each across 54 files**, in a commit touching 58
(cp `393ad9c8`). Tony caught it by hand and committed only the roll-up.

> **The figure, pinned.** "34 files" (`improvements.md`, night-of) was an
> undercount; v02's "54 across 58" conflated stamp count with commit size.
> Derived: `git show 393ad9c8 | awk '/^diff --git/{f=$3} /^\+Provenance.*0\.108\.1/{print f}' | sort -u`
> → 54 distinct files, one stamp each. Quote **54 / 54 / 58**.

### 1.1 Three silences, verified on two machines

```
plugin/hooks/sync-cli-version.sh          compares PLUGIN vs CLI
  ├─ silence 1: walks up for .cp-engine.toml → exit 0 inside a tenant  (deliberate, #28)
  └─ silence 3: no-downgrade guard → exit 0 when CLI is AHEAD of plugin
                 (the incident's direction — removing silence 1 alone changes nothing)

.claude/hooks/check-cp-engine-version.py  compares CLI vs TENANT PIN
  └─ silence 2: pin was `~= 0.42`, satisfied by 0.42.0 … 0.120.x (78 releases)
                 → `if ok: return 0  # healthy — the common path, fully silent`
                 → never reads the plugin version at all (verified: 3 mentions, all prose)
```

Every cp session starts inside a tenant. The only check that can see
plugin-vs-CLI drift switches itself off there, and the check it defers to
structurally cannot see it. Tony confirms he has **never** seen either warning.
*The self-heal reported healthy while twelve releases of drift accumulated
underneath it.*

### 1.2 The pin is a shared defect, and it is mine

`~= 0.42` was set by me 2026-06-30 (`0951d0b6`) and never moved through 80
releases. It lives in a committed file, so **every tenant had the same dead
check and no per-machine scan could find it.** Tightened to `~= 0.120` on
2026-09-18 — the instance, not the class.

### 1.3 What reproduced on machine two

| Hypothesis | Tony's machine |
|---|---|
| Stale `cxp mcp` subprocesses | **Confirmed** — 2 of 3, serving 0.119.0 bytecode, live |
| 0.40.0 downgrade bomb | Absent — Drew's machine only |
| 29 cached plugin copies | Absent — he has 2, byte-identical hooks |

---

## 2. The constraint that decides the architecture

**The component that has drifted cannot carry the fix that detects its drift.**

The plugin hook runs from `CLAUDE_PLUGIN_ROOT` — the installed plugin's own
cache. A stale plugin runs its stale hook. So a plugin-vs-CLI check shipped in
the plugin hook (v03's Phase 1) is invisible on exactly the machine it exists
for. Its control test proves the *new* hook warns on the fixture; it does not
prove the new hook *runs* on a drifted machine.

**Each drift direction is observable only from the side that is not stale:**

| Direction | Stale side | The only place a check can run |
|---|---|---|
| CLI ahead of plugin (**the incident**) | plugin hook | **tenant hook** — packaged in `cp_engine`, written by `cxp sync`, git-tracked, delivered by `tenant-freshness.sh` ff |
| Plugin ahead of CLI | tenant hook | **plugin hook** |

Two checks, each reading the other's version, and a drift test that they agree.

The delivery path for the tenant side needs no action from the drifted user: CI's
`[cp-sync]` runs at the resolved pin, rewrites `.claude/hooks/`, commits, and the
drifted machine fast-forwards it at next session start. *(Verify CI sync commits
hook diffs — it should; the file is tracked.)*

### 2.1 And therefore: one module, not five mechanisms

v03 added a reporter, a check in the plugin hook, a check in the tenant hook, a
pin check in `cxp sync` *and* the tenant hook, MCP detection, and a doctor — in
bash and Python, with nothing keeping the copies in agreement. That is §1's
failure reproduced one layer up.

**v04:** all check logic lives in **one Python module in `cp_engine`**
(`cp_engine/health.py`, working name). It emits *findings*. Everything else is a
caller:

```
cp_engine/health.py ── findings ──┬─ cxp doctor --brief     → the one line
                                  ├─ cxp doctor             → full report
                                  ├─ tenant hook             → calls --brief at SessionStart
                                  └─ cxp sync                → raises the pin floor (§4.3)

plugin/hooks/sync-cli-version.sh   ← minimal bash: ONE comparison for the
                                     plugin-ahead direction only (it cannot
                                     call a stale cxp), guarded by a drift test
                                     that the bash and Python comparisons agree
```

Doctor is no longer "last and smallest." **Its engine is built first; its
verbose face is the last thing wired up.**

---

## 3. From Tony's audit and review — what shapes the output

- **He installed nothing.** A cold Claude session pointed at `mc-2` improvised a
  four-surface install from one sentence and wrote nothing down. The install
  path must be written for an agent, placed where an agent looks, and **emit its
  own record**.
- **He states intents; he does not type commands.** SessionStart output is the
  only thing he reliably reads — and only *"if it looked alarming."* A line that
  appears solely when something is wrong is alarming by construction.
- **He runs hosted and local simultaneously.** Profiles are dropped.
- **Vocabulary.** The spine is the memory layer in MC-2 and he names it
  correctly. What has no name is the toolchain reaching it. Output must name the
  *install*, not the spine.
- **Three artifacts in a row were addressed to a reader who was not there** —
  v01's one-pager, v01's `cxp sync` warning, v02's review request. The test
  adopted: *can this be answered by reacting to something, or does it require
  knowing how it works?* The second is the builder's call.

---

## 4. Build

### 4.1 `health.py` — the findings module

Pure functions, no side effects, no network unless asked. Each returns a
`Finding(severity, code, summary, detail, remedy)` or nothing.

| Check | Reads | Fires when |
|---|---|---|
| `plugin_vs_cli` | `installed_plugins.json` (every entry, every scope) + `cp_engine.__version__` from the **installed interpreter** | any mismatch, **either direction**; remedy follows direction |
| `pin_floor` | `.cp-engine.toml [engine].version` + installed version | floor is below the installed *minor* (§4.3 for why this, not "N minors") |
| `stale_mcp` | `ps` start times vs `uv-receipt.toml` mtime | any `cxp mcp` older than the install; names PIDs, never kills |
| `hosted_vs_local` | `/health` `server_version` vs installed (network, opt-in) | mismatch — or documents that deployment pins them, if that proves true |
| `install_record` | `.cp-engine.local.toml [install]` (§4.5) | absent, or older than the installed versions it describes |

Severity: `plugin_vs_cli` > `pin_floor` > `stale_mcp` > `hosted_vs_local` >
`install_record`. The first is the incident; the last is bookkeeping.

**Control for `plugin_vs_cli` — both directions, or the guard defect survives:**

| Fixture | Required |
|---|---|
| plugin 0.108.1 / CLI 0.119.0 (the incident) | warn → remedy `claude plugin update cp-engine@cp-engine` |
| plugin 0.120.5 / CLI 0.119.0 | warn → remedy `uv tool install --force --reinstall …` from the receipt's source |

### 4.2 The one line — `cxp doctor --brief`

Prints the highest-severity finding plus a count, or nothing. **Silent when
healthy.** Wording is Tony's, amended per the review of v03:

```
[cp] Your cp tools are out of sync with each other and may save bad
     data. Say "update cp-engine" and this session will update them —
     then restart Claude Code to pick it up.  (+2 more: cxp doctor)
     (plugin v0.108.1, engine v0.119.0)
```

The amendment is the *restart* clause. v03's line promised *"this session will
fix it"*; the session can update the files but has already loaded the stale
skills, and in the other direction a reinstalled binary leaves running `cxp mcp`
processes on old bytecode. Without the restart the user does the thing and
continues silently on stale code — the shape being fixed.

**Where it prints.** The tenant hook calls `cxp doctor --brief`. The plugin hook
keeps its own bash comparison for the plugin-ahead direction. That is **two
registrations, so up to two lines** — one per hook system. v03's "one line
total" assumed a consolidation mechanism across registrations that does not
exist: Claude Code runs hooks independently and (verify) in parallel, hook
stdout is plain transcript text, and colour will not render. Two lines, each
silent when healthy, is the honest budget. The plugin's two existing scripts
*can* be merged into one entry, and should be.

### 4.3 The pin moves with releases, from the tenant side

v03's "warn when the floor is more than N minors behind" cannot work: **14
distinct minors shipped in the last 21 days.** Any N that would have caught 78
is also satisfied by a month of drift; any N small enough to matter fires
weekly. With `~=` on 0.x the pin is a floor by construction.

**The floor has to move with releases, and `release.py` cannot move it** (it
lives in another repo). **`cxp sync` can.** Sync runs with a known CLI version;
it raises `[engine].version` to `~= <installed minor>` when the installed minor
is higher, never lowers it, and commits with the rest of the sync. CI sync does
this at every tick, so the floor tracks the resolved pin without anyone
remembering.

**What this costs, stated plainly:** the spec's "tenants opt into a new minor
deliberately" becomes "tenants opt *out* by pinning explicitly." Anyone behind
the raised floor hits `EngineVersionMismatch` — now actionable (v0.120.5) — and
must upgrade. That is the gate working. If a tenant genuinely needs to hold
back, it sets `[engine] version_lock = true` and sync leaves it alone.

`pin_floor` in `health.py` then has a precise meaning: it fires only when sync
has *not* run since a release — which is itself a finding.

### 4.4 Stale `cxp mcp` — detect, name, never kill

Reproduced on both machines. A process whose start time predates the receipt
mtime is serving old bytecode. `health.py` names the PIDs and says `/mcp`
restarts it. **Nothing auto-kills** — a running server belongs to a live session,
possibly someone else's. The existing `mcp_server.py` staleness warning (server
vs disk, #150) covers the same condition from inside MCP results; the two must
agree, and a test should assert they do.

### 4.5 The install record — `.cp-engine.local.toml [install]`

v03 said the install must "emit its own record" and named no destination. It is
`.cp-engine.local.toml`: already per-machine, already gitignored, already home to
`[local-repos]`.

```toml
[install]
recorded_at = "2026-09-18T18:40:00Z"
installer   = "agent"            # agent | human
cli         = { version = "0.120.5", source = "git+…@v0.120.5" }   # from uv-receipt.toml
plugin      = { version = "0.120.5", scope = "user" }
tenant      = { path = "/Users/…/cp", pin = "~= 0.120" }
hosted      = { url = "https://cp.mc-2.1p.is/mcp" }
```

Written by the install payload (§4.6) and refreshed by `cxp sync`. Read by
`install_record` in `health.py` and by `cxp doctor`. This is what connects the
install to the doctor — v03 had both and no link between them.

### 4.6 The install payload — written for an agent, in the repo

Full payload in **cp-engine**, versioned with what it describes. **Pointer in
`mc-2`'s `CLAUDE.md`** — a few lines naming what cp-engine is and where the
payload lives — because that is where the evidence says a cold session arrives.

Ordered so a cold session learns *what the tools give access to* (the spine, the
tenant, the sync) before it learns the toolchain has four parts. Then:
**install / verify / upgrade / uninstall as executable instructions**, what a
correct install looks like afterwards so the agent can check its own work, and
**an explicit statement of who the reader is and what they can be assumed to
know** — because an agent reproduces the frame it is given, and the frame it is
usually given is the builder's.

The verify step is `cxp doctor`. The record step is §4.5.

### 4.7 `cxp doctor` — the verbose face

Every finding from `health.py`, plus inventory: every install of every surface
(all `installed_plugins.json` entries, all scopes), the receipt's source, the
pin, the marketplace clone's fetch age, the hosted build, the install record.
Exit 0 clean / 1 on any finding. Read-only; no `--fix`.

**Acceptance:** run on a machine in the 13-day drift state, it must not report
healthy. Run on a tenant pinned `~= 0.42`, it must warn.

---

## 5. Order of work

1. `health.py` with `plugin_vs_cli` (both directions) and `stale_mcp`; tests with
   both-direction fixtures and the bash-vs-Python agreement test.
2. Tenant hook calls `cxp doctor --brief`; plugin hook gains its one-comparison
   warn above the guard; the plugin's two scripts merge into one entry.
3. `cxp sync` raises the pin floor (§4.3); `pin_floor` check; `version_lock`.
4. `[install]` record schema; sync refreshes it; `install_record` check.
5. `cxp doctor` verbose; `hosted_vs_local`.
6. Install payload + `mc-2` pointer — **after** the convention issue is filed
   (§7), so the payload does not set the convention by shipping first.

Before step 6, and independent of it: **a bounded read-only pass over tenant
commits in the thirteen-day window** (2026-09-04 → 09-17) for anything else the
0.108.1 plugin wrote. A couple of hours; a dirty result changes the severity
framing of the whole issue.

---

## 6. Explicitly dropped

- **Profiles** — two machines, two shapes, neither fits.
- **`--fix`** — the traps are discovery problems, and the one thing that most
  needs fixing (a stale subprocess) cannot be fixed by reinstalling.
- **"One line total" across registrations** — no mechanism; two lines, each
  silent when healthy.
- **"N minors behind"** — replaced by a moving floor.

---

## 7. The convention issue — filed now

Tony's argument, which the review of v03 turned from proposal into evidence:
three consecutive artifacts were addressed to a reader who was not there, and an
agent asked to draft them inherits the author's vocabulary rather than
translating it. The missing thing is not documentation but *the context a
machine needs to choose the right surface on a person's behalf*, committed into
the repo where a cold session will read it first.

It is larger than #296 and gets its own issue **before** §4.6 ships, because
otherwise the payload sets the convention implicitly — which is how the original
unsupported configuration came to exist.

---

## 8. Still open

1. **Does Claude Code run same-matcher hooks in parallel?** Decides whether
   merging the plugin's two scripts is optional or required.
2. **Does CI's `[cp-sync]` commit `.claude/hooks/` diffs?** It is the delivery
   path for the tenant-side check; if not, the fix reaches nobody who has not run
   `cxp sync` by hand.
3. **`hosted_vs_local`** — does hosted deployment pin to the CLI, or can they
   drift? Whichever is true should be written down.
4. **The marketplace tracks `ref: "main"`; the CLI installs from a tag.** Two
   halves with structurally different update semantics. Raised by the gap
   analysis, not yet addressed anywhere.
5. **Marcello is unaudited.** Two machines refuted two versions of this plan.
