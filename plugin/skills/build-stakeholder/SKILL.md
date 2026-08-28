---
name: build-stakeholder
description: Build a cp stakeholder dossier card for a person on an engagement or initiative — reading their LinkedIn through the user's signed-in Chrome (the reliable path; WebFetch and curl are blocked with HTTP 999), plus a mine of the tenant's own transcripts and spine. Use when the user says "create a stakeholder for X", "build a stakeholder card", "add X as a stakeholder", or supplies a LinkedIn URL for someone on a project. Writes to the project spine's Stakeholders layer via propose_spine_step, and never invents biographical fact.
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

**Chrome is the answer. Use it first.** A LinkedIn profile is fully readable
through the user's own signed-in browser, and that is the reliable path — not a
fallback.

### A · Chrome (default)

Load the browser tools per the `claude-in-chrome` skill, then **three steps**:

```
tabs_context_mcp   {createIfEmpty: true}
navigate           https://www.linkedin.com/in/<slug>/          → get_page_text
navigate           https://www.linkedin.com/in/<slug>/details/experience/  → get_page_text
```

**The second navigate is the one that matters, and it is easy to miss.** The
main profile page returns the top card only — name, headline, location, current
employer, school. **The experience section is not in that page's DOM at all**,
so scrolling and scraping `<section>` elements returns nothing and looks like a
failure. The `/details/experience/` sub-page returns the entire history, fully
expanded, in one call. There are sibling pages (`/details/education/`,
`/details/skills/`) but they are often empty when the top card already carries
the summary — check the top card before spending a call.

Close the tab when done (`tabs_close_mcp`).

Requires: Chrome running with the extension connected, the user signed in to
LinkedIn, and site permission granted for linkedin.com. If any of those is
missing you will get an empty result or a permission error — fall through to B
rather than retrying.

### B · Pasted profile text

When Chrome is unavailable — no extension, not signed in, permission refused.
Ask for the top card plus experience: headline, current role, tenure, previous
roles, and the About section. One line is enough: *"Chrome isn't connected —
paste the profile text and I'll build from that."*

**Do not ask for a paste before trying Chrome.** And when the user has already
supplied a URL, do not imply they gave you nothing — say what failed.

### C · No biographical source at all

Legitimate. Build from tenant evidence alone and mark the role line
`**Role:** <as observed in project material — LinkedIn not yet reviewed>`.
Say plainly in your response that the layer is missing.

### What does NOT work — settled, do not retry

- **`WebFetch` → HTTP 999.** LinkedIn's bot block.
- **`curl` plain → 999.** With a browser `User-Agent` → **301**, which looks
  promising and is not: the redirect lands on an **authwall** and returns 999
  again. Public profile scraping is closed; this is not a header problem.
- Scraping LinkedIn outside the user's own authenticated session violates their
  terms. Chrome works because it is the user's own logged-in browsing.

**Always keep the URL** as provenance in the card, whichever path ran, and say
when content came from a live read.

### Read the title, then check it against the room

The LinkedIn title is the sourced fact and belongs in the card. But people
introduce themselves by **the function they perform for you**, which is often
not their title — someone whose title is "Customer Marketing Manager" may say
"I'm the Customer Advocacy Manager here." Record both, use the real title in
writing, and note the difference rather than silently picking one.

**The history is where the reframe hides.** Prior roles routinely change how to
read what someone said in a meeting — a caution that scans as a client hedging
reads differently from someone who has done that exact job fifty times. Look for
that specifically; it is the highest-value thing on the page.

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
