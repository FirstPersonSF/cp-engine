"""cp-engine-webhook — Phase C.2

FastAPI service that fathom-meeting-sync calls when a high-confidence
project-status meeting arrives. Generates an ingest plan via Claude,
applies it to a fresh clone of the cp tenant, commits + pushes.

Deployment model: Railway service co-located with fathom-meeting-sync
under the same Railway project (shares env vars + metrics). SSH-based
git push using a deploy key on the cp tenant repo with write access.

Layout (arch-phase-4, cp-engine #32 — split from a single 4k-LOC file):
  main.py            — app creation, middleware, logging, Sentry, health
  signatures.py      — HMAC + timestamp verification (all three schemes)
  git_ops.py         — tenant clone → commit → push lifecycle
  pipeline.py        — shared auto-ingest pipeline + Supabase glue
  routers/ingest.py  — POST /api/auto-ingest, …/runs/{id}/rerun,
                       /api/auto-ingest-account, /api/auto-ingest-sprint-planning
  routers/spine.py   — POST /api/spine/promote, /api/spine/promote-transcript
  routers/meetings.py— POST /api/meetings/promote-transcript, /api/meetings/synthesize
  routers/assets.py  — POST /api/assets/ingest
  routers/integrations.py — POST /api/resolve-tags, /clickup-task-closed
  routers/slack.py   — POST /slack-action

Endpoints:
  POST /api/auto-ingest   — main entry; HMAC-signed by caller
  POST /clickup-task-closed — ClickUp → cp bullet-flip (v0.12+); HMAC-signed
  POST /slack-action      — Slack interactive button + modal (v0.14+); HMAC-signed
  GET  /health            — liveness check; reports cp-engine version

Required env vars:
  WEBHOOK_HMAC_SECRET     — shared secret with fathom-meeting-sync
  ANTHROPIC_API_KEY       — for plan generation
  SUPABASE_URL            — for fetching transcripts
  SUPABASE_SERVICE_KEY    — service-role key (server-side only)
  CP_TENANT_REPO_URL      — git@github.com:FirstPersonSF/cp.git
  GIT_SSH_KEY             — private SSH key matching the deploy key
  GIT_AUTHOR_NAME         — "cp-engine-webhook"
  GIT_AUTHOR_EMAIL        — "webhook@firstperson.is" or similar
  CLICKUP_WEBHOOK_SECRET  — (v0.12+) HMAC for /clickup-task-closed; separate
                            from WEBHOOK_HMAC_SECRET so they rotate independently
  SLACK_SIGNING_SECRET    — (v0.14+) HMAC for /slack-action; from Slack app's
                            Basic Information page
  SLACK_BOT_TOKEN         — (v0.14+) `xoxb-...`; used by views.open to launch
                            the "Snooze until…" date-picker modal

Required BUILD-TIME token (NOT a runtime env var):
  gh_token                — read-only GitHub token used to clone the private
                            git+https deps (Canonic-OS/canonic-component-
                            library). Accepted by webhook/Dockerfile via
                            EITHER a BuildKit secret (id=gh_token, preferred,
                            never in image history) OR a build ARG `gh_token`.
                            On Railway: add a service VARIABLE named `gh_token`
                            (lowercase) under Service → Variables — Railway
                            passes service variables to the build as ARGs
                            (Railway does NOT support BuildKit secret mounts).
                            Scope: Contents:read on canonic-component-library.
                            Consumed only inside the install RUN; never in ENV.
                            Local: docker build --secret id=gh_token,env=GH_TOKEN …
                            (or --build-arg gh_token=$GH_TOKEN to mirror Railway)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

# tests patch `main.mc2_db.get_client` and the mutation reaches every module.
import observability
from fastapi import FastAPI, Request

import cp_engine
from cp_engine import mc2_db  # noqa: F401 — module object shared with all routers;

# [cid:...] is the per-delivery correlation id (observability.py). "-" outside
# a request context (startup, background tasks spawned without one).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [cid:%(cid)s] %(message)s",
)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(observability.CorrelationIdFilter())
log = logging.getLogger("cp-engine-webhook")

# Error alerting (issue #28): strict no-op without SENTRY_DSN in the env.
observability.init_sentry(release=cp_engine.__version__)

app = FastAPI(title="cp-engine-webhook", version=cp_engine.__version__)


@app.middleware("http")
async def _correlation_id_middleware(request: Request, call_next):
    """One correlation id per delivery, set before any handler code runs.

    Honors an incoming ``X-Correlation-ID`` (fathom-meeting-sync can
    originate the id and grep both services' logs with it); generates a
    short unique id otherwise. Echoed back in the response header and
    tagged onto Sentry's per-request scope so unhandled route exceptions
    carry it too.
    """
    cid = observability.new_correlation_id(request.headers.get("x-correlation-id"))
    observability.tag_request_scope()
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = cid
    return response


@app.get("/health")
def health() -> dict:
    """Liveness probe. Reports the cp-engine version we're running against."""
    return {
        "status": "healthy",
        "cp_engine_version": cp_engine.__version__,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }


# Routers are imported AFTER logging/Sentry setup so their import-time
# side effects (there should be none) can't race the log configuration.
from routers import assets as _assets_router  # noqa: E402
from routers import dates_loop as _dates_loop_router  # noqa: E402
from routers import email as _email_router  # noqa: E402
from routers import ingest as _ingest_router  # noqa: E402
from routers import integrations as _integrations_router  # noqa: E402
from routers import meetings as _meetings_router  # noqa: E402
from routers import slack as _slack_router  # noqa: E402
from routers import spine as _spine_router  # noqa: E402

app.include_router(_integrations_router.router)
app.include_router(_ingest_router.router)
app.include_router(_spine_router.router)
app.include_router(_assets_router.router)
app.include_router(_dates_loop_router.router)
app.include_router(_meetings_router.router)
app.include_router(_slack_router.router)
app.include_router(_email_router.router)


# ── Back-compat re-exports (arch-phase-4 split) ───────────────────────
# Tests and callers historically reached every helper on `main`. These are
# import-time VALUE bindings kept so direct CALLS keep working
# (`main._push_with_retry(...)`). CAUTION: monkeypatching `main.<name>`
# does NOT affect route behavior anymore — patch the owning module
# (signatures / git_ops / pipeline / routers.*) instead.
from git_ops import (  # noqa: E402,F401
    _NON_FAST_FORWARD_MARKERS,
    _cloned_tenant,
    _commit_and_push,
    _commit_and_push_promote,
    _commit_clickup_close,
    _commit_meeting_artifacts,
    _commit_with_message_and_push,
    _correlation_trailer,
    _push_with_retry,
    _ssh_env,
)
from pipeline import (  # noqa: E402,F401
    DEFAULT_MODEL,
    _action_item_texts,
    _append_inbox_card,
    _append_retrospective,
    _background_tasks,
    _create_supabase_client,
    _fetch_meeting,
    _fetch_transcript,
    _find_successful_duplicate_run,
    _generate_meeting_artifacts,
    _ingest_one_project,
    _link_meeting_safe,
    _load_tenant_config,
    _log_run_to_supabase,
    _normalize_transcript,
    _perform_auto_ingest,
    _persist_transcript,
    _plan_decisions,
    _sanitize_transcript_title,
    _segments_to_text,
    _spawn_background,
    _speaker_names,
    _stage_transcript,
    _status_from_ingested,
)
from routers.assets import _asset_runs_table, _run_asset_ingest  # noqa: E402,F401
from routers.integrations import (  # noqa: E402,F401
    _lookup_proposal_by_clickup_task_id,
    _resolve_engagement_code,
    _resolve_initiative_code,
)
from routers.meetings import (  # noqa: E402,F401
    _fetch_meeting_by_recording_id,
    _run_meeting_promote,
    _run_meeting_synthesize,
    _transcript_segments_for_service,
)
from routers.slack import (  # noqa: E402,F401
    _confirmation_text,
    _handle_block_action,
    _handle_view_submission,
    _open_snooze_modal,
    _post_response_url_update,
    _run_action_in_background,
    _run_plan_for_one_item,
)
from routers.spine import (  # noqa: E402,F401
    _frame_promote_in_tree,
    _resolve_project_id_for_promote,
    _run_frame_promote,
    _run_promote,
    _spine_promote_runs_table,
)
from signatures import (  # noqa: E402,F401
    _TIMESTAMP_REPLAY_WINDOW_SEC,
    _truthy_env,
    _verify_clickup_signature,
    _verify_signature,
    _verify_slack_signature,
)

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
