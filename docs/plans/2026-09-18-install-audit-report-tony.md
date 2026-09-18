---
Project: cp-engine (FirstPersonSF/cp-engine)
Provenance: Version 01 | 2026-09-18
Filename: 2026-09-18-cp-engine-install-audit-v01.md
Author: Claude (on T.Welch's machine, at D.Fiero's request)
---

# cp-engine install audit — Tony's MacBook Pro (M2 Max, macOS Tahoe)

**Read-only. Nothing was changed, upgraded, or repaired.** Every drifted state
described below is still in place.

Run: 2026-09-18, from the tenant at `/Users/tonywelch/Documents/_claude/1P/cp`.

---

## CORRECTIONS TO "WHAT WE ALREADY BELIEVE"

Four of your five beliefs hold. One consequence you drew from them is wrong,
and the way it is wrong is the most important finding in this document.

| # | Belief | Verdict |
|---|---|---|
| 1 | Both halves installed locally; 0.119.0 CLI / 0.108.1 plugin on 9/17 | **Correct.** Artifact trail confirms it (below). |
| 2 | uv receipt pins an exact git rev | **Correct.** Now `?rev=v0.120.2`. |
| 3 | `claude plugin install` refused with "already installed" | **Unverifiable from disk.** No log survives. Tony to confirm. |
| 4 | Stale CLI would have walked provenance backwards across 34 files | **Correct as to the commit shape** (`29947a0` is the roll-up alone). Tony to confirm intent. |
| 5 | `cxp capture-session` runs here — CLI in real use | **Correct.** `8eac404` is a session capture. |

### The correction: hypothesis A is right about the clone and wrong about the consequence

You wrote that, absent a local clone, the tenant hook *"has been printing
'Reinstall manually' rather than fixing anything, for months."*

It has not been printing anything. **It has been returning green.**

The tenant hook compares the installed CLI against the tenant pin in
`.cp-engine.toml`. That pin is:

```toml
[engine]
version = "~= 0.42"
```

Replaying the hook's own `_satisfies()` against its own pin:

```
installed 0.120.2    vs pin '~= 0.42'  -> satisfies = True
installed 0.119.0    vs pin '~= 0.42'  -> satisfies = True
installed 0.108.1    vs pin '~= 0.42'  -> satisfies = True
installed 0.42.0     vs pin '~= 0.42'  -> satisfies = True
installed 0.41.0     vs pin '~= 0.42'  -> satisfies = False
```

`~= 0.42` is satisfied by **every 0.x release from 0.42 upward** — 0.42 through
0.999. The hook took its `if ok: return 0  # healthy — the common path, fully
silent` branch on every session for the entire drift window. The missing clone
never mattered, because the code path that reads the clone was never reached.

So there was no nag to ignore. This is a stronger result than "the self-heal was
broken": **the self-heal reported healthy while twelve releases of drift
accumulated underneath it.**

### And the second half of the correction: the check that *would* have caught it disables itself here

The plugin hook `sync-cli-version.sh` compares **plugin version to CLI
version** — precisely the comparison that would have shown 0.108.1 vs 0.119.0.
It never runs in Tony's sessions. Its first executable block:

```bash
# ── Tenant deferral (arch-phase-3, issue #28) ──
_dir="$PWD"
while [ "$_dir" != "/" ] && [ -n "$_dir" ]; do
    if [ -f "$_dir/.cp-engine.toml" ]; then
        exit 0
    fi
    _dir=$(dirname "$_dir")
done
```

Every cp session Tony runs starts inside the tenant. The hook finds
`.cp-engine.toml`, exits 0, and defers — as designed — to the tenant hook.

**The two mechanisms did not independently fail. They composed into a failure.**
The deferral hands authority from a check that can see plugin-vs-CLI drift to a
check that structurally cannot: `check-cp-engine-version.py` never reads the
plugin version at all. It compares CLI to a pin, and the pin is a floor eighty
releases below current.

