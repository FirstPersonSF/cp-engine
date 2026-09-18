# Review request for Tony — #296 plan v02

*Drew: send this, or paste it into a Claude Code session on Tony's machine. It
asks for five specific things, not general feedback. The plan and his audit are
both committed on `main`.*

---

Your audit refuted the plan. We rewrote it. **This is the rewrite, and we need
you to try to break it the same way.**

Read:
- **The plan** — `docs/plans/2026-09-18-install-and-doctor.md` (v02)
- **Your report**, committed alongside as the evidence base —
  `docs/plans/2026-09-18-install-audit-report-tony.md`

## What you changed

Four things, so you can see whether we heard them correctly:

1. **The causal claim was wrong and you corrected it.** We said the tenant hook
   had been printing *"Reinstall manually"* at you for months. It returned
   green — `if ok: return 0 # healthy` fires before `_read_repo_path` is ever
   called. Verified in source. The self-heal reported healthy while twelve
   releases of drift accumulated underneath it, which is a worse finding than
   the one we had.

2. **The warning was in the wrong place.** v01 put it in `cxp sync`. You don't
   run `cxp sync`. It moved to SessionStart.

3. **Profiles are dropped.** You run hosted and local at once; `full` /
   `hosted-only` had no name for you, and you're half the known population.

4. **`cxp doctor` dropped from the headline to Phase 5**, because it would not
   have caught this on either machine — both halves were installed, the drift
   was between them, and the worst defect (`~= 0.42`, ours, unmoved for 80
   releases) lives in a committed file no per-machine scan reaches.

## The five things we're asking

**1. Would a SessionStart warning actually reach you?**
This is the one that decides Phase 1, and it rests entirely on a claim about
your behaviour that we inferred from four sentences. If that output blurs past
the way most startup noise does, Phase 1 lands in the wrong place too — and
we'd be two for two on that mistake. An honest *"I'd scroll past it"* is more
useful than a yes.

**2. Rewrite the warning line.** This is the one we most want back from you.
Our draft, in Phase 1:

```
[cp] Your cp install is out of step with itself — the slash commands are
     v0.108.1, the engine they call is v0.119.0. Anything that renders or
     syncs may write stale results. Ask this session to update cp-engine,
     or run:  claude plugin update cp-engine@cp-engine
```

It deliberately says **install**, not spine — the spine is fine, the toolchain
reaching it isn't, and an earlier draft said "the spine's two halves disagree,"
which would have sent you to look in the wrong place. But it's still our guess
at what you'd act on at 8am. **Write the version you'd actually stop for.**

**3. Phase 4 — cp-engine or `mc-2`?**
You pointed a cold session at `mc-2` and it improvised a four-surface install
from one sentence. That makes you the only evidence about where an agent
actually looks. The install payload has to live where it gets found.

**4. Warning-fatigue budget.**
Phases 1 and 3 both add SessionStart output, on top of tenant-freshness. You're
the one who'd experience the accumulation. How much before it becomes noise?

**5. Anything in v02 that reads wrong from your side** — especially §2.1, where
we characterise how you work from four sentences of yours. If we've flattened
something, say so plainly; that's exactly the failure this round is correcting.

## Two notes back to you

**One correction to your report.** It cites a pre-existing `cxp doctor` proposal
at `improvements.md:2871`, described as written well before #296. We checked:
line 2915 mentions `cxp doctor` in passing about a different check, and the only
real proposal is the 09-18 entry itself. The "standing practice, not a flash of
diligence" argument still holds on the rest of the evidence — 3,300 lines of
dated entries make it — but that specific citation doesn't.

**Your convention argument is filed separately, deliberately.** The diagnosis —
that the install docs, doctor output and drift warning are all designed for a
human reader who doesn't appear in the incident — is right, and it's bigger than
#296. §6 of the plan says so explicitly. We're not folding it in because #296
has a shippable core (close the hook gap, make the pin capable of failing,
surface at SessionStart) that shouldn't wait on an org-wide convention. If you
think that's the wrong call — that the core is not worth shipping without the
convention — that's worth arguing now rather than after it's built.

## Left alone on your machine, as you left it

Your two stale `cxp mcp` processes (PIDs 4573, 13691) are the live fixture for
Phase 3's control test. **Please leave them running** until that test exists, or
tell us if you need the machine clean and we'll capture what we need first.
