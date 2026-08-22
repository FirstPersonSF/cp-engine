# cp-engine plugin

Claude Code slash commands for the Context Protocol Engine.

## Install

The plugin lives in a subdirectory of the cp-engine repo. Claude Code
discovers it via the marketplace manifest at the repo root.

```
/plugin marketplace add FirstPersonSF/cp-engine
/plugin install cp-engine@cp-engine
```

Updates pull from the same repo:

```
/plugin marketplace update cp-engine
/plugin update cp-engine@cp-engine
```

Since v0.6, the plugin's `SessionStart` hook auto-installs the matching
`cp` CLI via `uv tool install` whenever the plugin and CLI versions
drift. So a fresh install pulls both halves; subsequent `/plugin update`
runs propagate to the CLI automatically. The hook is fast on the happy
path (~50ms version check) and never blocks session start on failure —
if the install fails, the next `/cp-summarize` raises a loud
`EngineVersionMismatch` with the manual recovery command.

## Prerequisites

Before the slash commands work, run a one-time setup on each machine:

1. Clone the cp tenant repo (e.g. `cp`).
2. Add a `[local-repos]` section to `.cp-engine.local.toml` mapping each
   source repo's name to its local clone path.
3. Run `cxp link-local` from inside the cp tenant clone. This writes
   `.cp-link` files into each source repo and adds them to
   `.git/info/exclude`.

Example `[local-repos]`:

```toml
[local-repos]
"mc-2"      = "/Users/you/Documents/Python/mc-2"
"cp-engine" = "/Users/you/Documents/Python/cp-engine"
"storyos"   = "/Users/you/Documents/Python/storyos"
```

## Commands

### /cp-summarize

Run from inside any linked source repo. Drafts a session summary, writes
it to the corresponding cp working dir's `sessions/` directory, updates
that project's `cp.md` "Last session:" line, then commits and pushes
the cp clone.

If the current repo isn't tracked in the cp tenant, the summary lands
in `<cp-tenant>/exceptions/` and surfaces in the engine-managed
exceptions README on next sync.

Since v0.5.1, capture-session auto-rebases on push rejection (e.g. when
a `[cp-sync]` cron commit lands between captures) and retries once.

### /cp-context

Run from inside a cp working dir. Prints a 7-day timeline merging git
commits from the linked source repo's local clone with session captures
from the working dir's `sessions/` directory. Useful for "what's been
happening on this project?" without manually grepping logs.

`--days N` overrides the window; `--user <name>` picks a specific
`[local-repos.<user>]` entry when more than one teammate has the repo
mapped.