This is the #296 thesis ("a green light that cannot go red") as a concrete
mechanism rather than an analogy — and it is worse than a single blind check,
because the blinding is *deliberate*, documented, and correct-looking.

**The pin is not Tony's.** `git blame` on `.cp-engine.toml`:

```
^862642a (drewcanon 2026-08-28 07:43:59 -0700 5) [engine]
^862642a (drewcanon 2026-08-28 07:43:59 -0700 6) version = "~= 0.42"
```

Set 2026-08-28, committed, never moved as the engine went 0.42 → 0.120. **Every
tenant carrying that pin has the same dead check.** This is a shared-artifact
defect, not a machine-local one, and it will not show up in any per-machine
doctor that scans installs only.

---

## What this machine actually is

| Surface | State | Knew about it? |
|---|---|---|
| `cxp` CLI | **0.120.2**, `uv tool`, pinned `?rev=v0.120.2`, installed 2026-09-17 23:39 | Yes |
| Claude Code plugin | **0.120.2**, user scope only, updated 2026-09-17 23:44 | Yes |
| Marketplace clone | `~/.claude/plugins/marketplaces/cp-engine` @ `278e28e` (v0.120.2), fetched 2026-09-17 23:43 | **No** |
| Hosted MCP connector | `cp-hosted` → `https://cp.mc-2.1p.is/mcp`, in tenant `.mcp.json` | Yes |
| Local MCP server | `cp-sources` → `cxp mcp`, in tenant `.mcp.json` | Yes |
| Tenant clone | One only: `1P/cp`, HEAD `4b74d4f` | Yes |
| Local `cp-engine` clone | **None anywhere on disk** | — |
| Plugin cache | 2 versions: `0.108.1`, `0.120.2` | **No** |
| Running `cxp mcp` | **3 processes — 2 of them stale** | **No** |

**Both halves are current, and agree, as of right now (0.120.2 / 0.120.2).**
Remote latest tag is **v0.120.3**, so there is a one-patch gap — trivial, noted
for completeness.

**A correction to your framing, not to a numbered belief:** Tony runs *both*
the hosted connector and the local CLI. `.mcp.json` carries `cp-hosted` and
`cp-sources` side by side. He is not a "full install" or a "hosted-only"
profile — he is both at once, which your proposed two-profile model
(`full` / `hosted-only`) does not have a name for.

---

## A. Local clone and tenant hook

```
$ ls -d ~/Documents/Python/cp-engine
NO local cp-engine clone at ~/Documents/Python/cp-engine

$ find ~/Documents ~/Projects ~/code ~/src ~/dev -maxdepth 4 -type d -name "cp-engine"
(no results)
```

```toml
# /Users/tonywelch/Documents/_claude/1P/cp/.cp-engine.local.toml
[supabase]
url_ref         = "op://1P Internal Tools/cp-supabase/url"
service_key_ref = "op://1P Internal Tools/cp-supabase/service_key"

[local-repos]
"mc-2"    = "/Users/tonywelch/Documents/_claude/1P/mc-2"
"storyos" = "/Users/tonywelch/Documents/_claude/canonic/storyos"
```

**No `cp-engine` key under `[local-repos]`, and no clone on disk.** Confirmed —
the self-heal path in `check-cp-engine-version.py` cannot execute.

But per the correction above, it was never reached. See the `_satisfies()`
replay in the corrections section.

