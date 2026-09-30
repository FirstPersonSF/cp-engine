---
Project: cp-engine
Provenance: Version 01 | 2026-09-30
Filename: install.md
Author: Claude (cp-engine #296 §4.6, following the #297 convention)
---

# Installing cp — instructions for the session doing it

**Reader.** You are most likely a Claude Code session that a person pointed at
this repo (or at `mc-2`, or at their cp folder) with a one-line goal such as
*"set my computer up to use cp"* or *"update cp-engine."* Assume the person
knows what cp is **for** — their projects, their clients, the shared memory in
Mission Control — and assume they know **nothing** about how it is put
together: not the four parts below, not the commands, not that versions can
drift. Do the work yourself, explain in their words, and ask them only things
they can answer by reacting (which folder, which account, "sign in now").

If you are a person reading this: say *"install cp from docs/install.md"* to a
Claude Code session and let it do the rest.

---

## 1. What cp is

cp is how First Person and Canonic keep one shared, current picture of every
piece of work — every client job, account, program, and internal initiative
(together, **workstreams**).

- **The spine** is the memory: the distilled decisions, briefs, stakeholders,
  sources, and commitments for each workstream, stored in Mission Control
  (MC-2).
- **The tenant** is the shared notebook: a git repo (`FirstPersonSF/cp`) with
  one folder per workstream — its status summary, sprint files, meeting notes,
  decisions.
- **The sync** keeps the notebook current from Mission Control. It runs on its
  own in CI; a person never needs to run it.

A Claude session with cp installed can read and write all of that on the
person's behalf, under the person's own identity.

## 2. How a person uses it

**Conversationally.** They open Claude Code in their cp folder and state
intents — *"where are we on Google?"*, *"update ggl-5168"*, *"wrap up"*,
*"prep sprint planning"*. The session reads the tenant's `CLAUDE.md` (which
lists the trigger phrases) and runs whatever is needed. **A person is expected
to remember nothing** beyond "open Claude Code in the cp folder."

When something is wrong with the install, the session is told at start-up (a
`[cp] …` line in its context) and should raise it in plain words and offer to
fix it. The person's whole part is saying *"update cp-engine"* and restarting.

On claude.ai or Claude mobile there is nothing to install: they add the hosted
connector once (see [`hosted-mcp-team-setup.md`](hosted-mcp-team-setup.md)).

## 3. What gets installed

A Claude Code computer runs **all four** parts. They are separate installs with
separate update commands, which is why this page exists.

| Part | What it gives the person | Installed with |
|---|---|---|
| **Engine** (`cxp` command) | The session's local tools: sync, render, capture, the local `cp-sources` server | `uv tool install` from a release tag |
| **Plugin** | The slash commands and skills (`/cp-wrapup`, `/cp-prep`, …) and the start-up check | `claude plugin` from the `cp-engine` marketplace |
| **Hosted connector** (`cp-hosted`) | Live spine reads and writes under the person's own login | Already listed in the tenant's `.mcp.json`; needs a one-time sign-in |
| **Tenant clone** | The notebook itself | `git clone` |

## 4. Install

Run these in order. Each step says what success looks like; do not continue
past a failure — report it to the person in one sentence and stop.

### 4.0 Prerequisites

```bash
command -v git && command -v gh && command -v uv && command -v claude
gh auth status          # must show a logged-in account with FirstPersonSF access
gh auth setup-git       # lets git and uv read the private repos with that login
```

- `uv` missing → `curl -LsSf https://astral.sh/uv/install.sh | sh`, then open a
  new shell.
- `gh` not logged in → ask the person to run `gh auth login` (it opens a
  browser; they must do it).
- No FirstPersonSF access → stop; the person needs Drew to add them.

### 4.1 Tenant

Ask the person where they keep project folders (suggest `~/Documents/cp` if
they have no preference). Then:

```bash
gh repo clone FirstPersonSF/cp "<chosen path>"
cd "<chosen path>"
```

