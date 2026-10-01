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
if the install fails, the next `cxp` call raises a loud
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