**Answer to your related question** — "has Tony ever seen `[cp-engine] cxp CLI
version drift…` or `[cp-engine version check]…`?" — **the artifacts predict he
has not.** Both message paths are unreachable on this machine: the plugin's
message is behind the tenant-deferral `exit 0`, and the tenant hook's message is
behind a pin comparison that always passes. Tony's own answer is below; if he
says he *has* seen one, that contradicts the code and needs chasing.

---

## B. Plugin copies on disk

**No second install. No bomb. Your hypothesis B does not apply to this machine.**

Registered installs — the complete `cp-engine` section of
`~/.claude/plugins/installed_plugins.json`:

```json
"cp-engine@cp-engine": [
  {
    "scope": "user",
    "installPath": "/Users/tonywelch/.claude/plugins/cache/cp-engine/cp-engine/0.120.2",
    "version": "0.120.2",
    "installedAt": "2026-09-04T14:05:26.732Z",
    "lastUpdated": "2026-09-18T06:44:07.052Z",
    "gitCommitSha": "278e28ec87ed1dc7148e69bfef42371a8f14829c"
  }
]
```

**One entry. User scope. No `projectPath`. No project-scoped install anywhere.**

Cached versions — **two**, not twenty-nine:

```
$ ls -la ~/.claude/plugins/cache/cp-engine/cp-engine/
drwxr-xr-x@ 9 tonywelch staff 288 Sep 17 23:44 0.108.1
drwxr-xr-x@ 8 tonywelch staff 256 Sep 17 23:44 0.120.2

$ find ~/.claude/plugins/cache/cp-engine/cp-engine -name hooks -type d | wc -l
       2
```

**Oldest cached version: 0.108.1.** Well above the 0.102 danger line.

The dangerous probe is absent:

```
$ grep -rn "cp --version" ~/.claude/plugins/cache/cp-engine/
NONE FOUND — no /bin/cp probe in any cached hook
```

And the two cached hook directories are **byte-identical**:

```
$ diff -r .../0.108.1/hooks/ .../0.120.2/hooks/
IDENTICAL
```

Note what that last line means for your mental model of the incident: the 0.108.1
plugin's *hooks* were never the problem. The twelve releases of drift were in the
skills and slash commands — `/cp-wrapup` running from a stale cache — not in hook
behavior. The hooks had already been correct since at least 0.108.1.

Also clean: **no stale `cp` shim.** `~/.local/bin/cp` does not exist;
`which cp` → `/bin/cp`.

The install timeline worth having: plugin installed **2026-09-04** at 0.108.1,
updated **2026-09-17 23:44** to 0.120.2. Thirteen days at one version while the
CLI moved. That is the drift window, and it is bounded.

---

## C. Stale `cxp mcp` subprocesses — CONFIRMED, same as your machine

```
$ ps -eo pid,lstart,command | grep "cxp mcp" | grep -v grep
79265 Fri Sep 18 11:37:37 2026  .../cp-engine/bin/python /Users/tonywelch/.local/bin/cxp mcp
 4573 Thu Sep 17 12:49:12 2026  .../cp-engine/bin/python /Users/tonywelch/.local/bin/cxp mcp
13691 Thu Sep 17 13:03:59 2026  .../cp-engine/bin/python /Users/tonywelch/.local/bin/cxp mcp

$ stat -f "%Sm %N" ~/.local/share/uv/tools/cp-engine/uv-receipt.toml
Sep 17 23:39:59 2026 /Users/tonywelch/.local/share/uv/tools/cp-engine/uv-receipt.toml
```

| PID | Started | vs install (Sep 17 23:39) | Serving |
|---|---|---|---|
| 4573 | Sep 17 **12:49** | ~11 h **before** | **0.119.0 — stale** |
| 13691 | Sep 17 **13:03** | ~10 h **before** | **0.119.0 — stale** |
| 79265 | Sep 18 11:37 | after | 0.120.2 — current |

**Two stale processes, live right now, answering tool calls from 0.119.0
bytecode.** Left running deliberately as evidence.

This reproduces your finding exactly, on a second machine, which upgrades it
from anecdote to pattern. Worth noting it is also the *only* one of your three
hypotheses that reproduced.

Receipt contents:

```toml
[tool]
requirements = [{ name = "cp-engine", git = "https://github.com/FirstPersonSF/cp-engine?rev=v0.120.2" }]
entrypoints = [
    { name = "cxp", install-path = "/Users/tonywelch/.local/bin/cxp", from = "cp-engine" },
]
```

Belief 2 confirmed: still an exact-rev pin, so the `uv tool upgrade` no-op trap
is still armed for the next upgrade.

---

## D. Tenant and MCP surfaces

**One tenant on this machine.** `find ~/Documents -name .cp-engine.toml` returns
`1P/cp` and nothing else.

```toml
[engine]
version = "~= 0.42"