Success: `.cp-engine.toml` exists in that folder. Other tenants exist (for
example Canonic's); install only the one the person names, default `cp`.

### 4.2 Engine

Install the newest release tag. The tenant's pin always accepts the newest
release, because `cxp sync` raises the pin as releases ship.

```bash
TAG=$(git ls-remote --tags --refs https://github.com/FirstPersonSF/cp-engine.git 'v*' \
      | sed 's#.*refs/tags/##' | sort -V | tail -1)
uv tool install --force --reinstall \
  --from "git+https://github.com/FirstPersonSF/cp-engine.git@$TAG" cp-engine
cxp --version                 # prints the version in $TAG
cxp resolve-engine-pin        # run inside the tenant; prints a tag ≤ $TAG
```

### 4.3 Plugin

```bash
claude plugin marketplace add FirstPersonSF/cp-engine
claude plugin install cp-engine@cp-engine
```

If `install` says it is already installed, run
`claude plugin update cp-engine@cp-engine` instead — `install` never upgrades.

### 4.4 Local config

Inside the tenant:

```bash
cxp init --non-interactive
```

Success: `.cp-engine.local.toml` exists (it is gitignored — per machine).

### 4.5 Restart, then sign in to the hosted connector

Tell the person to quit and reopen Claude Code **in the tenant folder**. A
restart is required: the running session loaded none of what was just
installed. In the new session, have them run `/mcp`, choose `cp-hosted`, and
complete the Google sign-in in the browser (mc2.1p.is → approve). This is the
one step only they can do.

### 4.6 Verify

```bash
cxp doctor
```

Success: exit code 0 and `cp install: no findings from …`. Also call the
`whoami` tool on `cp-hosted`: it must answer with the person's email. If it
answers but every read returns zero rows, they are not on the Mission Control
roster — tell them to ask Drew.

Any finding from `cxp doctor` carries its own fix command. Run it, restart if
it says to, and re-run `cxp doctor`.

### 4.7 Record what you did

```bash
cxp record-install --installer agent     # "human" if the person typed the commands
```

This writes `[install]` into `.cp-engine.local.toml`: versions, where each part
came from, the tenant path and pin, the hosted URL, who is on the machine, and
**who installed it**. `cxp sync` keeps the versions current afterwards but
never changes `installer`. Then tell the person, in two or three sentences, what
is now on their machine and that the only thing to remember is *open Claude
Code in the cp folder*.

## 5. What a correct install looks like

| Check | Expected |
|---|---|
| `cxp doctor` | exit 0, no findings; inventory lists engine, one user-scope plugin, the tenant and its pin, the hosted build |
| `cxp --version` vs plugin version in the inventory | equal |
| `whoami` on `cp-hosted` | the person's email |
| `.cp-engine.local.toml` | has an `[install]` table with `installer = "agent"` or `"human"` |
| A new Claude Code session in the tenant | starts with **no** `[cp] …` line |

## 6. Upgrade

When the session start-up shows a `[cp] …` line, or the person says *"update
cp-engine"*:

```bash
cxp doctor                    # names each out-of-date part and its exact command
```

Run the commands it prints. Typically:

```bash
TAG=$(cxp resolve-engine-pin)
uv tool install --force --reinstall \
  --from "git+https://github.com/FirstPersonSF/cp-engine.git@$TAG" cp-engine
claude plugin update cp-engine@cp-engine
```

Then restart Claude Code and run `cxp doctor` again. Two commands look right
and do nothing — do not use them:

- `uv tool upgrade cp-engine` reports success and changes nothing (the install
  is pinned to an exact tag).
- `claude plugin install cp-engine@cp-engine` refuses with "already installed."

**A project-scoped plugin install** (one project pinned to an old version on
purpose — e.g. a workstream on hold) is reported by `cxp doctor` with a command
that `cd`s into that project. Ask the person before updating it; an old pin
there may be deliberate.

The hosted connector is shared infrastructure. Never redeploy it from a
person's machine; if `cxp doctor` says hosted is behind, tell Drew.

## 7. Uninstall

```bash
claude plugin uninstall cp-engine@cp-engine
claude plugin marketplace remove cp-engine
uv tool uninstall cp-engine
```

Then, only if the person confirms: push or discard any local work in the tenant
(`git status` must be clean and pushed) before deleting the folder. The hosted
connector is removed from claude.ai under Settings → Connectors; in Claude Code
it disappears with the tenant folder.

## 8. Managing it day to day

- **Nothing is scheduled on the person's machine.** Sync runs in CI; the tenant
  fast-forwards at session start.
- **The start-up check is the signal.** Silence means healthy. A `[cp] …` line
  means act on it this session.
- **After any upgrade, restart.** Running `cxp mcp` servers keep old code until
  Claude Code restarts; `cxp doctor` names any that are stale.

## Pointer for other repos

A cold session often lands in a repo that uses cp rather than in cp-engine.
Such repos (first: `mc-2`) carry this in their `CLAUDE.md`:

```markdown
## cp (the shared project memory)

This repo is tracked in cp — First Person's shared memory of every workstream.
To install or update cp on this machine, follow
https://github.com/FirstPersonSF/cp-engine/blob/main/docs/install.md
(the instructions are written for you, the session). Do not improvise an
install from this repo.
```
