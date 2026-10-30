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


`/cp-help` lists every cp skill — plugin commands, plugin skills and the
tenant's own `.claude/skills/` — with what each does, the words that start it,
and an example (`/cp-help <skill>` for one in detail). It runs `cxp skills`,
which reads each skill's frontmatter (`label`, `summary`, `triggers`,
`example`, `hint`), so the list cannot drift from the skills themselves. The
same source feeds the one-line hint at session start (`cxp skills --hint`),
the shareable guide page (`cxp skills --html cp-skills.html`) and the skill
rows of the tenant `CLAUDE.md` trigger table.

After editing a plugin skill's frontmatter, regenerate the snapshot an
installed `cxp` reads: `python -m cp_engine.skill_guide --write-snapshot`
(`tests/test_skill_guide.py` fails until you do).
