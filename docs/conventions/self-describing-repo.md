---
Project: cp-engine
Provenance: Version 01 | 2026-09-30
Filename: self-describing-repo.md
Author: Claude, from Tony Welch's framing (cp-engine #297)
---

# Convention: the repository is the unit of instruction

**Who this is for:** an agent (or a person) about to write the instructions,
warnings, or questions that ship with a tool — and an agent arriving cold at a
repo, deciding what to read first. You need no knowledge of cp to use it.

**Status:** adopted as the standing rule for every tool repo First Person and
Canonic build. cp-engine is the first instance (see "cp-engine's instance"
below). The framing is Tony's, from his 2026-09-18 machine audit and his review
of the #296 install plan.

---

## Why this exists

In September a Claude session was pointed at `mc-2` and asked, in substance,
*"what is this, and how do I set my computer up to use it."* It installed a
four-part system correctly enough to run in production for two weeks. **That is
the target workflow, and it worked unprompted on the first try.** Its one defect:
the repo contained nothing telling it the right answer, so it guessed privately
and wrote nothing down. Nobody, including the person whose machine it was,
could later say what had been installed.

Then, designing the fix, three artifacts in a row were written for a reader who
was not there — an install page for someone who reads docs before installing
(the installer was an agent), a warning inside a command the daily user has
never run, and a review request that assumed knowledge of the internals.
Three for three is a pattern with a cause: **the default when writing is to
write from inside the system**, and an agent asked to draft the artifact
inherits the author's vocabulary rather than translating it. *Agents reproduce
the frame they are given unless something tells them not to.*

The workflow is validated. What was missing is the payload.

## The rule

Each tool — a whole repo, or a clearly bounded directory inside one — commits
answers to four questions, **placed so a cold session reads them before
anything else**:

1. **What is this?** In the vocabulary of the person who will use it, not its
   internals. Say what it gives them access to before naming its parts.
2. **What is the human–machine interaction model?** Who does what; whether it
   is driven conversationally or by named commands; what a person is expected
   to remember — which should be close to nothing.
3. **How is it installed, verified, upgraded, uninstalled, and managed?** As
   executable instructions for an agent, not prose for a person. Include what
   a correct install looks like afterwards, so the agent can check its own
   work — **and have the install emit a record of what it did**, somewhere a
   later session or tool can read it.
4. **Who is the reader, and what can they be assumed to know?** Stated
   explicitly at the top, because the one writing the payload is the actor
   most likely to aim it past the audience.

The human interface then collapses to one sentence, learnable once:

> *Point a session at the repo and ask what this is, how to use it, and how to
> install it.*

## Where the answers go

- **The first file a cold agent reads.** For Claude Code that is `CLAUDE.md` at
  the repo root — it is loaded automatically, before the agent chooses what to
  open. Keep it to the four answers (or a pointer to where each one lives) plus
  whatever the repo's own developers need.
- **The README's first screen** says the same in a few lines, for anyone
  arriving through GitHub instead of a session.
- **The install payload** may be its own file when it is long (cp-engine's is
  `docs/install.md`). The entry file names it in its first lines; nothing
  below the fold is load-bearing.
- **Repos a person is likely to be pointed at by mistake** carry a short pointer
  to the right repo. A cold session lands wherever the person happens to be,
  not where the tool lives.
- **Versioned with the thing it describes.** The payload lives in the tool's own
  repo so a release that changes the install changes the instructions in the
  same commit.

## Required pre-write check: the reader test

Before writing any instruction, warning, or question for a user, ask:

> **Can this be answered by reacting to something, or does it require knowing
> how it works?**

The first is a user question. The second is the builder's call — make it, and
confirm it later by watching what the user does. A warning that tells a person
to run a command they have never heard of fails the test; a line that names the
condition, the consequence, and one sentence to say to the session passes it.

Apply it to every artifact this convention covers: entry files, install
payloads, SessionStart output, error messages, review requests.

## Conformance checklist

A tool repo conforms when a reviewer can tick all of these from the repo alone:

- [ ] The entry file a cold agent reads first answers questions 1–4, or points
      to where each is answered, in its first screen.
- [ ] Question 1 is answered in the user's words before any component is named.
- [ ] Install, verify, upgrade, uninstall are written as commands an agent can
      run, in order, with the expected result of each check.
- [ ] A correct install is described concretely enough to verify (a command
      and its expected output, not "should work").
- [ ] The install writes a record of what was installed and who installed it.
- [ ] The reader is named, with what they can be assumed to know.
- [ ] Every user-facing warning or question passed the reader test.

## cp-engine's instance

| Requirement | Where |
|---|---|
| Entry file | [`CLAUDE.md`](../../CLAUDE.md) at the repo root; the README's first section mirrors it |
| 1 · What is this | `docs/install.md` §"What cp is" |
| 2 · Interaction model | `docs/install.md` §"How a person uses it" |
| 3 · Install / verify / upgrade / uninstall | `docs/install.md` §§3–7; verify = `cxp doctor` |
| 3 · The record | `cxp record-install --installer agent\|human` → `[install]` in the tenant's `.cp-engine.local.toml` |
| 4 · Reader | `docs/install.md`, first paragraph |
| Pointer from where people land | `mc-2`'s `CLAUDE.md` — the pointer text is in `docs/install.md` §"Pointer for other repos" |

## Not decided here

These came up and are left open on purpose, rather than set by being first:

- **What counts as "a clearly bounded directory."** A monorepo with several
  tools (e.g. `prototypes/hosted-mcp/` inside cp-engine) may need one entry
  file per tool, or one per repo with sections. No instance yet forces it.
- **Agents other than Claude Code.** `CLAUDE.md` is the Claude Code entry point.
  Whether to also ship an `AGENTS.md` or equivalent for other agents has not
  been decided.
- **Machine-checked conformance.** The checklist is reviewed by hand. A CI
  check (for example, "the entry file names an install payload that exists")
  is possible but not built.
- **Generated entry files.** Tenant `CLAUDE.md` files are generated by
  cp-engine; a repo whose entry file is generated must carry the four answers
  in its generator template, not by hand. Whether `mc-2`'s is generated has to
  be checked before its pointer is added.
