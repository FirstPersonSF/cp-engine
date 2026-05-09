---
allowed-tools: Bash(cp:*), Bash(git:*), Bash(cat:*), Bash(mktemp:*), Bash(rm:*), Bash(ls:*), Bash(test:*), Bash(whoami:*), Bash(realpath:*), Bash(find:*), Read, Write
description: Capture a session summary back to the cp working dir for the current source repo.
---

# /cp-summarize

Capture a summary of the work just done in this source repo into the
corresponding **cp** (Context Protocol) working directory. Commits and
pushes the change so the cp clone stays current.

## What you do

Run these steps in order. **Do not skip steps; do not improvise.** The
plumbing (path resolution, file naming, cp.md edits, git commits) lives
in the `cp capture-session` Python command — your job is to draft a
**good** summary and hand it off.

### 1. Detect the mode

Three real-world cases. Detect which one applies before doing anything else:

```bash
GIT_ROOT=$(git rev-parse --show-toplevel 2>/dev/null)
PWD_NOW=$(pwd)
```

If `git rev-parse` fails (not in a git tree at all): stop and tell the user
`/cp-summarize` must be run from inside a git working tree.

Otherwise:

- **Mode A — Source-code repo** (the common case): `pwd` is at or under a
  source repo's git root, AND that root is NOT inside a cp tenant. Detected
  by `test "$GIT_ROOT" = "$PWD_NOW"` OR `test ! -f "$GIT_ROOT/.cp-engine.toml"`.
  Continue to step 2.

- **Mode B — Content-only working dir** (1P engagements without a code repo):
  `pwd` is *inside* a cp tenant (so `git rev-parse --show-toplevel` returns
  the tenant root) AND `pwd != $GIT_ROOT`. Detected by
  `test -f "$GIT_ROOT/.cp-engine.toml" && test "$GIT_ROOT" != "$PWD_NOW"`.
  Skip steps 2–3; go straight to step 4 with `WORKING_DIR="$PWD_NOW"`.

- **Mode C — Inside the cp tenant root itself** (rare): `pwd` IS the
  tenant root. There's no project context. Tell the user "you're at the
  cp tenant root, not inside a project working dir — `cd` into a project
  dir first."

### 2. (Mode A only) Determine whether this repo is linked

Check for `.cp-link` at the source-repo's git root. If present, this is a
linked repo. If absent, this is potentially an unlinked repo (will write
to `<cp-tenant>/exceptions/`).

```bash
test -f "$GIT_ROOT/.cp-link"
```

### 3. (Mode A only) Resolve the cp tenant root (unlinked sub-case)

The `cp capture-session` command needs `--cp-tenant` for unlinked repos.
Walk up from `.cp-link`'s target (when present) to find an ancestor
containing `.cp-engine.toml`. If `.cp-link` is missing, look for sibling
repos that have a `.cp-link`:

```bash
# Try the "buddy" approach: check sibling dirs of the current repo for
# a .cp-link, then walk up from its target to the cp tenant root.
for d in $(dirname "$(git rev-parse --show-toplevel)")/*/; do
    if [ -f "$d/.cp-link" ]; then
        target=$(cat "$d/.cp-link")
        # Walk up to the tenant root.
        cur="$target"
        while [ "$cur" != "/" ]; do
            if [ -f "$cur/.cp-engine.toml" ]; then
                echo "$cur"
                break 2
            fi
            cur=$(dirname "$cur")
        done
    fi
done
```

If neither `.cp-link` nor a buddy is found, ask the user: "I can't find
your cp tenant on this machine. What's the absolute path to your cp
clone (e.g. `~/Documents/Python/cp`)?" — and use that.

### 4. Draft the session summary

Use this template **exactly**. Fill in real prose; don't leave bracketed
placeholders. Keep "What we did" tight — that's what gets pulled into
`cp.md`'s Last session line.

```markdown
## Session: <YYYY-MM-DD HH:MM>, <user>

### What we did
<2-5 sentences. Concrete, specific, and free of jargon. Include the
shape of the change — files touched, decisions made, what got verified.>

### Decisions
- <decision 1, with brief reasoning>
- <decision 2>

### Open threads
- <unresolved thing 1>
- <unresolved thing 2>

### Next
- <next concrete action, dated where possible>
- <next concrete action>
```

If a section has nothing to put in it, write `- (none)` rather than
deleting the heading. Future cross-project rollups will rely on the
consistent shape.

### 5. Determine the user

```bash
whoami
```

Or, if the user told you their name in conversation, use that (with
proper casing — "Drew" not "drew"). The user name lands in the filename
and the cp.md Last session line.

### 6. Write the summary to a temp file

```bash
TMP=$(mktemp -t cp-summarize.XXXXXX.md)
# Then use the Write tool to put the drafted summary into $TMP.
```

### 7. Invoke `cp capture-session`

Pick the invocation that matches the mode you detected in step 1.

**Mode A, linked source repo** (`.cp-link` exists at git root):

```bash
cp capture-session \
    --source-repo "$GIT_ROOT" \
    --summary-file "$TMP" \
    --user "Drew"
```

**Mode A, unlinked source repo** — add `--cp-tenant`:

```bash
cp capture-session \
    --source-repo "$GIT_ROOT" \
    --summary-file "$TMP" \
    --user "Drew" \
    --cp-tenant "$CP_TENANT_ROOT"
```

**Mode B, content-only working dir** — pass `--working-dir` instead of
`--source-repo` (the cp tenant root is inferred from the working dir's
ancestors, so `--cp-tenant` isn't needed):

```bash
cp capture-session \
    --working-dir "$PWD_NOW" \
    --summary-file "$TMP" \
    --user "Drew"
```

In Mode B, `cp capture-session` automatically commits **everything text-y
inside the working directory** — synthesis docs, transcripts, hand-written
notes, the new session file, the `cp.md` update. Binaries (`.docx`,
`.pptx`, `.pdf`, etc.) are excluded by the tenant `.gitignore`. **Do
not ask the user "should I commit X?"** — the engine has already decided.
The CLI prints the full list of files committed; just relay that to the
user as part of step 9.

The command writes the file, updates `cp.md`'s Last session line, commits,
and pushes. Print its full stdout output.

### 8. Clean up the temp file

```bash
rm -f "$TMP"
```

### 9. Report

Show the user a one-paragraph confirmation:

> Captured session to `<path returned by step 7>`.
> cp clone committed (`<sha>`) and pushed.

If `cp capture-session` reports that it wrote to `exceptions/`, also
say:

> Heads up: this repo isn't tracked in your cp tenant, so the summary
> went to `<cp-tenant>/exceptions/`. Consider registering the repo in
> MC-2's `/repos` page so it gets a proper working directory.

## What good looks like

- The "What we did" section reads as a clear paragraph that someone
  reviewing in a week's time will understand without context.
- Decisions are *decisions*, not observations. ("Decided to ship the fix
  without the dropdown overhaul" — yes. "The fix works" — no.)
- Open threads are real follow-ups, not generic "more testing needed."
- Next is dated where possible, owners named where possible.

## Failure modes

- **Permission denied on `cp` command.** Means `cp-engine` isn't
  installed on this machine. Tell the user to run `uv tool install
  --from <path-to-cp-engine-repo> cp-engine` (or `pip install -e .`
  from inside that repo).
- **`cp capture-session` reports a stale `.cp-link`.** It self-heals
  silently — no user action needed. Just continue.
- **`cp capture-session` errors with `CpLinkUnresolvable`.** The repo
  has no `.cp-link` and no `--cp-tenant` was provided. Re-run step 3
  with the user-provided cp tenant path.
