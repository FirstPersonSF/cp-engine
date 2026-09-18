---
Project: cp-engine
Provenance: Version 02 | 2026-09-18
Filename: 2026-09-18-install-and-doctor.md
Author: Claude
---

# Close the hook gap, then report what is running

**Issue:** [cp-engine #296](https://github.com/FirstPersonSF/cp-engine/issues/296)
**Status:** v02 — rewritten against a second-machine audit that refuted v01's
central causal claim. Five fixes already shipped; the doctor is re-scoped.

> **v01 is superseded.** It diagnosed "a handoff between two correct
> mechanisms" and proposed a five-surface scanner plus a human-facing install
> page. An adversarial review refuted the first; Tony's machine audit
> (`docs/plans/2026-09-18-install-audit-prompt.md` → his report, 2026-09-18)
> refuted the second and one factual claim I made about his machine. What
> survives is smaller and lands in different places.

---

## 1. What actually failed

A `cxp` twelve releases behind its plugin ran a routine render and rewrote **54
provenance stamps backwards** across 58 files. Tony caught it by hand.

**The mechanism, verified on two machines:**

```
plugin/hooks/sync-cli-version.sh   compares PLUGIN vs CLI  ← the only check that
                                                             could see this drift
   └─ walks up for .cp-engine.toml, exit 0 inside a tenant  ← deliberate (#28)

.claude/hooks/check-cp-engine-version.py   compares CLI vs TENANT PIN
   └─ pin was `~= 0.42`, satisfied by 0.42.0 … 0.120.x     ← 78 releases wide
```

Every cp session starts inside the tenant. So the check that *can* see
plugin-vs-CLI drift disables itself exactly where all the work happens, and
hands authority to a check that **structurally cannot see it** —
`check-cp-engine-version.py` never reads the plugin version at all.

### 1.1 The correction I got wrong, and why it matters

v01 claimed the tenant hook had been printing *"Reinstall manually"* at Tony for
months, because he has no local `cp-engine` clone (confirmed: none on disk, no
`[local-repos]` entry).

**It printed nothing. It returned green.** Verified in the source:

```python
ok = _satisfies(installed, pin)
if ok is None:  return 0   # can't evaluate
if ok:          return 0   # healthy — the common path, fully silent
repo = _read_repo_path(root)          # ← never reached while the pin passes
```

The missing clone never mattered. Tony confirms he has **never seen** either
warning — predicted by the code, confirmed by the user.

That is a stronger result than "the self-heal was broken." **The self-heal
reported healthy while twelve releases of drift accumulated underneath it.**
A doctor that only compares installed versions would have called his machine
healthy for thirteen days, because both halves *were* installed — the drift was
*between* them, and the only surface that compares them had switched itself off.

### 1.2 The pin is a shared defect, and it is mine

`.cp-engine.toml`'s `version = "~= 0.42"` was set by me on 2026-06-30
(`0951d0b6`) and never moved through 80 releases. **Every tenant carrying that
pin has the same dead check.** It lives in a committed file, not on a machine —
so no per-machine scan finds it, and fixing one laptop fixes nothing.

Tightened to `~= 0.120` on 2026-09-18 (`4c5eb9c9`). That closes the instance,
not the class: nothing prevents the next pin from going stale the same way.

### 1.3 What reproduced on machine two, and what did not

| v01 hypothesis | Tony's machine |
|---|---|
| Stale `cxp mcp` subprocesses | **Confirmed** — 2 of 3 stale, serving 0.119.0 bytecode, live |
| 0.40.0 downgrade bomb | **Absent** — 2 cached versions, oldest 0.108.1, no `cp --version` probe |
| 29 cached plugin copies, each with hooks | **Drew's machine only** — he has 2, byte-identical |

One hypothesis generalised. **The stale-MCP finding is therefore the only
machine-local item that has earned a place in v2**, and it needs its own
acceptance criterion: neither `--fix` nor any reinstall clears a stale
subprocess. Only killing it does. A `--fix` that reinstalls while stale servers
keep answering would report success and change nothing the user experiences —
the same trap shape, one layer up.

---

## 2. The finding that re-scopes the work

> **Tony, asked how he installed cp-engine:** *"I pointed a Claude Code session
> in terminal at the mc-2 repo and asked it if it knew what 'the spine and cp'
> was, and how to set up my computer so it could access it… I did not know what
> it did, but after that, it 'worked'."*

Corroborated by timestamps: tenant hooks 09-03 07:38, plugin 09-04 07:05,
`.cp-engine.local.toml` 07:06 — one sitting, machine-paced.

**The install was authored by an agent, not a person.** v01 proposed "one-page
install docs — the thing you can send a new person." On that machine **there was
no new person**. A session improvised a four-surface install from a one-sentence
goal, correctly enough to run in production for two weeks, and wrote nothing
down — not what it installed, not at what versions, not back to the person whose
machine it was.

So the install path must be **written for an agent, placed where an agent
looks** (in the repo, discoverable from `mc-2`), and **must emit its own
record**, because the actor that runs it will not.

### 2.1 And the warning has to land where he will see it

Tony rarely types slash commands in the normal flow of work — he states intents
(*"update the spine"*) and lets the session choose which command to call.

> **Terminology, since this plan uses one word for two things.** *Installable
> surfaces* are the four things that get installed and can drift: the `cxp` CLI,
> the plugin, the hosted connector, the tenant clone. *Command surfaces* are what
> a session calls to do work — slash commands, `cxp` verbs, MCP tools. Tony
> chooses neither by name; he states an outcome. The drift was in an installable
> surface and showed up (invisibly) in a command surface.

Two consequences:

- **The drifted half was the half he could never see.** The twelve-release gap
  was in the plugin — skills and slash commands. `/cp-wrapup` ran from a 0.108.1
  cache for thirteen days. He could not notice it misbehaving because he never
  invoked it by name.
- **v01's item 4 was directionally right and mis-placed.** It put the warning in
  `cxp sync`. **He does not run `cxp sync` either.** SessionStart output is the
  only thing he reliably reads — and both existing hooks already run there, so
  the fix is placeable today.

**Vocabulary — and a distinction worth keeping straight.** The spine is the
distilled-memory layer in MC-2: versioned elements across layers, bound to
sources and deliverables. That is what Tony names when he says *"update the
spine"* or *"can you access the spine"* — and he is naming it correctly. It is
the thing he works on.

What has no name he would recognise is the **local toolchain that reaches it**:
the `cxp` CLI, the Claude Code plugin, the hosted MCP connector, and the tenant
clone. Four independently-installed surfaces, and the incident was a drift
*between two of them*. So a warning phrased as *"your plugin is 0.108.1 and your
CLI is 0.119.0"* names install mechanics that sit a layer below anything he
interacts with.

**The gap is not that he lacks a word for the spine. It is that the toolchain
reaching it has four names and no collective one.** Output in surface-speak will
not be read; output that says "the spine is broken" would be wrong, because the
spine is fine — the install that talks to it is not.

### 2.2 The profile model is refuted

Tony's `.mcp.json` carries `cp-hosted` **and** `cp-sources` side by side. He runs
hosted and local simultaneously. `full` / `hosted-only` has no name for him, and
he is half the known population.

**Drop profiles from v2.** Two shapes cannot describe two machines; a third
invented now would be a guess. Revisit only if a real configuration needs a name.

---

## 3. What is already shipped

| | Release | What it does |
|---|---|---|
| Provenance monotonicity guard | v0.120.3 | A render cannot lower a stamp. Blocks the damage. |
| Marketplace refresh fixed | v0.120.4 | Pointed at the clone, not the cache. It had **never executed**. |
| Minor-release pin reminder | v0.120.4 | Names the tenant edit a minor bump needs. |
| Actionable `EngineVersionMismatch` | v0.120.5 | Names a fix that works without a local clone. |
| Tenant pin `~= 0.42` → `~= 0.120` | tenant | The hard gate the soft checks stood in for. |

Also: the 0.40.0 downgrade bomb on Drew's machine, defused and disarmed.

**What none of these do:** make plugin-vs-CLI drift visible inside a tenant.
That is the actual failure, and it is still open.

---

## 4. Build, in order

### Phase 1 — close the hook gap (the fix)

**The tenant deferral in `sync-cli-version.sh` is correct and stays.** Two hooks
installing the CLI would fight. What changes is that deferring the *install*
must not also defer the *observation*.

Inside a tenant, before `exit 0`, compare **plugin version vs installed CLI** and
warn on mismatch. Warn only — no install, no fight. This is the comparison
nothing performs today, in the one place every session passes through.

```
[cp] Your cp install is out of step with itself — the slash commands are
     v0.108.1, the engine they call is v0.119.0. Anything that renders or
     syncs may write stale results. Ask this session to update cp-engine,
     or run:  claude plugin update cp-engine@cp-engine
```

Three deliberate choices. It names the **install**, not the spine — the spine is
fine; the toolchain reaching it is not, and saying otherwise would send someone
looking in the wrong place. It names the **consequence** (*stale results*) rather
than only the numbers, because two version strings do not tell a reader whether
to care. And it offers the **conversational path first**, because that is how the
work is actually driven here — the typed command is the fallback, not the lead.

⚠ **This wording is a draft and Tony should replace it.** It is my guess at what
he would act on at 8am, and the last time I guessed about his machine I was
wrong in a way only he could catch. Asking him to write the line is item 2 of
the review request.

**Control:** a fixture with plugin 0.108.1 and CLI 0.119.0 inside a tenant must
**fail** against today's hook (silent) and warn after the change. That is the
exact state of Tony's machine for thirteen days.

### Phase 2 — the pin must be capable of failing

A pin whose floor is 78 releases down is indistinguishable from no pin. Add a
check — in `cxp sync` and in the tenant hook — that the pin's floor is within N
minor versions of the installed engine, warning when it is not.

**This is the finding no per-machine doctor can reach**, because the defect is
in a committed file shared by every tenant. It is also the cheapest item here.

Open: whether `release.py`'s existing minor-bump reminder is enough, or whether
the tenant should self-check. Lean self-check — the reminder fires on the
releaser's terminal, not on the machine carrying the stale pin.

### Phase 3 — stale `cxp mcp` detection

Reproduced on both machines; invisible by construction, since the tools answer
normally from old code. Detection is read-only and needs no round-trip: a
process whose start time predates the install mtime is stale.

Report at SessionStart alongside Phase 1. **Do not auto-kill** — a running MCP
server belongs to a live session, possibly someone else's work. Name the PIDs
and say `/mcp` restarts it.

**Acceptance:** `--fix`, if it ever exists, must state that it cannot clear this.

### Phase 4 — the install payload, written for an agent

Not a one-pager for a human. In the repo, weighted so a cold session reads it
first, covering: **what this is** — stated as what the tools give you access to
(the spine, the tenant, the sync), not as an inventory of the four things being
installed; **the interaction model**; and **install / verify / upgrade /
uninstall as executable instructions**, including what a correct install looks
like afterwards so the agent can check its own work and report it.

The ordering matters and is the lesson from §2.1: a cold session pointed at this
repo should learn *what the system does for its user* before it learns that the
toolchain has four parts. Tony's install succeeded on exactly that framing —
*"what is the spine and cp, and how do I set my computer up to access it"* — and
failed only because nothing in the repo answered it.

The human interface collapses to one sentence, learnable once: *point a session
at the repo and ask what this is and how to install it.*

**This is the phase most likely to over-run.** It is the instance of a larger
convention question (see §6) — build the cp-engine payload, do not solve the
org-wide version here.

### Phase 5 — `cxp doctor`, last and smallest

Read-only. Reports every install of every surface, the pin and **its width**,
the marketplace clone's fetch age, stale MCP processes, and the hosted build.

**It is last because the audit demoted it.** It would not have caught this
incident on either machine: both halves were installed, the drift was between
them, and the worst defect was in a shared file. Doctor is for answering *"what
is running here?"* on demand — a real need, but not the fix.

**Dropped from v01:** profiles (§2.2), `--fix` (nothing in the audit shows a
fix-shaped gap; the traps are discovery problems, and the one thing that most
needs fixing — a stale subprocess — cannot be fixed by reinstalling).

---

## 5. Verification

- **Phase 1 control:** plugin 0.108.1 + CLI 0.119.0 inside a tenant — silent
  today, warns after. Tony's exact state.
- **Phase 2 control:** a tenant pinned `~= 0.42` against engine 0.120.x must
  warn. Every tenant was in this state until today.
- **Phase 3 control:** a `cxp mcp` process started before the install mtime must
  be reported. Two exist on Tony's machine right now, left running as evidence.
- **Phase 5 control:** doctor run on a machine in the 13-day drift state must
  **not** report healthy.
- Verify the **installed interpreter**, never the source tree.

---

## 6. Explicitly out of scope

Tony's report closes with a convention proposal: make the repository the unit of
instruction and self-describing to a cold agent, on the argument that the native
interface of a language model is language, and that the missing product is the
context a machine needs to choose the right surface on a person's behalf.

**The diagnosis is right and the scope is larger than #296.** It should be its
own issue. #296 has a shippable core — close the hook gap, make the pin capable
of failing, surface at SessionStart — and that core must not wait on an
org-wide convention. Phase 4 builds cp-engine's instance of it; the convention
itself is filed separately.

---

## 7. Open questions

1. **Phase 2 placement** — tenant self-check, or is `release.py`'s reminder
   enough? (Lean self-check.)
2. **Warning fatigue** — Phases 1 and 3 both add SessionStart output, which
   already prints tenant-freshness. What is the budget before it becomes noise
   nobody reads?
3. **Does Phase 4 belong in cp-engine, or in `mc-2`** — the repo a cold session
   was actually pointed at?
4. **Marcello is still unaudited.** Two machines, two different shapes. A third
   may refute something here, as the second refuted v01.