[sync]
backend = "mc-2"
cron = "0 * * * *"
```

```
$ ls -la /Users/tonywelch/Documents/_claude/1P/cp/.claude/hooks/
-rwxr-xr-x  8983 Sep  3 07:38 check-cp-engine-version.py
-rwxr-xr-x  6455 Sep  3 07:38 guard-engine-regions.py

$ git -C <tenant> log --oneline -1
4b74d4f mission-control: exec summary back inside budget
```

```json
{
  "mcpServers": {
    "cp-hosted": { "type": "http", "url": "https://cp.mc-2.1p.is/mcp" },
    "cp-sources": { "command": "cxp", "args": ["mcp"] },
    "miro": { "type": "http", "url": "https://mcp.miro.com/" }
  }
}
```

`.mcp.json` is healthy — `cp-sources` already says `cxp`, not the pre-rename
`cp`, so the hook's `_repair_mcp_command` no-ops.

**What works here:** `tenant-freshness.sh`. This session opened with
`[cp] tenant fast-forwarded 6 commit(s) from origin.` — HEAD moved `6f88577` →
`4b74d4f` at session start. That is the one piece of the version/freshness
machinery observably doing its job on this machine.

**Marketplace clone:** at `origin/main`, `278e28e` = v0.120.2, last fetched
2026-09-17 23:43 (~13 h). Not meaningfully stale. Tony did not know it existed
as a separate thing — relevant to your acceptance criterion about reporting its
fetch age, since a surface nobody knows about cannot be sanity-checked by its
owner.

---

## Answers only Tony can give

*Answered by Tony directly, 2026-09-18. Artifact context retained where it
corroborates or sharpens the answer.*

### 1. How did you install cp-engine originally?

> **Tony:** "I pointed a Claude Code session in terminal at the mc-2 repo and
> asked it if it knew what 'the spine and cp' was, and how to set up my computer
> so it could access it, and it said that it did and did its thing. I did not
> know what it did, but after that, it 'worked'."

**This is the answer to your question, and it is not the one the question
anticipated.**

You wrote: *"you assembled a working setup unaided, and how you did that is the
single most useful thing in this document."* He did not assemble it. **He
described an outcome to a Claude session and the session improvised an install**
— no instructions, no doc, no record of what it chose, and no report back to him
about what it had done.

Corroborated by the artifact timing: tenant hooks written 2026-09-03 07:38,
plugin installed 2026-09-04 07:05, `.cp-engine.local.toml` 07:06 — one minute
after the plugin. A single sitting, machine-paced.

This closes the loop on the finding you called the actual one in #296 —
*"someone was running a configuration nobody designed, and nobody knew."* **The
configuration was designed. By an LLM, once, from inference, and the design was
never written down anywhere — including to the person whose machine it was.**

So the gap is not that Tony went off-script. **There was no script, and the
absence of one is what invited an agent to write a private one.** Any install
plan that assumes a human will read instructions is aiming at the wrong actor:
on this machine the installer was an agent acting on a one-sentence goal, and it
will be again.

### 2. What do you reach for, and what do you avoid?

> **Tony:** "I don't use any of those terms specifically per se — I ask my Claude
> Code sessions everything much more conversationally. For example: 'update the
> spine/cp based on this session', or 'can you access the spine? if so, get
> context about job X or Y', or 'update the spine'."

**He rarely invokes slash commands by name in the regular flow of work** —
reserving them for truly base functions. The default is to state an intent and
let the session pick the surface.

Two consequences worth sitting with:

- **The drifted half is the half he never names.** The twelve-release gap was in
  the plugin — skills and slash commands. `/cp-wrapup` ran from a 0.108.1 cache
  for thirteen days. He could not have noticed it misbehaving, because he never
  invoked it as a command; he asked for an outcome and a session chose it for
  him. **A stale command surface is undetectable by a user who works this way**,
  which is a real argument for surfacing drift in session startup rather than in
  command behavior — your proposed item 4, and this is the evidence for it.
- **"The spine" is his unit, not "cp-engine" or "the CLI" or "the plugin."** He
  has one mental object where the system has four installable surfaces. Any
  message, doctor output, or install doc written in surface vocabulary is
  addressed to a reader who is not there.

### 3. What have you worked around?

The workaround is `29947a0` — the roll-up committed alone on 9/17, rather than
letting the render walk provenance stamps backwards across 34 files. Your read
of the commit shape was right, and your read of the call was right. **It was a
joint call: caught and reasoned through in the moment by Tony and the session
together**, not one handing the other a fix.

**An observation about method, because it bears directly on why you know about
any of this.** Across the artifacts this machine holds — the tenant hooks, the
bootstraps, `improvements.md`, the CLAUDE.md tree — a consistent pattern shows
up. Work starts from the end-state objective, the route back to it gets worked
out second, and the route gets written down so it is repeatable. Three steps,
and the third one is habitual rather than occasional: `improvements.md` alone
runs to 3,300+ lines of dated friction entries, including a `cxp doctor`
proposal at line 2871 written well before the incident that produced #296.

So the entry written on the night of 9/17 was not a flash of diligence. **It was
the third step of a standing practice, and it is the only reason the drift
surfaced at all.** No automated check on this machine saw anything — every one
of them reported healthy, as documented above. A written note did the work the
instrumentation could not.

**The same pattern explains the gap in answer 1, and this is the part worth
carrying into the design.** *"Set up my computer so it can access the spine"* is
that identical shape: objective stated, route to be worked backwards. What
differed was who executed it. Run by hand, the loop terminates in a written
record. **Handed to an agent, the agent performed the first two steps and
silently dropped the third** — no record of what it installed, what it chose, or
why, and nothing reported back.

So the missing install documentation is not a gap in how this machine's owner
works. **It is an agent executing a documented-by-default method and omitting
the documenting.** That is a design constraint for #296 rather than a footnote:
the install path has to emit its own record, because the actor that runs it
will not.

### 4. When you upgrade, what do you run — and how do you know it worked?

> **Tony:** "I don't upgrade — I assume it's automatic."

**His model is what the system intends.** `sync-cli-version.sh` exists precisely
to make upgrades automatic and invisible. He is not wrong about the design; he
is uninformed about a silent gap between the design and what runs.

So the honest statement of this machine's failure is not *"the user didn't
upgrade."* It is: **the user held the correct mental model, the system promised
exactly that behavior, and the promise was void inside a tenant** — the one
place he does all of his work.

**"What would have made this a non-event?"** — he hasn't answered in those
words, and on the evidence the question may be misdirected. He never ran an
upgrade command, so no instruction, however well written, would have reached
him. **A non-event here means the automation working, not a better command to
type.**

### 3b. Confirmation on the startup message

> **Tony:** "Never seen that."

**Confirms the code reading.** Neither `[cp-engine] cxp CLI version drift…` nor
`[cp-engine version check]…` has ever appeared on this machine. Both paths are
unreachable: the plugin's is behind the tenant-deferral `exit 0`, the tenant
hook's is behind a pin that always passes. Predicted by the code, confirmed by
the user.

---

## What this changes for #296

Offered as findings, not as a design. Several contradict the issue as written.

1. **A per-machine doctor would have called this machine healthy for thirteen
   days.** Both halves were *installed*; the drift was between them, and the only
   surface that compares them disables itself inside a tenant. **The scanned unit
   has to include the composition, not just the inventory.**

2. **`~= 0.42` is a committed, shared defect.** Add a check that the tenant pin is
   *capable of failing* — a pin whose floor is eighty releases down is
   indistinguishable from no pin. This is an acceptance criterion #296 does not
   have, and it lives in a file, not on a machine. No per-machine scan finds it.

3. **The install was authored by an agent, not a person.** This is the
   load-bearing correction. #296 proposes "one-page install docs — the thing you
   can send a new person." **On this machine there was no new person to send it
   to.** A Claude session was handed a one-sentence goal and improvised the whole
   setup, silently. Either the install instructions must be written for an agent
   and placed where an agent will find them (in the repo, discoverable from
   `mc-2`), or the install must be a single command that an agent cannot
   improvise around. A human-facing one-pager addresses a reader who was never in
   the room. **And whatever it becomes, it has to emit its own record** — what it
   installed, at what versions, where — because an agent running a one-line goal
   will not write that down, and on this machine it didn't.

4. **Drift must surface at session start, not in command behavior.** Tony rarely
   types slash commands in the regular flow of work — only for truly base
   functions; otherwise he states intents. A stale command surface is invisible
   to him by construction. Your item 4 (warn-only line in `cxp sync`) is directionally
   right but lands in the wrong place — **he doesn't run `cxp sync` either.**
   SessionStart is the only surface he reliably sees.

5. **Write it in his vocabulary.** He has one object, "the spine." The system has
   four installable surfaces and no name he recognises. Doctor output in
   surface-speak will not be read.

6. **Acceptance criterion "a 0.40.0 project install is a live fixture" does not
   exist here.** This machine has one install and two cached versions. If that
   fixture is load-bearing for the control test, it lives on exactly one machine
   — yours — which is the situation #296 exists to end.

7. **Stale `cxp mcp` processes reproduced on machine two.** The one hypothesis
   that generalized. Invisible by construction: the tools answer normally, from
   old code. **Suggest promoting it from a `doctor` line-item to its own
   acceptance criterion** — and note that neither `--fix` nor any reinstall
   clears it. Only killing the process does. A `--fix` that reinstalls while
   stale servers keep running would report success and change nothing the user
   experiences. That is trap #1's shape again, one layer up.

8. **The profile model needs a third shape, or none.** Tony runs hosted and local
   simultaneously. `full` / `hosted-only` has no name for him, and he is half the
   known population.

---

## The recommendation this audit actually points to

Findings 3, 4 and 5 above are three symptoms of one thing, and it is worth
naming directly rather than leaving as three separate fixes.

*Attribution, since it matters for how you weigh this: the framing below was
Tony's, surfaced on reading the findings back. The analysis and the case for it
are mine. It is in the document rather than in a footnote because the evidence
gathered above independently supports it — and because it comes from the one
person in this investigation who uses the system daily without building it.*

**The install docs, the doctor output and the drift warning are all being
designed for a human reader who does not appear anywhere in this incident.** The
installer was an agent. The daily operator states intents rather than commands.
Nobody on this machine ever read an instruction or typed an upgrade. Writing a
better page for that reader improves nothing, because the reader is not there.

### What the incident already proves works

Look again at how this machine got set up. A cold Claude Code session was pointed
at the `mc-2` repo and asked, in substance, *what is this, and how do I set my
computer up to use it.* **The session then installed a four-surface system
correctly enough that it ran in production for two weeks.**

That is not a cautionary tale. **That is the target workflow, and it already
happened spontaneously, unprompted, on the first try.** It has exactly one
defect: the repo it was pointed at contained nothing telling it what the right
answer was. So it inferred one, privately, and wrote nothing down.

**The workflow is validated. The payload is missing.**

### The structural read

cp-engine is being built faster than its conventions are being set. There is no
standing rule for where a tool lives, what a tool ships with, or what a tool
owes a person who arrives at it cold. #296 names this in the version dimension —
"no supported configuration" — but the gap is wider than versions. It is the
same gap that produced four surfaces with no shared signal, a tenant pin nobody
revisited for eighty releases, and an install nobody could describe afterwards.
**Those are not four bugs. They are one missing convention, showing up four
times.**

### The convention worth adopting

Make **the repository the unit of instruction**, and make it self-describing to
an agent.

Isolate each tool as a whole repo, or a clearly bounded directory inside one,
and commit into it — weighted so a cold session reads it before anything else —
the answers to:

- **What is this?** In the vocabulary of the person who will use it, not the
  vocabulary of its internals.
- **What is the human-machine interaction model?** Who does what. Whether this
  is driven conversationally or by named commands, and if by commands, what a
  person is expected to remember — which should be close to nothing.
- **How is it installed, verified, upgraded, uninstalled, and managed?** Written
  as executable instructions for an agent, not prose for a person. Including
  what a correct installation looks like afterwards, so the agent can check its
  own work and report it.

The human interface then collapses to one durable sentence, usable against any
tool in the org, learnable once: **point a session at the repo and ask "what is
this, how do I use it, and how do I install it."**

A cold session reads the repo, acquires the context in full, and can then do the
three things this audit found nobody doing: **teach the user what the tool is,
install it correctly, and keep it managed on their behalf.**

### A note on where the interface has already moved

*Tony's read again, and the sharpest thing he said in the whole exchange — that
in an agentic system the work is building the context and structure machines
need in order to serve people who communicate by talking, which is what the
"language" in the acronym was always pointing at. The case for it below is mine.*

Worth separating two things that this system currently treats as one: the
commands are excellent, and the assumption that a person types them is
inherited rather than chosen.

Named commands are the natural unit when the object being designed is the tool.
They are precise, enumerable, easy to document, and easy to reason about — and
every command surface in cp-engine is well built. But they are also a carry-over
from a period when a machine could not be told what you wanted, only which
function to call. Every remedy proposed in #296 — the one-page doc, the
`cxp sync` warning line, `cxp doctor` itself — assumes a person who arrives
knowing a name to type. On the evidence of this machine, that person is a
smaller share of the user base every month.

**And it is worth noticing what the operator here lost by not working at the
command level: nothing.** Base functions aside, he described outcomes, sessions
selected the surface, and the work got done correctly for months. That is not a user who failed to learn the tool. That is
the interface the technology now offers, being used as designed.

The reason is sitting in the acronym. The native interface of a large *language*
model is language. So the thing worth investing in is not a better command
vocabulary for humans to memorise — it is the **context and structure a machine
needs in order to choose the right surface on a person's behalf.** Commands stay
exactly as they are; they remain the right thing for an agent to call. What
changes is who is expected to know them.

Read that way, the missing convention above is not a documentation task at all.
**It is the substrate an agentic system runs on, and it is the part of this
product that currently does not exist.** The slash commands were built. The
context that lets a cold session use them on someone's behalf was not — which is
precisely why an agent improvising an install in September had to guess, and why
a stale command surface could run for thirteen days without its only user being
able to see it.

### Why this is the durable version of #296's item 5

#296 proposes "one-page install docs — the thing you can send a new person."
Right instinct, wrong recipient, and it does not scale: it produces one document
per tool, maintained by hand, read by whoever happens to be told it exists. The
convention above produces the same artifact as a **property every repo has**,
addressed to the actor that actually performs installs, and it makes the
knowledge retrievable at the moment of use rather than at onboarding.

It also removes a load-bearing assumption nothing in this audit supports: **that
people will memorise a set of bespoke slash commands for an internal tool they
have never seen.** They will not, and the evidence here is that the most fluent
daily user of this system reaches for them only at the base-function level — and
never needed more, until the moment the system's health depended on him noticing
a command behaving oddly.

### What this would have changed

- The Sept 3 install would have been performed against committed instructions
  rather than inference, **and would have emitted a record** — which is the
  entire content of finding 3.
- "Up to date" would have had a repo-local definition to check against, instead
  of a pin last touched eighty releases ago.
- A drift warning would have had somewhere legitimate to be read from, and a
  vocabulary to be read in.

Offered as a convention proposal, not a scope item for #296 — it is larger than
that issue, and #296 is one of its instances.

---

## Everything left untouched, as asked

- Two stale `cxp mcp` processes (PIDs 4573, 13691) — still running.
- `0.108.1` plugin cache — still present.
- `~= 0.42` tenant pin — unedited.
- No clone added to `[local-repos]`.
- The one-patch gap to v0.120.3 — not closed.

Nothing in this audit wrote to the tenant except this file.
