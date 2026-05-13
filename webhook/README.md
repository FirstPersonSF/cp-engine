# cp-engine-webhook

FastAPI service that auto-ingests Fathom meetings into the cp tenant when fathom-meeting-sync calls it. Phase C of the meeting-type cascade.

## Architecture

```
fathom-meeting-sync (Node, Railway)
        │
        │ HMAC-signed POST /api/auto-ingest
        │ { meeting_id, project_codes, transcript_text? }
        ▼
cp-engine-webhook  (Python/FastAPI, Railway, co-located)
        │
        ├── verify HMAC
        ├── (if transcript_text absent) fetch from Supabase
        ├── git clone --depth=10 cp tenant via deploy key
        ├── for each project_code:
        │     ├── cp_engine.plan_from_transcript.generate_plan()  (Claude call)
        │     ├── cp_engine.ingest.execute_plan()
        ├── git add -A && git commit -m "[auto-ingest] ..."
        ├── git push origin main
        └── return { ingested, commit_sha, skipped_no_op }
```

The webhook is **stateless across requests** — each call does a fresh clone, applies the plan, pushes, and discards the working copy. This avoids stale-state bugs and concurrent-write races at the cost of a few seconds of clone overhead per call.

## Local development

```bash
# From the cp-engine repo root, with cp-engine installed in your venv:
pip install -e ./webhook

# Set env vars (see "Required env vars" below).

# Run:
uvicorn main:app --reload --app-dir webhook
```

To test against a meeting without fathom-meeting-sync wired up:

```bash
SECRET="local-dev-secret"
BODY='{"meeting_id":"b57c4b8d-4003-498b-acda-c82686a2783e","project_codes":["ggl-5136"]}'
SIG=$(python3 -c "import hmac,hashlib,os; print(hmac.new(os.environ['SECRET'].encode(), os.environ['BODY'].encode(), hashlib.sha256).hexdigest())")

WEBHOOK_HMAC_SECRET=$SECRET curl -X POST http://localhost:8000/api/auto-ingest \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Signature: $SIG" \
  -d "$BODY"
```

## Required env vars

| Variable | Purpose |
|---|---|
| `WEBHOOK_HMAC_SECRET` | Shared secret with fathom-meeting-sync for request signing |
| `ANTHROPIC_API_KEY` | For Claude plan generation |
| `SUPABASE_URL` | For fetching transcripts (only needed when caller omits `transcript_text`) |
| `SUPABASE_SERVICE_KEY` | Service-role key for the same |
| `CP_TENANT_REPO_URL` | `git@github.com:FirstPersonSF/cp.git` |
| `GIT_SSH_KEY` | Private SSH key matching the deploy key on the cp tenant |
| `GIT_AUTHOR_NAME` | Defaults to `cp-engine-webhook` |
| `GIT_AUTHOR_EMAIL` | Defaults to `webhook@firstperson.is` |

## Deploying to Railway

1. **Generate a deploy key** for the cp tenant repo:
   ```bash
   ssh-keygen -t ed25519 -C "cp-engine-webhook deploy" -f /tmp/cp_webhook_key -N ""
   cat /tmp/cp_webhook_key.pub  # add this to github.com/FirstPersonSF/cp Settings → Deploy keys, "Allow write access"
   cat /tmp/cp_webhook_key      # set this as GIT_SSH_KEY in Railway
   rm /tmp/cp_webhook_key /tmp/cp_webhook_key.pub
   ```

2. **Add a service to the existing Railway project** (the one running fathom-meeting-sync). Connect it to the `FirstPersonSF/cp-engine` GitHub repo, `main` branch.

3. **Point Railway at this directory's `railway.toml`** — it tells Railway to use `webhook/Dockerfile` with the repo root as build context.

4. **Set the env vars** above. `WEBHOOK_HMAC_SECRET` should be a fresh random string; share it with fathom-meeting-sync.

5. **Deploy.** First boot pulls cp-engine source, installs it + webhook, starts uvicorn. `/health` should return 200 with the cp-engine version.

## Trust boundary

The auto-commit step is the riskiest part. Safety nets:

1. **HMAC signature** on every request — only fathom-meeting-sync (the secret holder) can trigger an ingest.
2. **Plan validation is byte-identical to `cp ingest --dry-run`.** Same `_validate_plan` call. If Claude produces a malformed plan, we never write.
3. **Idempotency markers** in the plan executor mean re-firing the same meeting can never duplicate content.
4. **`[auto-ingest]` commit prefix** makes auto-generated commits trivially identifiable and revertible.

What we accept: low-quality (but valid) plans landing in main. A human can always add a follow-up `/cp-ingest --project <code>` to enrich, or revert the auto-ingest commit if the extraction was off.
