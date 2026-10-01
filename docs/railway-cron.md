# Railway cron: health line, tenant sync, Monday drafts

GitHub Actions' scheduler ran this tenant's crons 4–8 hours late (measured
2026-09/10: the 14:00 UTC sync ran 18:02 / 20:21 / 19:05; the 09:47 daily
digest 14:49–17:48; the 12:23 health line had not fired by 13:53). Three jobs
now fire from Railway crons instead, one cron service per job:

| Job (`CRON_JOB`) | Service | Schedule (UTC) | Was | Response |
|---|---|---|---|---|
| `health` | `cp-cron-health` | `23 12,13 * * *` | tenant `health.yml` | 200, synchronous |
| `sync` | `cp-cron-sync` | `0 14,22 * * *` | tenant `sync.yml` | 202 + background |
| `draft-summaries` | `cp-cron-drafts` | `17 12,13 * * 1` (Mondays) | tenant `draft-summaries.yml` | 202 + background |

```
Railway cron service "cp-cron-<job>"     (CRON_SCHEDULE, UTC)
  └─ python trigger.py                  (webhook/cron/trigger.py, stdlib only)
       │  derives the slot that fired, signs, POSTs {"slot": "0 14 * * *"}
       ▼
cp-engine webhook  POST /api/cron/<job>   (webhook/routers/cron.py)
  health:  read-only clone → slot gate → idempotency → gather → Slack post
  sync / draft-summaries:
           slot → gate → idempotency → 202 accepted ─┐
           background: full clone → work → commit as cp-engine-bot →
           [writer lock] push rebase-or-fail → row on /api/cron/<job>#background
```

## Why one service, not two

Railway's cron gives the process no schedule context. Two options:

- **Two services**, one per slot, each with `CRON_SLOT` fixed. Lag-proof, but
  two services to keep in step.
- **One service** at `23 12,13 * * *`; the trigger takes the most recent
  scheduled time at or before now as the slot. Railway fires "within a few
  minutes", so this is exact in practice. A fire more than 45 minutes late
  (under the 60-minute spacing) cannot be attributed safely: the trigger exits
  2 and Railway shows a failed run — loud, never a wrong guess.

One service. `CRON_SLOT` stays available for a manual run.

The same holds for every job: sync's slots are 8 hours apart, drafts' an hour
apart on Mondays. The trigger reads a weekday field too (`17 12,13 * * 1`
→ slot `17 12 * * 1`, carrying the weekday it fell on; a fire on any other
day exits 2). Ranges and steps (`1-5`, `*/2`) are refused, not guessed.

## The route

`POST /api/cron/{job}` — HMAC-signed like every other route
(`X-Webhook-Signature`, `X-Webhook-Timestamp`; production sets
`WEBHOOK_REQUIRE_TIMESTAMP=true`). Body `{"slot": "<M H * * *>", "dry_run": false}`.

| Response | Meaning | `webhook_runs.status` / `detail.outcome` |
|---|---|---|
| 200 `posted` | the line went out | ok / posted (+ `posted_ts`) |
| 200 `skipped` | not the 05:xx-Pacific slot today | ok / skipped |
| 200 `duplicate` | today already posted (a retry) | ok / duplicate |
| 200 `dry_run` | rendered, not posted, day not marked | ok / — |
| 400 | bad JSON or slot | rejected |
| 401 | bad or missing signature | rejected |
| 404 | unknown job (checked after the signature) | rejected |
| 502 `failed` | the Slack post failed | failed / failed |
| 500 `failed` | clone / config / crash | failed |

The table's CHECK constraint has no `skipped`, so skipped and duplicate runs
are status `ok` and the outcome lives in `detail` — no migration.

Idempotency: one post per tenant day. Before posting, the route reads the
last 30h of `webhook_runs` for `/api/cron/health` and stops at a row whose
`detail.outcome` is `posted` for today; a process-local memo and a lock cover
the moment between a post and its row landing. If the ledger is unreadable it
still posts (silence would hide the outage it reports) and says
`idempotency unverified` in `warnings`, so the run row is `partial`.

To add a job: write `_job_<name>(payload)` in `webhook/routers/cron.py`, add
it to `JOBS` (a long one returns `_accept_background(...)`), add it to
`webhook/cron/provision.py`'s `JOBS`, and provision its own cron service with
`CRON_JOB=<name>`.

