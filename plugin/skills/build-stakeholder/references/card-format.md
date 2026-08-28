# Stakeholder card format

The exact on-disk contract for a spine stakeholder card, plus a worked example.

## Frontmatter

```yaml
---
est_item_id: _authored/<slug>
est_item_kind: context
binding: unbound
layer: Stakeholders
placement: context
steps:
- position: 1
  title: Created <Name> — <role descriptor>
  status: done
  date: 'YYYY-MM-DD'
  note: null
---
```

Then the version header:

```
## v1 — YYYY-MM-DD · live
framing: <Name> — <role descriptor>
sources:
```

`sources:` is often empty on a card built from a call or a paste. Populate it
when the card draws on an ingested source with an id.

### Field notes

- **`est_item_id`** — always `_authored/<slug>`; must match the filename stem.
- **`placement: context`** — derived from the key shape (`_authored/*` = context).
  Never set from `binding`.
- **`binding: unbound`** — normal and correct for stakeholder cards. Unbound
  context is not an error state.
- **Version status** — `live` or `superseded`, nothing else. Other vocabulary
  breaks promote.

## Slug convention

`<first>-<last>-<role-descriptor>`, lowercase, hyphens only.

The descriptor is **what they are to the engagement**, not their business card.
A person whose title is "Customer Advocacy Manager" may be, to us, the one who
owns casting — the slug records the second thing:

| Person | Slug |
|---|---|
| The brand leader who owns the budget | `<first>-<last>-<client>-brand-lead-buyer` |
| The person who referred the deal and runs design | `<first>-<last>-referrer-design-counterpart` |
| The client contact who owns customer recruitment | `<first>-<last>-<client>-customer-liaison` |
| The client's security reviewer on a gated deliverable | `<first>-<last>-<client>-security-gate` |

## Body shape

Prose in bolded-lead paragraphs. Open with the provenance line.

```markdown
_Stakeholder dossier, v1 — from <source>, <date>._

**Role:** ...

**Reports to / around them:** ...

**What they want:** ...

**How they work:** ...

**Watch items:** ...
```

Sections flex to the person. Common alternates: **What they own / drive**,
**Their view on our role**, **Cadence**, **Known hard-won lesson they carry**,
**Their workarounds / instincts to note**.

## Worked example — the register to aim for

Structure only; the specifics are invented. Real cards carry real client
detail and stay in their tenant.

> **How they work:** Fast, candid, self-aware ("I'm probably just off the cuff
> doing this"), decisive. Came into the briefing without a formal brief because
> it was scheduled fast. Comfortable making calls in the room. Juggling a very
> small, fully-loaded team.

Note what that does: **a quoted line doing the characterisation**, a specific
behaviour cited as evidence, and a constraint named. No adjectives floating
free of evidence. "Decisive" alone is worthless; "decisive" next to the
decision they made is a briefing.

And the watch-item register:

> **Watch items:** Budget not yet firm — mid-forecast, expects a number next
> week. […] Don't over-index on the CMO's scepticism as blocking; the buyer has
> already decided the effectiveness argument wins.

That second sentence is the most valuable kind of line in a card: **it corrects
a misreading the reader would otherwise make.** If a card contains exactly one
insight, make it that one.

## What stays in the tenant

Cards hold candid, non-public reads on named individuals — how they handle
pressure, what they are anxious about, where a relationship could break. That
is legitimate working material and it is why the card is useful.

**It is also why a card never leaves the tenant.** Do not paste card content
into client-facing documents, decks, or email. When a client asks what we know
about their own people, answer from the sourced layer — role, ownership, open
items — not from the watch items.

## Marking inference

Where a claim is inferred rather than sourced, mark it in the prose:

- *"Reads as…"* / *"Appears to…"* for behavioural inference.
- `⚠️` for anything that would be costly if wrong.
- `<as observed in project material — LinkedIn not yet reviewed>` when the
  biographical layer is absent.

## Versioning

A material change adds a block; it does not overwrite:

```
## v2 — YYYY-MM-DD · live
framing: <Name> — <updated role descriptor>
sources:

_Updated <date> — <what changed and why>._

<full revised body>
```

Then change the v1 header to `· superseded` and append a step to the
frontmatter `steps:` list with the next `position`.
