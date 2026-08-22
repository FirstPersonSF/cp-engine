---
allowed-tools: Bash(cxp:*), Bash(git:*), Bash(test:*), Bash(pwd:*), Read
description: Show recent activity (commits + session captures) for the current cp working dir.
---

# /cp-context

Run from inside a cp working directory (e.g.
`cp/firstpersonsf/mc-2/`). Reads the linked source repo's
local clone path from `<tenant>/.cp-engine.toml` `[local-repos.<user>]`,
runs `git log --since='7 days ago'` there, lists session captures from
the working dir's `sessions/` directory in the same window, and prints
one chronological timeline.

The user typically wants an answer like "what shipped on this project
this week?" or "what's the current state of this work?" — the timeline
is the raw material; you summarize.

## What you do

### 1. Confirm cwd is a cp working dir

```bash
test -f "$(pwd)/_repo.md" || test -f "$(pwd)/cp.md"
```

If neither exists, stop and tell the user `/cp-context` must be run
from inside a cp working directory (`<scope>/<dir-slug>/`).

### 2. Run `cp project-context`

```bash
cp project-context
```

If the user has a specific lookback window in mind, pass `--days <N>`
(default is 7). If multiple users have local clones registered and the
default machine-detection picks the wrong one, pass `--user <name>` to
override.

The command prints a header (repo name, GitHub URL, local clone path)
plus one timeline line per commit and per session, newest first.

### 3. Synthesize for the user

Read the timeline output. Produce a short summary that answers their
implicit question. Default framing:

> Last 7 days on `<repo>`:
>
> - **Commits:** <2-4 sentences grouping recent work by theme>
> - **Sessions:** <1-2 sentences summarizing what humans captured>
>
> _<one-line takeaway: what's in flight right now, what just shipped>_

If the user asked something specific ("what touched the auth flow?"),
filter the timeline to relevant entries and answer that directly
instead of the default framing. The raw output is yours to interpret.

### 4. Surface gaps

If `cp project-context` reports `Local clone: (not on this machine)`,
that means the local-clone path either isn't configured for any user
in `[local-repos.<user>]` or the configured paths don't exist on this
machine. Mention it: "I don't have a local clone of this repo on this
machine — only session captures, no commit history. Ask me to add a
`[local-repos.<your-name>]` entry to `.cp-engine.toml` if you want
git context too."

If the timeline is empty, say so plainly: "Nothing's happened on this
project in the last 7 days." Don't manufacture insight.

## What good looks like

- The summary surfaces *what changed* and *who changed it*, not just
  raw commit subjects. ("Drew shipped the dropdown fix; Tony refactored
  auth" beats "fix billing dropdown / refactor auth.")
- Session captures and commits are integrated, not listed separately.
  ("After Tuesday's session captured the dropdown bug, Drew shipped
  the fix Wednesday afternoon" tells a story; two parallel lists don't.)
- One-line takeaway at the end matches the framing of the project's
  `cp.md`'s **Current work** field.

## Failure modes

- **`cp` command not found.** Tell the user to install cp-engine
  system-wide: `uv tool install --force --from <path-to-cp-engine-repo> cp-engine`.
- **`Error: No \`[local-repos.<user>].<repo>\` entry`.** They passed
  `--user` for a user who doesn't have this repo configured. Re-run
  without `--user` to let the command pick whichever path exists.
- **No `_repo.md` or `cp.md` in cwd.** They ran from outside a working
  dir. Tell them to `cd` into a project working dir
  (`<tenant>/<scope>/<dir-slug>/`) first; the step-1 check above
  catches this before invoking `cp project-context`.
