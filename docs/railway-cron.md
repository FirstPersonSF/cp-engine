# Railway cron: the daily health line

GitHub Actions' scheduler ran this tenant's crons 4–8 hours late (measured
2026-09/10: the 14:00 UTC sync ran 18:02 / 20:21 / 19:05; the 09:47 daily
digest 14:49–17:48; the 12:23 health line had not fired by 13:53). The health
line now fires from a Railway cron instead.

```
Railway cron service "cp-cron-health"   (23 12,13 * * * UTC)
  └─ python trigger.py                  (webhook/cron/trigger.py, stdlib only)
       │  derives the slot that fired, signs, POSTs {"slot": "23 12 * * *"}
       ▼
cp-engine webhook  POST /api/cron/health   (webhook/routers/cron.py)
       read-only tenant clone → slot gate → idempotency → gather → Slack post
       one webhook_runs row: detail = {job, slot, day, outcome, posted_ts}
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

To add a job (e.g. `draft-summaries`): write `_job_<name>(payload)` in
`webhook/routers/cron.py`, add it to `JOBS`, and give it its own cron service
with `CRON_JOB=<name>`.

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
- To restore the strong reading: give the webhook's `GH_PAT` (fine-grained)
  repository access to **FirstPersonSF/cp** with **Actions: Read-only** (it
  already reads cp-engine). No new env var; nothing else changes.

## Railway setup (project "Fathom Meeting sync")

1. **New service** → GitHub repo `FirstPersonSF/cp-engine`, branch `main`.
   Name: **`cp-cron-health`**. No public domain.
2. **Variables**:

   | Name | Value |
   |---|---|
   | `RAILWAY_DOCKERFILE_PATH` | `webhook/cron/Dockerfile` |
   | `CP_WEBHOOK_URL` | `https://${{cp-engine.RAILWAY_PUBLIC_DOMAIN}}` (or `https://cp-engine-production.up.railway.app`) |
   | `WEBHOOK_HMAC_SECRET` | `${{cp-engine.WEBHOOK_HMAC_SECRET}}` (reference variable — never pasted) |
   | `CRON_JOB` | `health` |
   | `CRON_SCHEDULE` | `23 12,13 * * *` — must equal the Cron Schedule setting |

   Nothing else: no Supabase, Slack or GitHub token in this service. All of
   that stays in `cp-engine`.
3. **Settings → Build**: Dockerfile builder (the variable above selects the
   file). Watch paths: `webhook/cron/**`.
4. **Settings → Deploy**: Cron Schedule `23 12,13 * * *`; Restart policy
   **Never**; start command empty (the Dockerfile's `CMD python trigger.py`).
   No healthcheck.
5. **Verify before the first real slot**: add `CRON_DRY_RUN=1` and
   `CRON_SLOT=23 12 * * *`, deploy, read the logs — expect
   `HTTP 200 · dry_run` and the rendered lines, with no Slack post. Then
   remove both variables. Confirm the deploy logs show `python trigger.py`
   ran (a service that built the wrong Dockerfile fails silently otherwise).
6. **Retire the GitHub schedule**: remove the two `schedule` crons from the
   tenant's `.github/workflows/health.yml` (keep `workflow_dispatch`). Do it
   the same day, or both will post — the route's idempotency only covers its
   own ledger, not the Actions job.

The cp-engine webhook must be deployed with the route first (it ships with
this change on `main`).

Exit codes, as Railway sees them: 0 on any 2xx (posted / skipped /
duplicate / dry_run); 1 on non-2xx or a network error; 2 on missing config or
an unattributable fire.
