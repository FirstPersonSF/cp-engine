# Prompt for Tony's Claude — cp-engine install audit

*Drew: paste everything below the line into a Claude Code session on Tony's machine. It's read-only — it changes nothing. Ask him to send back the markdown document it produces.*

---

We're rebuilding how cp-engine gets installed and kept current, and we need to
design it against a second real machine rather than against inference from
Drew's. **Please audit this machine and produce a markdown report.** Do not fix
anything, do not upgrade anything, do not modify a single file — a wrong reading
here is worse than no reading, and any "helpful" upgrade destroys the evidence
we're collecting.

Background, so you know why each question matters: on 2026-09-17 a cp-engine CLI
that was twelve releases behind its plugin re-stamped provenance **backwards
across 54 files** in the shared tenant. It looked like an engine bug; it was
version drift. Five separate mechanisms exist that were each supposed to prevent
this, and we've since found that some are dead code and some are unavailable on
machines without a local cp-engine clone. We need to know which of them are real
on *your* machine.

## What to collect

Run these and report the actual output. Where a file or path doesn't exist, **say
so explicitly** — an absence is a finding, often the most important one.

### A. The two version halves

```bash
cxp --version
which cxp
cat ~/.local/share/uv/tools/cp-engine/uv-receipt.toml
```

The receipt matters more than the version. We need to know whether
`requirements` says `directory = "..."` (installed from a local clone) or
`git = "..."` with a `rev`/tag (installed from a release). These two have
completely different upgrade semantics and one of them makes
`uv tool upgrade` a silent no-op.

### B. Every plugin install, not just the obvious one

```bash
cat ~/.claude/plugins/installed_plugins.json
```

Report **every** entry whose key contains `cp-engine` — there may be more than
one. For each: the `scope`, the `version`, the `projectPath` if it's
project-scoped, and `lastUpdated`. On Drew's machine this turned up a
project-scoped install eighty releases behind that nobody knew existed.

### C. Is the marketplace clone real, and is it a git repo?

```bash
ls -d ~/.claude/plugins/marketplaces/cp-engine
git -C ~/.claude/plugins/marketplaces/cp-engine log --oneline -1
git -C ~/.claude/plugins/marketplaces/cp-engine log -1 --format=%cr
```

Then, the decisive one — check whether the plugin's own self-update code could
ever run. Find the plugin cache directory (the `installPath` from section B,
user scope) and run:

```bash
git -C "<that installPath>" rev-parse --show-toplevel
```

We expect "fatal: not a git repository". If so, say so plainly. That block is
commented in the source as "the once-and-for-all downgrade fix" and we believe
it has never executed on any machine.

### D. Do you have a local cp-engine clone?

```bash
ls -d ~/Documents/Python/cp-engine 2>/dev/null || echo "no cp-engine clone at the usual path"
```

And in whichever cp tenant you work in, report whether `.cp-engine.local.toml`
exists and whether it has a `[local-repos]` entry for `cp-engine`:

```bash
grep -A 8 "local-repos" <tenant>/.cp-engine.local.toml
```

This is load-bearing. The tenant's SessionStart hook can only self-heal a stale
CLI by reinstalling **from a local clone listed in that file**. If you don't have
one, that hook has been printing "Reinstall manually" at you rather than fixing
anything — and we'd like to know whether you ever saw that message.

### E. The tenant side

For each cp tenant you have checked out (there may be more than one — `cp`,
`cp-canonic`, others), report the path and:

```bash
grep -A 3 "\[engine\]" <tenant>/.cp-engine.toml
ls <tenant>/.claude/hooks/
git -C <tenant> log --oneline -1
git -C <tenant> status --porcelain | head
```

### F. The MCP surfaces

```bash
cat <tenant>/.mcp.json
```

Report which MCP servers are configured. If you use the hosted one, also:

```bash
curl -s --max-time 20 https://cp.mc-2.1p.is/health
```

### G. The questions only you can answer

These matter as much as the commands, and we have no way to find them out
ourselves. Please answer in your own words:

1. **How did you install cp-engine originally?** What did you run, or who told
   you what to run? If you don't remember, say that — "I don't remember" is a
   real and useful answer about a process that should be memorable.
2. **What do you actually use?** The `cxp` CLI, the slash commands
   (`/cp-wrapup`, `/cp-prep`, …), the hosted connector, or some combination?
   Drew assumed you were hosted-only and therefore never sent you CLI
   instructions — we want to know what you assembled and how.
3. **Have you ever seen a version warning** at session start, in a slash
   command, or in MCP output? Specifically anything like "cxp CLI version
   drift", "EngineVersionMismatch", or "restart the MCP connection"? Did you act
   on it?
4. **When you upgrade, what do you run?** And has it ever appeared to succeed
   while changing nothing?
5. **Is there anything about installing or updating cp that you've found
   confusing, or worked around?** Workarounds are the most valuable thing here —
   they're the part we can't see from our side.

## How to report

Produce a single markdown document with a section per letter (A–G), the raw
command output in fenced blocks, and a short **"What this machine actually is"**
summary at the top: which surfaces are installed, at which versions, and which
of them you knew about before running this.

Flag anything surprising. If a command errors, paste the error — errors are
data. If something contradicts what this prompt expects, **say so loudly**; we'd
rather find out our model is wrong now than build on it.

Again: **change nothing.** If you notice something stale and want to fix it,
report it and leave it — we specifically need the drifted state preserved.