## The long jobs: `sync` and `draft-summaries`

Both take minutes, so the request does only the fast part and answers:

| Response | Meaning | rows |
|---|---|---|
| 202 `accepted` | the run started in the background | `accepted` now; `#background` row when it ends |
| 200 `skipped` | the gate said no (drafts: not Monday before 10:00 tenant time) | ok / skipped |
| 200 `duplicate` | this slot already ran today and ended ok (`commit_sha` echoed) | ok / duplicate |
| 200 `running` | a run of this job is in flight in this process | ok / running |
| 400 / 401 / 404 | bad slot / signature / job | rejected |

The background row (`route = /api/cron/<job>#background`):

| status | `detail.outcome` | when |
|---|---|---|
| ok | `pushed` (+ `commit_sha`) | committed and pushed |
| ok | `no_changes` | the render / drafts changed nothing |
| ok | `skipped` | drafts: the gate re-checked after the tenant config loaded |
| ok | `dry_run` | `{"dry_run": true}`: ran, committed nothing, slot not ended |
| partial | `pushed` / `no_changes` | sync: a managed-region edit was replaced (`region_edits` lists the quarantine files); drafts: some drafts errored |
| failed | `failed` | clone / sync / push conflict / every draft errored — `error` says which |

**Idempotency, per slot + tenant day.** A slot whose background row ended ok
(`pushed` / `no_changes` / `skipped`) is a `duplicate` for the rest of the day
— read from the last 30h of `#background` rows, plus a process memo. A
`failed` run is NOT an end: the same slot may be re-requested and runs again.
A run in flight answers `running` to any slot of the same job (one process;
a redeploy mid-run loses that run, and its slot is then retryable).

**What each job does** — the tenant workflow's steps, minus pin resolution
(the webhook runs its own deployed engine, as auto-ingest does):

- `sync` (`sync.yml`): full-history clone (partial-refresh detection blames
  each Exec Summary field; a shallow clone dates every line today) →
  `sync_tenant` → the region-edit flagging step (each new file under
  `exceptions/region-edits/` logged as a warning and listed in the row) →
  commit `[cp-sync] <date -u +%Y-%m-%dT%H:%MZ> — auto-sync from MC-2` →
  push. No webhook region guard on this commit: sync is the renderer, and it
  quarantines foreign edits itself before it splices.
- `draft-summaries` (`draft-summaries.yml`, `--planning-morning-only`):
  gate (Monday before 10:00 tenant time; checked at request time and again
  after the config loads) → `run_drafts` (the same function `cxp
  draft-summaries` calls; needs `ANTHROPIC_API_KEY`, which the webhook
  service has) → commit `[draft-summary] N Exec Summaries drafted from recent
  material (#251)` with the workflow's body (written list + NOT-written
  list) → push. Every attempt errored → `failed`, nothing committed.

**Identity.** Both commit as `cp-engine-bot
<cp-engine-bot@users.noreply.github.com>`, author and committer — the
workflows' `git config user.*`. Set through the commit's env, because the
webhook service sets `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL` and git's env beats
its config.

**Writer lock.** The clone and the work run WITHOUT the per-tenant writer
lock; the lock is taken around commit + push only. Most routes take that lock
synchronously on the event-loop thread, so holding it through a
minutes-long sync would stall every delivery behind it. A commit that lands
while the job works is rebased onto; a conflict aborts, pushes nothing and
fails the run (`_push_with_retry`) — the same contract as the workflows'
`push-with-rebase.sh`. The next slot re-renders from the new state.

## GitHub reads from Railway

Probed 2026-10-01 from the webhook service's env (HTTP status only):

| Token (webhook env) | cp-engine `tests.yml` runs | tenant `sync.yml` runs |
|---|---|---|
| `GH_PAT` | 200 | 404 |
| `gh_token` (build) | 200 | 404 |

- **CI line** works from Railway as-is: `github_get` falls back to `GH_PAT`.
- **Sync line** cannot read the tenant's Actions. It falls back to the newest
  `[cp-sync]` commit in the clone and says so: `✅ Sync · 05:08 · via git`
  (the Actions 404 goes to the logs and the run row's `check_errors`, never
  the Slack line — labels are 1–3 words). That is a weaker claim
  than a run conclusion (sync commits only when the render changed, and a
  failed run leaves no commit), which is why it names its source. Older than
  26h → ⚠️; no sync commit in the 3-day clone → ⚠️ `unreadable`.
