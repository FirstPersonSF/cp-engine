---
Project: cp-engine
Provenance: Version 01 | 2026-09-18
Filename: 2026-09-18-install-and-doctor.md
Author: Claude
---

# One supported install, and a doctor that can see it

**Issue:** [cp-engine #296](https://github.com/FirstPersonSF/cp-engine/issues/296)
**Status:** plan, pre-build — awaiting independent assessment

---

## 1. What we thought the problem was, and what it actually is

The presenting complaint was version drift: Tony's CLI at 0.119.0 against a
plugin at 0.108.1, twelve releases apart, discovered only because a stale CLI
re-stamped provenance **backwards across 34 files** and that looked like an
engine bug.

The obvious reading is "people forget to upgrade." **That reading is wrong, and
building against it would produce a tool that cannot catch the next instance.**

cp-engine already ships two self-healing SessionStart hooks:

| Hook | Installed by | Truth source | Behaviour |
|---|---|---|---|
| `plugin/hooks/sync-cli-version.sh` | the plugin | `plugin.json` version | reinstalls the CLI to match; refreshes the marketplace clone; never downgrades |
| `.claude/hooks/check-cp-engine-version.py` | `cxp sync` into the tenant | `.cp-engine.toml [engine].version` | reinstalls the CLI when it misses the pin |

Both work. Neither fired.

**The plugin hook defers inside a tenant — by design.** Lines 37–50 of
`sync-cli-version.sh` walk up for `.cp-engine.toml` and `exit 0` if found,
because two hooks keyed to different truths would fight over the installed CLI.
That is correct, and it means the plugin hook never runs where the work happens.

**The tenant hook then checks against a range that is 78 releases wide.**
`.cp-engine.toml` pins `[engine] version = "~= 0.42"`. Verified:

```
   0.40.0  satisfies ~=0.42 : False
   0.42.0  satisfies ~=0.42 : True
  0.108.1  satisfies ~=0.42 : True
  0.120.2  satisfies ~=0.42 : True
```

So Tony's 0.108.1 **satisfied the pin**. The tenant hook was silent and correct
by its own rules. Inside a tenant nothing enforces currency; outside a tenant
the plugin hook enforces it in a place nobody works.

> **The root cause is not forgetfulness. It is a handoff between two correct
> mechanisms, where each assumes the other covers the gap, and the gap is
> exactly where the work happens.**

This matters for the build: **a doctor that only compares versions would not
have caught this either**, because by the tenant's declared contract nothing was
wrong. What was wrong is that the contract does not express "current."

### 1.1 The second finding: nobody knows who runs what

Drew did not know Tony used the CLI at all — assumed he was hosted-only, and
so never sent CLI instructions. Tony assembled a working setup unaided.

This is not a version problem. **Someone was running a configuration nobody
designed, and nobody knew.** No tool that compares numbers addresses it; it
needs a declared, checkable configuration.

### 1.2 The third finding: multiplicity

Scanning `~/.claude/plugins/installed_plugins.json` on one machine:

```
user scope    → 0.120.2   (lastUpdated 2026-09-18)
project scope → 0.40.0    (ggl-5136-events-calendar, lastUpdated 2026-06-29)
```

Eighty releases apart, and unknown until read directly. Note 0.40.0 **fails**
the tenant pin — so a session there would trigger a reinstall, meaning the
project-scoped plugin and the tenant hook actively disagree.

`ggl-5136-events-calendar` is **on hold, not abandoned** (Drew, 2026-09-18). Its
pin is a preserved working state, not decay.

---

## 2. Design principles, each earned from a specific failure

1. **Report every instance, never one per surface.** A signal reporting one
   value for a multi-instance thing is structurally unable to see its own
   failure — the `/health` mistake (#285: counted registration, not execution)
   and the `SERVER_VERSION` mistake (v0.120.2: confident and nine months wrong).
2. **Currency is not satisfaction.** The tenant pin answers "is this allowed?"
   Doctor must answer "is this current?" These are different questions and
   conflating them is what produced the gap.
3. **Read-only before `--fix`.** A fixer whose diagnosis is wrong is worse than
   no fixer.
4. **Held ≠ stale.** A deliberately pinned project reads as *pinned*. Doctor
   must not nag a parked project, and `--fix` must not silently revive it.
5. **Never mutate shared infrastructure.** The hosted server is checked, never
   changed; one person's `--fix` must not redeploy what others are using.
6. **Surface without being sought.** The drift is invisible precisely because
   nobody suspects it, so the check must live where people already are.

---

## 3. Build

### Phase 1 — `cxp doctor`, read-only

Scan and report, exit 0 clean / 1 on drift (so it can gate CI or a hook later).

Surfaces, and where each is read:

| Surface | Source | Notes |
|---|---|---|
| CLI | `cp_engine.__version__` from the **installed** interpreter | never the repo — `git log` shows what is written, only the interpreter shows what runs |
| Plugin (all installs) | `~/.claude/plugins/installed_plugins.json` | iterate **every** entry: user scope AND each project scope |
| Marketplace (available) | `marketplaces/cp-engine/.claude-plugin/marketplace.json` | report with the clone's **fetch age** — it lags, and "available" is only as fresh as the clone |
| Tenant pin | `.cp-engine.toml [engine].version` | report the pin **and its width** — a range satisfied by 78 releases is a finding, not a pass |
| Hosted MCP | `GET /health` | `server_version` + `build`; degrade cleanly offline |

Output sketch:

```
cp-engine doctor                          profile: full

  CLI  (cxp)          0.120.2   ✓
  Plugin (user)       0.120.2   ✓
  Plugin (project)    0.40.0    ⚠  ggl-5136-events-calendar — pinned, 80 behind
  Marketplace         0.120.2   ✓  fetched 17h ago
  Tenant pin          ~= 0.42   ⚠  satisfied by 78 releases — does not enforce currency
  Hosted MCP          0.120.2   ✓  build dcc61c3a449c

  1 warning, 1 note.  Run `cxp doctor --fix` to update local surfaces.
```

### Phase 2 — the profile

`full` (CLI + plugin + tenant) or `hosted-only`. Recorded per machine in
`.cp-engine.local.toml` (already gitignored, already per-machine), asked once.

**Why this and not a single blessed setup:** the hosted wrap-up path exists
precisely so a session with no `cxp` can complete the ritual. Hosted-only is a
designed configuration, not a degraded one. Without profiles, doctor either
nags hosted-only users about a CLI they correctly lack, or stays silent for
everyone.

The profile is also the answer to §1.1: it makes "what is this person running?"
a declared, checkable fact.

### Phase 3 — `--fix`, local surfaces only

Routes around both documented traps:

- **`uv tool upgrade` reports success and changes nothing** when the receipt
  pins an exact git rev — nothing is newer at that tag. Use
  `uv tool install --force --reinstall`, and read `uv-receipt.toml` to report
  what the install actually points at.
- **`claude plugin install` refuses with "already installed"** instead of
  pointing at `claude plugin update`.

Never touches: the hosted server, a held/pinned project (without `--include-held`),
or the tenant pin.

### Phase 4 — close the hook gap

**This is the fix for the actual root cause; the rest is visibility.** Options,
for the reviewer to weigh:

- **(a) Narrow the tenant pin** to `~= 0.120` so it expresses currency. Cheap,
  but every release then needs a tenant commit, and a stale tenant clone would
  fight the CLI.
- **(b) Teach the tenant hook a currency check** distinct from satisfaction —
  warn (never auto-install) when the installed CLI is far behind the
  marketplace's available version, while keeping the pin as the hard gate.
- **(c) Let the plugin hook run inside a tenant in warn-only mode** — no
  install, so no fight with the tenant hook, but the drift becomes visible
  where the work happens.

**Recommendation: (b).** It keeps one installer (no hook fight), separates the
two questions that got conflated, and warns rather than acting — which is the
right default for something whose diagnosis is new and unproven.

### Phase 5 — one-page install doc

`docs/installing-cp.md`: what the surfaces are, which profile you want, the one
command, and how to verify. The thing you can send a new person. Today's
`docs/upgrading-to-cxp.md` documents a *rename*, not an install, and assumes the
self-healing hook works — which §1 shows it does not, inside a tenant.

---

## 4. Verification

Per the standing rule that a control test must **fail** against the unfixed
system:

- **The 0.40.0 project install is a live fixture.** Doctor must report it. A
  version of doctor that reads only user scope passes on this machine and is
  therefore wrong — that is the control.
- **A synthetic tenant pinned `~= 0.42` with CLI 0.108.1** must produce a
  currency warning while the pin check passes. This reproduces Tony's exact
  state, where every existing mechanism was silent.
- **Offline** must degrade to a clean "hosted: unreachable", not a crash or a
  false green.
- **`--fix` on a held project** must be a no-op without `--include-held`.
- Verify the installed interpreter, never the source tree.

---

## 5. Open questions for review

1. **Is Phase 4(b) the right call**, or does narrowing the pin (a) beat teaching
   the hook a second concept?
2. **Should `--fix` exist at all in v1?** Principle 3 says diagnose first. The
   counter-argument: the diagnosis is the whole cost, and a fix that requires
   copy-paste re-introduces the discovery problem.
3. **Is the profile over-engineering** for a two-person team, or the minimum
   that makes "up to date" mean anything?
4. **Does doctor belong inside `cxp sync`**, as its own command, or both?
5. **What about Marcello?** The plan assumes two profiles. If a third
   configuration exists in practice, the model is wrong before it ships.

---

## 6. Scope discipline

Not in scope: changing the release process itself, the hosted deploy path, or
the tenant pin's semantics beyond Phase 4. This plan makes the install
**legible and checkable**; it does not re-architect distribution.
