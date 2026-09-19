---
Project: cp-engine
Provenance: Version 01 | 2026-09-18
Filename: 2026-09-18-bringing-cp-current-v0.121.0.md
Author: Claude, for Drew → Tony
---

# Bringing cp current — v0.121.0

**Who this is for:** anyone with cp installed on their own machine who is about
to see it stop working. Tony first; Marcello next. You do not need to know how
any of this is put together — the whole thing is three commands and a restart,
and your Claude session can run them for you.

**The one-sentence version:** say *"update cp-engine"* to your Claude Code
session, then restart Claude Code. If that works, you are done. The rest of
this page is what that sentence does, and what to do if it doesn't.

---

## What happened, in two lines

We found that the two halves of a cp install — the engine (`cxp`) and the
Claude Code plugin (the slash commands) — could drift apart for weeks with
every check reporting healthy. That is what walked provenance stamps backwards
on 09-17. v0.121.0 fixes the checks, and one consequence of the fix is
deliberate: **an out-of-date `cxp` now stops, loudly, instead of quietly
writing bad data.** Yours is about to be out of date.

## What you will see first

The next time you open a Claude Code session in the cp tenant, the first `cxp`
command it runs will fail with something like:

```
Installed cp-engine 0.120.2 does not satisfy the tenant's engine pin '~= 0.121'.

Fix — pick the line that matches how you installed:
  - No local cp-engine clone (the common case; installs from a
    published release tag) ...
```

That message is correct and it is the fix. **It is not something your session
did wrong.** The pin moved because a release went out; every machine below it
gets stopped the same way.

## The fix — three commands and a restart

Run these in a terminal, or let your session run them. Order matters.

```bash
# 1. The engine. Resolve the pin to a real release tag first — the pin
#    itself ("~= 0.121") is not a tag.
TAG=$(cxp resolve-engine-pin)          # prints v0.121.0
uv tool install --force --reinstall \
  --from "git+https://github.com/FirstPersonSF/cp-engine.git@$TAG" cp-engine

# 2. The plugin (slash commands).
claude plugin update cp-engine@cp-engine

# 3. Restart Claude Code.
```

Then, in a fresh session:

```bash
cxp doctor
```

It prints what is installed and anything still out of step. **If it prints
"no findings," you are done.** (`cxp doctor` did not exist before 0.121.0 — that
is why it is the last step, not the first.)

## Two things that look right and do nothing

You may reach for these. Don't.

- **`uv tool upgrade cp-engine`** — reports success and changes nothing on your
  machine, because your install is pinned to an exact release
  (`?rev=v0.120.2`) and there is nothing newer at that tag. Step 1 above is
  the command that actually moves it.
- **`claude plugin install cp-engine@cp-engine`** — refuses with "already
  installed." `update` is the verb.

## Why the restart is not optional

The session you are in has already loaded the old slash commands, and the
`cxp mcp` servers it started are still running the old engine even after
step 1 replaces the files on disk. On your machine two of those servers date
from 09-17 (`cxp doctor` will name them). A restart clears all of it; nothing
short of one does.

## What you do not need to do

- Edit `.cp-engine.toml`. The pin now moves on its own when the engine
  releases; the raise you will see in the tenant's history was made by
  `cxp sync`, not by hand.
- Set up a local `cp-engine` clone. You never needed one; the earlier error
  message implied you did, and that was the message's fault.
- Run `cxp sync` to "fix" anything. It will run normally once the engine is
  current, and the first time it does it writes a small `[install]` record to
  your local config so the next drift is visible to something.

## If step 1 fails

Almost always one of:

- **`uv` not found** — `curl -LsSf https://astral.sh/uv/install.sh | sh`, then
  open a new terminal.
- **GitHub auth** — the repo is private; `gh auth status` should show you logged
  in. If not, `gh auth login`.
- **Anything else** — paste the whole error to Drew, or say *"cp-engine won't
  update, here is the error"* to your session. Do not work around it; the
  workaround is what we are trying to stop needing.

---

*Why this page exists rather than the system just doing it: the mechanism
that should have updated you automatically has never run inside the cp tenant,
by design — it defers to a check that turned out to be blind. v0.121.0 gives
that check eyes. This is the one manual step that gets you onto the version
where the manual steps stop. Details, if you want them: cp-engine #296 and
`docs/plans/2026-09-18-install-and-doctor.md`.*