- Once the sync runs here, the Sync line reads the cron sync's own run row
  first (`/api/cron/sync#background`): `✅ Sync · 14:03 ok · via cron`,
  `⚠️ Sync · 14:03 failed · via cron`. Actions and the git commit are the
  fallbacks for before the move (and manual `sync.yml` runs).
- To restore the strong reading: give the webhook's `GH_PAT` (fine-grained)
  repository access to **FirstPersonSF/cp** with **Actions: Read-only** (it
  already reads cp-engine). No new env var; nothing else changes.

## Railway setup (project "Fathom Meeting sync")

Use `webhook/cron/provision.py` — one service per job. It prints its plan
and changes nothing without `--apply`:

```
python webhook/cron/provision.py sync                      # plan
python webhook/cron/provision.py sync --apply --verify     # provision; first run is a dry run
python webhook/cron/provision.py draft-summaries --apply --verify
```

What it does, and why each step is done this way (learned creating
`cp-cron-health`, 2026-10-01):

1. **Create the service empty** (GraphQL `serviceCreate`, name
   `cp-cron-sync` / `cp-cron-drafts`; reused if it exists). `railway add -r
   FirstPersonSF/cp-engine` 401s — Railway's GitHub app has no grant on the
   repo — so there is no repo source and no watch path.
2. **Variables** (`variableCollectionUpsert`, `skipDeploys`):

   | Name | Value |
   |---|---|
   | `WEBHOOK_HMAC_SECRET` | `${{cp-engine.WEBHOOK_HMAC_SECRET}}` (reference — never pasted) |
   | `CP_WEBHOOK_URL` | `https://${{cp-engine.RAILWAY_PUBLIC_DOMAIN}}` |
   | `CRON_JOB` | `sync` / `draft-summaries` |
   | `CRON_SCHEDULE` | `0 14,22 * * *` / `17 12,13 * * 1` — must equal the cron setting |
   | `CRON_DRY_RUN`, `CRON_SLOT` | `--verify` only: `1`, the first slot |

   Nothing else: no Supabase, Slack, GitHub or Anthropic key in a trigger
   service. All of that stays in `cp-engine`.
3. **Upload** a minimal directory with `railway up <dir> --path-as-root
   --service <id> --ci`: `Dockerfile` (a copy of `webhook/cron/Dockerfile`)
   at the root and `webhook/cron/trigger.py` at its repo path. A code change
   to the trigger means re-running the script (no repo source to redeploy
   from).
4. **Schedule + restart policy** with GraphQL `serviceInstanceUpdate`
   (`cronSchedule`, `restartPolicyType: NEVER`). A `railway.json`
   `cronSchedule` in the upload is IGNORED.
5. **Read back** `cronSchedule`, `restartPolicyType`, `nextCronRunAt`; the
   script exits 1 if they are not what it set.

The first deploy runs the trigger once. With `--verify` that run is a dry run
of the first slot: expect `HTTP 202 · accepted` in `railway logs --service
<name>`, then a `#background` row with `outcome: dry_run` (sync rendered,
drafts drafted, nothing committed). A Tuesday-to-Sunday verify of
`draft-summaries` still runs (dry runs bypass the gate). Then delete
`CRON_DRY_RUN` and `CRON_SLOT` (`railway variable delete <NAME> --service
<name> --skip-deploys`). Without `--verify`, a first deploy away from a slot
exits 2 (one red run, harmless); inside a slot's 45-minute window it is that
slot's real, single run.

**Retire the GitHub schedules the same day** — the route's idempotency covers
its own ledger, not an Actions run, so both would sync / draft:

- tenant `sync.yml` and `draft-summaries.yml`: remove `schedule:` (keep
  `workflow_dispatch`, so either can still be run by hand — the
  `concurrency` group then only orders manual runs);
- tenant `health.yml`: already manual-only (2026-10-01).

The cp-engine webhook must be deployed with the routes first (they ship with
this change on `main`).

Exit codes, as Railway sees them: 0 on any 2xx (posted / skipped /
duplicate / dry_run / running / 202 accepted); 1 on non-2xx or a network
error; 2 on missing config or an unattributable fire. A 202 is "started", not
"succeeded": the background row (and the health line's Webhook 24h count of
failed / partial rows) is where a failed sync or draft run shows.
