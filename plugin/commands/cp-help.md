---
allowed-tools: Bash(cxp:*)
argument-hint: "[skill]"
description: List every cp skill — what it does, the words that start it, and an example.
label: help
summary: List every cp skill, what it does and how to start it.
hint: false
triggers:
  - list the skills
  - what can cp do
example: /cp-help wrap up
---

# /cp-help

Show the person which skills cp has and how to start each one. The list is
generated from each skill's own frontmatter (plugin commands, plugin skills,
and this tenant's `.claude/skills/`), so read it from the engine — never write
it from memory.

## Run

With no argument:

```bash
cxp skills
```

With an argument (`/cp-help wrap up`, `/cp-help cp-prep`, `/cp-help canonic-layers`):

```bash
cxp skills "$ARGUMENTS"
```

Show the output **as it comes back** — it is already a compact Markdown table
(or, for one skill, a short detail block). Add nothing above it but one line
at most; do not re-describe the skills in your own words.

## If it fails

- `No such command 'skills'` → the installed `cxp` is older than this plugin.
  Say so in one line and offer: say "update cp-engine".
- `cxp: command not found` → the CLI is not installed; point to the cp-engine
  install guide (`docs/install.md` in the cp-engine repo).
- `No skill named …` → show the list it printed and ask which one they meant.

Want a page to share? `cxp skills --html cp-skills.html` writes the same guide
as a self-contained web page.
