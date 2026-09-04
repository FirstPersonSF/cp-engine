---
name: rfp-authoring
description: Write a partner RFP from a cp project — running the readiness preflight first, drafting against real scope rather than an empty scaffold, verifying anonymisation as a pass over the finished draft, and persisting the result as a versioned spine element. Use when the user says "write an RFP", "draft an RFP for <code>", "we need a production partner for <project>", or asks to send a brief to outside vendors. Never drafts from a project the preflight refuses.
---

# Write a partner RFP

An RFP is an **invitation to a peer**, not a requirements document thrown
over a wall. The partner works *with* us, not for the client — we are
agency of record, they are the production capability we are adding.
Everything below follows from that.

## The hard rule

**Run `cxp preflight <code> --kind rfp` first, and do not draft if it says
NOT READY.** This exists because a complete, well-formatted production RFP
was once written against a project with no video in it, from a `cp.md`
scaffolded the day before. It was plausible, polished, and useless. The
preflight has three gates and each catches a different way that happens:

| gate | means |
|---|---|
| unauthored scaffold | the project exists but nobody has written it up |
| `shape_warning` | wrong KIND of project — an RFP asks a partner to MAKE something |
| `funding_warning` | right shape, but the scope is not funded — going to market wastes a partner's time |

`ready: false` is a stop, not a warning. If the user wants to proceed
anyway, say plainly what the preflight found and make them decide.

**`partner_budget` is always reported missing, and you must ask for it.**
It is *not* the engagement fee. A $425k engagement might carry a $150k
partner budget; conflating them would be a serious error. Never infer one
from the other, and never quote the engagement fee to a partner.

---

## Step 1 — Preflight and read

```
cxp preflight <code> --kind rfp
```

Take `found` as your scope: deliverables, audience, schedule, usage.
**Deliverables come from CP, never from invention** — if the preflight did
not find a deliverable, it is not in the RFP.

Read `conflicts` carefully. A schedule disagreement is not a blocker to
drafting, but the RFP must take a position on it, and you should say which
position you took and why. Surfacing the conflict to the user first is
usually right — it is often a decision nobody has made yet.

## Step 2 — Elicit only what CP does not know

Ask for `partner_budget` and anything else in `missing`. Do not ask for
what the preflight already found; re-asking is how a tool teaches people
it is not paying attention.

## Step 3 — Draft

### The section set (production)

1. **Who we are** — agency-of-record framing, and the partner relationship
2. **Deliverables** — from CP
3. **Where the creative stands** — explicitly whether a brief exists yet
4. **The central question**
5. **Requirements** — geography, capability, enterprise-readiness
6. **Budget** — the partner budget, stated
7. **What to send us**
8. **Timeline**
9. **How we will decide** — ranked criteria
10. **Submission**

Sections 2, 6 and 8 are CP-populated. Sections 1, 4, 7 and 9 are yours.
Sections 3 and 5 are hybrid. That boundary is the architecture in
miniature: **CP knows what is true about the project; you know how to say
it.**

### The craft, in order of how much it matters

**Put one real question at the centre.** For the 2027 video RFP it was
*"tell us where AI belongs in this campaign and why, and where it
doesn't."* That single question filtered harder than the entire rest of
the document — it separates shops with a considered position from shops
with a capability deck. Every RFP needs one, and it should be a question
where a thoughtful partner and a careless one produce visibly different
answers.

**Name the budget.** State the band up front so respondents self-select
honestly. Withholding it wastes everyone's time and selects for the shops
willing to guess.

**Do not ask for spec concepts.** Ask for approach, team and reasoning.
Concepts come after selection. Asking for free creative is how you lose
the shops worth having, and the ones who agree are telling you what their
time is worth.

**Ask them to show the seams.** For any capability claim — especially an
AI one — ask for a breakdown of what was captured versus generated versus
finished traditionally. A shop that can answer that is a shop that
understands its own process.

**Named people, not roles.** "Who would actually be on this" — not "a
senior producer." The answer to that question is the actual deliverable of
a pitch.

**Rights and indemnification as a first-class section** when the client is
enterprise. Not a clause at the end. Enterprise legal will find it either
way; better it is answered up front than renegotiated after selection.

**Short.** Four things done well beats a forty-page deck — asked of them,
and modelled by us. If our RFP is bloated we have no standing to ask for
concision.

## Step 4 — Anonymise, if asked

Anonymisation is a **verification pass over the finished draft**, never a
substitution during generation. Substitution during generation produces a
document that looks redacted and isn't.

```
cxp redact-check <draft.md> --client "SAP Concur" --alias Concur \
  --competitor Ramp --competitor Navan \
  --roster Salesloft --roster Infoblox \
  --descriptor "a global enterprise software company"
```

The command finds what is mechanical. **Three things it exists to catch,
all real misses from one session:**

1. The client name removed from the brief but left in **our own client
   roster** in the boilerplate — a five-name credentials list identifies
   them in one step.
2. **Named competitors.** Three competitor names plus a category names the
   client without the client ever appearing.
3. **The missing NDA line.** Without *"we'll name them under NDA once we're
   in conversation"*, an anonymous brief reads as a fishing expedition and
   good shops pass.

**Then do the part the tool cannot.** After `redact-check` is clean, ask
yourself explicitly: *do category, audience and competitor set still narrow
this to a handful of companies?* Often they do. **Say so in your reply** —
name the residual risk rather than implying an anonymity you have not
achieved. A descriptor must be specific enough to be useful ("a global
enterprise software company") and vague enough to protect ("a company"
fails the first test).

## Step 5 — Persist as a versioned element

The RFP is **a versioned spine element, not a file**. This is not
bookkeeping: the delivery date on one RFP changed three times in a single
session, and each change meant editing four places in a loose document.

Create it on `cp-hosted` (writes carry your identity):

```
create_spine_element(
  project_code = "<code>",
  layer        = "Brief",
  framing      = "Partner RFP — <what it is for>",
  body         = "<the RFP>",
  important    = true,
  sources      = [ …the sprint file, any ingested client brief… ]
)
```

Every revision after that is `add_spine_version`, not a new element or an
overwritten file. Dates then live in one place and the trail is diffable.

If the user wants a shareable file too, write one — but the element is the
source of truth and the file is an export of it.

## What this skill does not do

**Do not generate the vendor shortlist.** Curating who to invite is
judgment work with real reputational stakes. Research assistance is
welcome; a generated list is not.

**Never synthesise a contact email from a pattern.** Data brokers sell
patterns like `first.last@vendor.com`. A bounced RFP reads as carelessness
to exactly the shops you most want. An address is either confirmed from
the company's own site or it is unconfirmed, and unconfirmed must be
visibly marked.

**Do not send anything.** Drafting and sending are different acts, and the
second is the user's.
