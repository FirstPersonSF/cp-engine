# Prompt for Tony's Claude — cp-engine install audit (v02)

*Drew: paste everything below the line into a Claude Code session on Tony's
machine. It's read-only — it changes nothing. Ask him to send back the markdown
document it produces.*

> **Revision note (v02).** v01 asked Tony to tell us things we have since
> established from his own commits and his `improvements.md` entry — his
> versions, his receipt shape, both upgrade traps, and the fact that he runs
> the CLI locally. Asking again would waste his time and signal we hadn't read
> what he wrote. This version states what we believe, asks him to **correct**
> it, and spends its questions on the four things nothing in git can answer.

---

We are rebuilding how cp-engine is installed and kept current. **You are the
second machine** — every design decision so far rests on Drew's laptop plus
inference, and inference is what got us here.

**This audit is read-only. Change nothing, upgrade nothing, fix nothing.** If
you spot something stale, *report it and leave it*: the drifted state is the
evidence. A helpful upgrade destroys the thing we are trying to measure.

## What we already believe about this machine

We reconstructed the following from Tony's `improvements.md` entry (cp
`6f885776`) and his commits. **Your first job is to check each line and tell us
which are wrong** — a correction here is worth more than any command output.

1. Both halves are installed locally: the `cxp` CLI **and** the Claude Code
   plugin. On 2026-09-17 they read **CLI 0.119.0 / plugin 0.108.1**.
2. The uv receipt pins an **exact git rev** (`?rev=v0.119.0`), which is why
   `uv tool upgrade cp-engine` reported success and changed nothing.
3. `claude plugin install cp-engine@cp-engine` refused with "already installed"
   instead of pointing at `claude plugin update`.
4. A `cxp render` with the stale CLI would have walked provenance stamps
   **backwards across 34 files**; commit `29947a0d` shows the roll-up committed
   alone to avoid it. **You noticed and worked around it yourself.**
5. `cxp capture-session` runs here (commit `8eac4044`), so the CLI is in real
   use, not just installed.

If any of that is wrong, say so plainly and in detail. We have built two
reviews on top of it.

## The four things we cannot see

These are the actual questions. Commands first, then the ones only Tony can
answer.

### A. Is there a local `cp-engine` clone, and does the tenant hook work here?

```bash
ls -d ~/Documents/Python/cp-engine 2>/dev/null || echo "NO local cp-engine clone at the usual path"
grep -A 8 "local-repos" <tenant>/.cp-engine.local.toml 2>/dev/null || echo "NO .cp-engine.local.toml or no [local-repos]"
```

**Why this decides a design question.** The tenant's SessionStart hook
(`.claude/hooks/check-cp-engine-version.py`) can only repair a stale CLI by
reinstalling **from a local clone named in that file**. We believe you don't
have one — which would mean that hook has been printing *"Reinstall manually"*
rather than fixing anything, for months. If so, one of the two mechanisms we
thought protected you has never worked on your machine.

**Related question for Tony:** have you ever seen a message at session start
like `[cp-engine] cxp CLI version drift…` or `[cp-engine version check]…`? Did
it ever actually upgrade anything, or just tell you to?

### B. How many plugin copies are on disk, and is one of them dangerous?

```bash
cat ~/.claude/plugins/installed_plugins.json
ls ~/.claude/plugins/cache/cp-engine/cp-engine/
find ~/.claude/plugins/cache/cp-engine/cp-engine -name hooks -type d | wc -l
```

Report **every** `cp-engine` entry — `scope`, `version`, `projectPath` if
project-scoped, `lastUpdated`.

**Why.** Drew's machine has 2 registered installs but **29 cached versions,
each with its own hooks directory**. One of them (0.40.0) carries a hook
predating every safety guard: it probes `cp --version`, which now resolves to
`/bin/cp`, reads the failure as drift, and reinstalls the CLI **down to 0.40.0
unconditionally, every session in that directory.** If a pre-0.102 version is
cached here too, this machine has the same bomb. Report the oldest cached
version you find.

### C. Are any `cxp mcp` subprocesses older than the installed CLI?

```bash
ps -eo pid,lstart,command | grep "cxp mcp" | grep -v grep
stat -f "%Sm %N" ~/.local/share/uv/tools/cp-engine/uv-receipt.toml
```

**Why.** `cxp mcp` keeps serving the bytecode it started with. A process whose
start time predates the install mtime is running stale code right now. Drew has
**two** such processes as of today. This is silent — the tools answer normally,
from old code.

### D. The tenant and MCP surfaces

```bash
# for EACH cp tenant you have checked out — there may be more than one
grep -A 3 "\[engine\]" <tenant>/.cp-engine.toml
ls <tenant>/.claude/hooks/
git -C <tenant> log --oneline -1
cat <tenant>/.mcp.json
```

## Questions only you can answer

Please answer in your own words — these matter more than the command output.

1. **How did you install cp-engine originally?** What did you run, or who told
   you what to run? *Drew did not know you were using the CLI at all* — he
   assumed you were hosted-only and so never sent CLI instructions. So you
   assembled a working setup unaided, and how you did that is the single most
   useful thing in this document. "I don't remember" is a real answer, and
   itself a finding about a process that should be memorable.

2. **What do you reach for, and what do you avoid?** Which slash commands do
   you actually use? Do you use the hosted connector (`cp.mc-2.1p.is`) at all,
   or only the local CLI + plugin? Is there anything you stopped using because
   it was unreliable?

3. **What have you worked around?** We found one already — holding the
   provenance stamp in `29947a0d` rather than letting the render walk 34 files
   backwards. **That was exactly the right call**, and it is also the kind of
   thing we cannot see unless you tell us. What else have you routed around,
   even if it felt too small to mention?

4. **When you upgrade, what do you run — and how do you know it worked?** We
   are trying to write one instruction that is correct for everyone. What would
   have made this a non-event for you?

## How to report

One markdown document. Lead with a short **"What this machine actually is"** —
which surfaces are installed, at which versions, and which of them you knew
about before running this. Then a section per A–D with raw output in fenced
blocks, then the four answers.

**Corrections to "What we already believe" go at the very top, before
anything else.** If our model of your machine is wrong, that is the headline
and everything downstream needs rethinking.

If a command errors, paste the error — errors are data. If something
contradicts what this prompt expects, say so loudly.

And again: **change nothing.** We need the drift preserved.
