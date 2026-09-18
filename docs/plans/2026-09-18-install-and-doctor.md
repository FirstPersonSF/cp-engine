---
Project: cp-engine
Provenance: Version 03 | 2026-09-18
Filename: 2026-09-18-install-and-doctor.md
Author: Claude
---

# Close the hook gap, then report what is running

**Issue:** [cp-engine #296](https://github.com/FirstPersonSF/cp-engine/issues/296)
**Status:** v03 — v02 reviewed on Tony's machine and amended. **The review found
a defect that would have shipped Phase 1 broken** (a third silence v02 missed),
supplied a better warning line, answered both design asks as engineering calls,
added two findings, and reversed v02's call on when to file the convention issue.

> **v01 is superseded.** It diagnosed "a handoff between two correct
> mechanisms" and proposed a five-surface scanner plus a human-facing install
> page. An adversarial review refuted the first; Tony's machine audit
> (`docs/plans/2026-09-18-install-audit-prompt.md` → his report, 2026-09-18)
> refuted the second and one factual claim I made about his machine. What
> survives is smaller and lands in different places.

---

## 1. What actually failed

A `cxp` twelve releases behind its plugin ran a routine render and rewrote
**54 provenance stamps backwards, one each across 54 files**, in a commit
touching 58 files in total (cp `393ad9c8`). Tony caught it by hand.

> **The figure, pinned.** Two numbers were in circulation: "34 files"
> (`improvements.md`, written the night of) and v02's "54 stamps across 58
> files", which conflated stamp count with commit size. Derived from the commit:
> `git show 393ad9c8 | awk '/^diff --git/{f=$3} /^\+Provenance.*0\.108\.1/{print f}' | sort -u`
> → **54 distinct files**, one stamp each. The night-of 34 was an undercount.
> Quote 54/54/58.

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
(`0951d0b6`) and never moved through 80 releases. *(Tony's audit first reported
2026-08-28 from `git blame`; the `^` there marks a boundary commit — the oldest
in his clone's history — so blame reported where the clone begins, not where the
line was authored. He corrected it himself. Authorship stands, date superseded.)* **Every tenant carrying that
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

### Phase 0 — one line of SessionStart output, total

**Ships before Phases 1 and 3, not after.** Retrofitting a budget onto three
shipped hooks does not happen, and this plan is about to add two more printers to
a surface that already has one.

**The rule** (Tony's, adopted): *cp gets one line of SessionStart output, and
everything it wants to say competes for it.* Phases 1, 2 and 3 do not each get a
line — they each produce a **finding**, and one consolidated reporter prints the
highest-severity finding plus a count of the rest (`+2 more, ask for details`).
Silent when healthy.

**The reasoning is this plan's own thesis, inverted.** #296 exists because a
signal structurally unable to fail is worthless. The converse is equally true and
less often said: **a signal that always prints is also unable to fail**, because
it stops being read. Four independent hooks each convinced their own line matters
is exactly how that happens, and no single author ever decides to build it.

It also answers ask 1 by construction. Tony will act on SessionStart output
*"only if it looked alarming"* — and a line that appears **only** when something
is wrong is alarming by virtue of appearing at all. No adjective required.

**Scope:** a reporter that collects findings from the existing hooks
(`sync-cli-version.sh`, `tenant-freshness.sh`) plus the new ones, ranks them, and
prints one line. Severity ordering and the `--details` path are the design work.

### Phase 1 — close the hook gap (the fix)

**The tenant deferral in `sync-cli-version.sh` is correct and stays.** Two hooks
installing the CLI would fight. What changes is that deferring the *install*
must not also defer the *observation*.

> **⚠ There is a THIRD silence, and v02 missed it** (Tony's Claude, 2026-09-18;
> verified). Below the deferral sits the no-downgrade guard:
>
> ```bash
> _highest=$(printf '%s\n%s\n' "$PLUGIN_VERSION" "$INSTALLED_VERSION" | sort -V | tail -1)
> if [ "$_highest" = "$INSTALLED_VERSION" ]; then
>     exit 0          # installed is ahead → no-op
> fi
> ```
>
> Run against the incident's own numbers — plugin `0.108.1`, CLI `0.119.0` —
> `sort -V` returns `0.119.0`, the guard fires, `exit 0`. **The drift on Tony's
> machine was CLI-ahead-of-plugin**, so even with the deferral removed entirely
> this hook would still have said nothing.
>
> **This would have shipped Phase 1 broken.** The natural implementation reuses
> the comparison logic already in the file, and that logic contains the guard —
> so the §5 control fixture (plugin 0.108.1 / CLI 0.119.0) is precisely the
> direction the guard suppresses. The test would have passed by being silent for
> the wrong reason.

Inside a tenant, compare **plugin version vs installed CLI** and warn on any
mismatch. Warn only — no install, no fight. Three constraints, all load-bearing:

1. **Direction-agnostic.** Any mismatch warns. *"Installed is ahead"* is not a
   healthy state; it is half of the state that caused this incident.
2. **Placed above the equality check and the no-downgrade guard**, not below.
   The guard keeps its job for *installing* and gets no vote on *observing* —
   the same separation this phase already makes for the deferral, one line lower.
3. **The remedy follows the direction.** CLI behind plugin →
   `uv tool install --force --reinstall`. Plugin behind CLI (the incident) →
   `claude plugin update`. A single hard-coded remedy is wrong half the time.

**The warning text** — Tony's wording, chosen over v02's draft:

```
[cp] Your cp tools are out of sync with each other and may
     save bad data. Say "update cp-engine" and this session
     will fix it.
     (plugin v0.108.1, engine v0.119.0)
```

Why this beats the draft it replaces:

- **"may save bad data"**, not *"may write stale results."* *Stale* reads as
  slightly old and survivable. The actual failure wrote wrong history into
  committed files.
- **One action, phrased as a sentence to say rather than a command to run.** The
  typed command leaves the body entirely — he does not run
  `claude plugin update`, so offering it as a co-equal branch adds a path he will
  not take. Keep it for the log or `--verbose`.
- **Version numbers last, parenthesised.** They are evidence for whoever debugs
  it, not information he acts on.
- **It survives constraint 3**: *"say update cp-engine"* is true in both drift
  directions, because an agent can act on the intent either way. The
  direction-specific command belongs in the verbose detail, not the line.

**Alarming by placement, not by adjective.** Asked whether SessionStart output
would reach him, Tony said *"only if it looked alarming"* — routine grey text
gets skimmed. The answer is to make the line visually distinct (prefix, colour,
its own position above the housekeeping) while keeping this plain wording, which
also preserves stronger language for something that genuinely blocks.

**Control — both directions, or the guard bug survives.** Today's hook is silent
on both; a correct Phase 1 warns on both, with a *different remedy line* in each:

| Fixture | Today | Required |
|---|---|---|
| plugin 0.108.1 / CLI 0.119.0 (the incident) | silent | warn → `claude plugin update` |
| plugin 0.120.5 / CLI 0.119.0 | silent | warn → `uv tool install --force --reinstall` |

Testing only one direction leaves the defect in place.

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

**Where it lives — answered, asymmetrically.** Full payload in **cp-engine**,
versioned with the thing it describes: instructions that live apart from their
tool drift from it, which is the pin's defect class and this plan should not
create a second instance while fixing the first. **Pointer in `mc-2`** — a few
lines in its `CLAUDE.md` naming what cp-engine is and where the payload lives.
Not a copy. The evidence is behavioural rather than stated: the experiment
already ran in September, when a cold session pointed at `mc-2` got far enough to
install four surfaces. That result beats an opinion.

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

## 4b. Two findings v02 did not cover

Both from Tony's review. Scoped here rather than deferred, because each is the
§1 failure shape one layer over.

### 4b.1 Hosted and local expose the same verbs, independently versioned

`.mcp.json` carries `cp-hosted` (HTTP, Railway) and `cp-sources` (`cxp mcp`,
local) **simultaneously** — that is Tony's machine and Drew's. At least six verb
names exist on both: `list_spine_elements`, `list_project_sources`,
`pull_spine_element`, `list_commitments`, `list_project_meetings`,
`pull_project_source`.

So one session holds two implementations of the same operation at two
independently-deployed versions, **and nothing compares them.** Phase 5 reports
the hosted build as inventory; inventory is not comparison.

**Worse than the drift being fixed in one respect:** CLI/plugin drift produced
wrong *output*. This can produce **two different answers to the same question
inside one session**, with no indication which was used.

**Action:** either add hosted-vs-local verb-version comparison to Phase 0's
consolidated check, **or** state explicitly that deployment pins them together
and they cannot drift. If that is true it is worth writing down — nothing on
either machine currently shows it.

### 4b.2 Thirteen days of stale-plugin writes were never assessed

The provenance damage is known and now blocked (v0.120.3). But the plugin ran
twelve releases behind for **thirteen days**, and the drifted half was the skills
and slash commands. `/cp-wrapup` ran from that cache the entire time.

**Nobody has asked what else it wrote.** Exec Summaries, session captures,
decision sweeps, commitment routing — all authored by a 0.108.1 skill against a
0.119.0 engine. This plan treats the incident as one render event; the exposure
window is thirteen days of routine writes.

**Action:** a bounded read-only pass over tenant commits in that window, before
Phase 4. Not a phase — a couple of hours. A clean result is worth knowing; a
dirty one changes the severity framing of the whole issue.

---

## 5. Verification

- **Phase 1 control — BOTH directions.** plugin 0.108.1 + CLI 0.119.0 (Tony's
  exact state, and the direction the no-downgrade guard suppresses) *and* plugin
  0.120.5 + CLI 0.119.0. Today's hook is silent on both; a correct Phase 1 warns
  on both with a different remedy each. **Testing one direction leaves the guard
  defect in place** — which is how v02 would have shipped a passing test over a
  broken phase.
- **Phase 0 control:** three simultaneous findings must produce one line plus a
  count, not three lines.
- **Phase 2 control:** a tenant pinned `~= 0.42` against engine 0.120.x must
  warn. Every tenant was in this state until today.
- **Phase 3 control:** a `cxp mcp` process started before the install mtime must
  be reported. Two exist on Tony's machine right now, left running as evidence.
- **Phase 5 control:** doctor run on a machine in the 13-day drift state must
  **not** report healthy.
- Verify the **installed interpreter**, never the source tree.

---

## 6. The convention issue — file it NOW, not after Phase 4

Tony's report closes with a convention proposal: make the repository the unit of
instruction and self-describing to a cold agent, on the argument that the native
interface of a language model is language, and that the missing product is the
context a machine needs to choose the right surface on a person's behalf.

**v02 said "file it separately, after the core ships." His review changed that,
and the evidence is a table about us:**

| Artifact | Addressed to | Was that reader there? |
|---|---|---|
| v01's install one-pager | someone who reads docs before installing | **No** — an agent installed it |
| v01's `cxp sync` warning | someone who runs `cxp sync` | **No** — he has never run it |
| v02's review request | someone who knows the four surfaces | **No** — two of five asks were unanswerable |

**Three for three is not a series of slips. It is a pattern with a cause:** the
default when writing is to write from inside the system, and nothing in the
process interrupts that. Two of our five review asks could not be answered
because they required modelling the system — *which repo should hold the
payload*, *what is the fatigue budget* — and the terms in them (tenant-freshness
line, version pin, background processes) had no referent for the person being
asked.

**A usable test, adopted:** can this be answered by *reacting to something*, or
does it require *knowing how it works*? The first is a user question. The second
is the builder's call, confirmed later by watching what the user does. We asked
two builder questions as if they were user questions.

**And the part that bites hardest:** an agent asked to draft the question does
not fix this — **it amplifies it**, because it inherits the author's vocabulary by
default and has no instinct to translate. That is the same mechanism that let a
session improvise an install in September and write nothing down. *Agents
reproduce the frame they are given unless something tells them not to.*

So Phase 4's payload needs an explicit statement of **who the reader is and what
they can be assumed to know** — not only what to install. Without it, the payload
is one more correct artifact aimed past its audience, written by the actor most
likely to aim it there.

**Decision, reversed from v02:** file the convention issue **now**. Phase 4 will
otherwise set the convention implicitly by shipping first — which is precisely
how the original unsupported configuration came to exist. The shippable core
(Phases 0–3) does not wait on it, but the convention must exist before Phase 4
writes the payload that would silently become the standard.

---

## 7. Open questions

**Answered by the review** — kept here with their answers so the reasoning is not
lost: Q2 fatigue budget → Phase 0, one line total. Q3 payload location → Phase 4,
cp-engine with an `mc-2` pointer. Ask 1 (would SessionStart reach him) → yes,
*"only if it looked alarming"*, which Phase 0 satisfies by construction.

Still open:

1. **Phase 2 placement** — tenant self-check, or is `release.py`'s minor-bump
   reminder enough? (Lean self-check: the reminder fires on the releaser's
   terminal, not on the machine carrying the stale pin.)
2. **4b.1** — add hosted-vs-local verb comparison to Phase 0, or establish that
   deployment pins them together? Needs someone to check how the hosted server's
   verb set is built relative to the CLI's.
3. **Phase 0 severity ordering** — what outranks what, when three findings
   compete for one line.
4. **Marcello is still unaudited.** Two machines, two shapes, and the second
   refuted v01 while the review of v02 found a shipping defect. A third may do it
   again.
