# The `cp` command is now `cxp`

**TL;DR — type `cxp` where you used to type `cp`. Everything else is automatic.**

```bash
cxp sync          # was: cp sync
cxp --version     # was: cp --version
```

The slash commands are **unchanged**: `/cp-summarize`, `/cp-wrap`, `/cp-prep`,
`/cp-context`, `/cp-ingest`, `/cp-tools` all still work exactly as before.

---

## Why

The CLI installed itself as `cp`, which shadowed the Unix `cp` (copy) command.
`~/.local/bin` comes before `/bin` on a standard Mac PATH, so **every bare `cp`
in a terminal ran the Context Protocol Engine instead of copying a file**:

```
$ cp notes.txt backup.txt
Usage: cp [OPTIONS] COMMAND [ARGS]...
Error: No such command 'notes.txt'.
```

Any script — or AI agent — that reasonably assumed `cp` copies files hit this.
Renaming the binary is the fix. Released as **cp-engine v0.102.0**.

---

## What you have to do

**Nothing, in the normal case.** Open a Claude Code session as usual. The
plugin's SessionStart hook notices the version drift and upgrades you:

```
[cp-engine] cxp CLI version drift: plugin=0.102.0 installed=0.101.7. Updating...
[cp-engine] cxp CLI updated to v0.102.0.
```

That's it. From then on `cp` copies files again and `cxp` runs the engine.

The only real change is muscle memory: **`cp <subcommand>` → `cxp <subcommand>`.**

---

## Checking it worked

```bash
cxp --version       # → cxp, version 0.102.0
which -a cp         # → /bin/cp   (and nothing else)
cp a.txt b.txt      # → actually copies a file
```

If `which -a cp` still lists something in `~/.local/bin`, see *Stale `cp` shim*
below.

---

## If something looks wrong

### `cxp: command not found`

The auto-upgrade didn't run or failed (offline, or `uv` missing). Install by
hand:

```bash
uv tool install --force \
  --from 'git+https://github.com/FirstPersonSF/cp-engine.git@v0.102.0' \
  cp-engine
```

If you don't have `uv`: https://docs.astral.sh/uv/

### `cp: illegal option -- -` when you run `cp --version`

That is the **correct** new behaviour — you're seeing the real Unix `cp`
complain. Use `cxp --version`.

### The `cp-sources` MCP tools disappeared

Your tenant's `.mcp.json` still points at the old binary name. Fix:

```bash
cd ~/Documents/Python/cp    # your cp tenant checkout
cxp sync
```

That rewrites the config and refreshes the tenant hooks in one pass. Restart
the session (or `/mcp`) and the tools come back.

Newer versions of the tenant hook repair this on session start by themselves,
but the repair ships *inside* that hook — so if your hook predates the rename,
it can't fix itself. `cxp sync` breaks the cycle, because the CLI works
regardless of what `.mcp.json` says.

### Stale `cp` shim left behind

If `which -a cp` shows `~/.local/bin/cp` after upgrading, remove it:

```bash
ls -la ~/.local/bin/cp      # confirm it points into .../uv/tools/cp-engine/
rm ~/.local/bin/cp
```

Only delete it if it's a symlink into a `uv/tools/cp-engine/` directory. Never
touch `/bin/cp`.

### A scheduled job or script fails

Anything that shells out to `cp <subcommand>` needs updating — GitHub Actions
workflows, cron jobs, personal scripts. This already bit the cp tenant's four
scheduled workflows (sync, slack-digest, daily-digest, dates-loop) and the
webhook's Docker build. Grep your own repos:

```bash
grep -rn -E '(^|[[:space:]"'"'"'`;&|(=])cp (sync|ingest|wrap|brief|mcp|spine|prep-planning|dates-loop|attention-digest|capture-session|slack-digest)' .
```

For the full command list to grep against, run `cxp --help` — there are 59
subcommands, and a short hand-written list is how several of these were missed
the first time.

---

## What did NOT change

| | |
|---|---|
| Slash commands | `/cp-summarize`, `/cp-wrap`, `/cp-prep`, `/cp-context`, `/cp-ingest`, `/cp-tools`, `/cp-wrapup` |
| MCP server name | `cp-sources` (and `cp-hosted`) |
| Plugin + repo | `cp-engine` |
| Python package | `cp_engine` |
| The tenant, its layout, your files | all identical |

Only the binary you type was renamed. No `cp` alias was kept on purpose — the
alias *is* the bug being fixed.

---

## For whoever cuts the next release

Use `scripts/release.py <version>`. It bumps all the version mirrors
atomically and requires a drafted `CHANGELOG.md` section.

Hand-bumping is how v0.102.0 shipped broken the first time: three of the five
mirrors were updated, and the missed `webhook/pyproject.toml` pin failed the
Railway build with `Could not find a version that satisfies
cp-engine==0.101.7 ... from versions: none`. cp-engine isn't on PyPI, so a
mismatched pin has nothing to fall back to. The quieter miss was
`src/cp_engine/__init__.py`'s `__version__`, which is what the tenant pin check
and MCP staleness warning compare against.

Tag **after** pushing the commit and **before** anyone pulls — the SessionStart
hook installs from `git+…@v<version>`, so an untagged release fails every
auto-install on the team.

---

*Released 2026-08-22 · cp-engine v0.102.0 · questions → Drew*
