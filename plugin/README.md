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

## Prerequisites

Before the slash commands work, run a one-time setup on each machine:

1. Clone the cp tenant repo (e.g. `cp-1p`).
2. Add a `[local-repos]` section to `.cp-engine.local.toml` mapping each
   source repo's name to its local clone path.
3. Run `cp link-local` from inside the cp tenant clone. This writes
   `.cp-link` files into each source repo and adds them to
   `.git/info/exclude`.

Example `[local-repos]`:

```toml
[local-repos]
"mc-2"      = "/Users/you/Documents/Python/mc-2"
"cp-engine" = "/Users/you/Documents/Python/context-protocol"
"storyos"   = "/Users/you/Documents/Python/storyos"
```

## Commands

### /cp-summarize

Run from inside any linked source repo. Writes a session summary to the
corresponding cp working dir, updates `cp.md`'s "Last session:" line,
and commits + pushes the change.

If the current repo isn't tracked in the cp tenant, the summary lands
in `<cp-tenant>/exceptions/` and surfaces in the engine-managed
exceptions README on next sync.

### /cp-context

(Optional, may ship as a stub.) Wraps the convention of "open the
linked source repo and answer activity questions" with a friendly UX.
