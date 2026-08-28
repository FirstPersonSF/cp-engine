---
name: build-stakeholder
description: Build a cp stakeholder dossier card for a person on an engagement or initiative — from a LinkedIn profile, pasted bio, meeting transcripts already in the tenant, or any combination. Use when the user says "create a stakeholder for X", "build a stakeholder card", "add X as a stakeholder", or supplies a LinkedIn URL for someone on a project. Writes to the project spine's Stakeholders layer via propose_spine_step, and never invents biographical fact.
---

# Build a stakeholder card

A stakeholder card is a **working dossier, not a résumé**. Its job is to make
the next conversation with this person go better — so the useful content is how
they work, what they own, what they want, and what to watch. Job history is
context for those, not the point.

## The hard rule

**Never invent biographical fact.** Title, tenure, employer, education, and past
roles are either sourced or absent. Everything inferred from behaviour —
working style, motivations, watch items — is fine to write, but it must be
visibly grounded in something in the tenant (a transcript line, an email, a
meeting note), and where the inference is a real leap, say so in the text.

A card with three sourced sentences beats a card with twelve plausible ones.
The second kind gets quoted back at you in a client meeting.

---

## Step 1 — Get the biographical layer

**LinkedIn cannot be fetched.** `WebFetch` returns HTTP 999, and so does curl;
LinkedIn blocks automated access and scraping it violates their terms. Do not
try, and do not present a workaround as if it were reliable.

Three ways to get the facts, in order of preference:

**A · Pasted profile text (default).** Ask the user to open the profile and
paste the top card plus experience — headline, current role, tenure, previous
roles, and the About section if it has one. This takes them ten seconds and is
the only fully reliable path. If a URL was supplied without text, ask for the
paste and say why in one line: *"LinkedIn blocks automated reads — paste the
profile text and I'll build from that."*

**B · Chrome, if it is already running.** If the `mcp__claude-in-chrome__` tools
are available AND the user is signed in to LinkedIn in Chrome, `navigate` to the
profile and `get_page_text`. Load the browser tools per the claude-in-chrome
skill first. Treat this as a convenience that often fails — if it errors, does
not have permission, or returns a login wall, fall back to A immediately rather
than retrying. **Never** report Chrome-derived content without saying it came
from the live page.

**C · No biographical source at all.** Legitimate — build the card from tenant
evidence alone and mark the role line `**Role:** <as observed in project
material — LinkedIn not yet reviewed>`. Say plainly in your response that the
biographical layer is missing.

**Always keep the URL** as provenance in the card footer, whichever path ran.

---

## Step 2 — Mine the tenant, which is where the value is

This is the half that makes the card worth having, and it is the half a
LinkedIn scrape could never produce. Before writing anything, search the
project for what this person has actually done.

**Get the project directory from `master-cp.md`; never construct it** —
engagement dirs are company-nested and the slug is longer than the code.

```
grep -rn -i "<first name>" <project-dir>/                 # spine, transcripts, cp.md
grep -rn -i "<first name>" sprints/*/<code>.md
```

Not every project has a `meeting-transcripts/` directory — recursing the
project dir covers it either way. Also check the tenant-wide `weekly-cp.md`
when the person spans projects.

**Filter the false positives before reading.** A common first name will collide
with unrelated people in the corpus — check that hits are the right person
before mining them for quotes.

Read what comes back and pull out:

- **What they own** — the decisions that route through them, the deliverables
  they gate, what stalls when they are unavailable.
- **How they work** — pace, directness, whether they decide in the room or take
  it away, what they do when they disagree. Quote them where a line is
  characteristic; a real sentence is worth a paragraph of description.
- **What they want** — stated goals, and the thing underneath the stated goal.
- **Open items between us and them** — asks outstanding, commitments made,
  anything overdue in either direction.
- **Watch items** — where this relationship could go wrong, constraints they
  are under, sensitivities. This is the section people actually reread.

If the tenant has a `cp-sources`/`cp-hosted` MCP connection, `semantic_search`
on the person's name catches material the greps miss.

---

## Step 3 — Write the card

Match the house format exactly — see `references/card-format.md` for the
frontmatter contract, the slug convention, and a worked example.

The body is prose in **bolded-lead paragraphs**, not a bulleted profile:

```
**Role:** <what they do and what that means for us — one paragraph>
**Reports to / around them:** <the people whose views arrive through them>
**What they want:** <goals, stated and inferred>
**How they work:** <pace, register, decision style, with a quoted line if one exists>
**Watch items:** <risks, sensitivities, open threads>
```

Add or drop sections as the evidence supports. A card for a client buyer needs
*What they want*; a card for a delivery counterpart may need *What they own* and
*Cadence* instead. **Do not pad a thin card to fill the template.**

Open with the provenance line: `_Stakeholder dossier, v1 — from <source, date>._`

### Length

400–700 words for a principal. 150–300 for a secondary contact. Longer than
that and the card stops being read, which is the only failure mode that
matters.

---

## Step 4 — Land it in the spine

Stakeholder cards are `layer: Stakeholders`, `placement: context`,
`binding: unbound`, `est_item_kind: context`.

**Propose, do not write directly.** Per the tenant's activity-record rule, use
`propose_spine_step` so the card carries a step. For a working-directory
authored card, write the file into `<project-dir>/spine/_authored/<slug>.md`
and hand-propose the step.

Slug format: `<first>-<last>-<role-descriptor>`, lowercase, hyphenated —
`morgan-wright-salesloft-customer-liaison`. The descriptor is what they are
*to us*, not their job title.

Then confirm what you wrote, in one or two lines, naming which parts are
sourced and which are inferred.

---

## Refreshing an existing card

If a card already exists, **do not overwrite it.** Read it first. If the update
is material — a role change, a shift in what they own, a watch item that
resolved or turned real — add a `## v2` block per the format reference and mark
v1 `superseded`. Version headers use `live` / `superseded` only.

If the update is minor, edit in place and say you did.
