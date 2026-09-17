"""VENDORED from `cp_engine.mc2_db` — the constants only, no client code.

The container ships no `cp_engine` (see the Dockerfile) and the real module
imports httpx+supabase and carries the whole DAL. The vendored lint modules
need exactly two kinds of thing from it: the `Tables` names and the column
lists.

EVERY module-level literal constant is copied, not the ones today's callers
happen to touch. Picking constants one at a time is how this file shipped with
`Tables` and without `SPINE_LINT_COLUMNS` — which passed an import test and
raised AttributeError on the first real call, one deploy after the import fix.

NEVER `SELECT *`: these column lists are the reason reads stay small, and a
missing one here becomes a wrong query, not an obvious crash. A drift test
asserts each matches the source.
"""

from __future__ import annotations


class Tables:
    """Every MC-2 table cp-engine or the webhook touches. Grep-enforced.

    ``public`` schema unless noted. The estimator-schema names carry an
    ``EST_``/explicit prefix and must be used with
    ``client.schema("estimator")``.
    """

    # public — core entities
    PROJECTS = "projects"
    REPOS = "repos"
    INITIATIVES = "initiatives"
    COMPANIES = "companies"
    ENTITIES = "entities"  # people (partners/recipients); note author + recipient
    GITHUB_ORGS = "github_orgs"  # embedded-join only today (see *_COLUMNS)
    SPRINT_ALLOCATIONS = "sprint_allocations"
    NOTES = "notes"  # partner pings (in-app unread + Slack DM, mig 116)

    # public — meetings + ingest
    FATHOM_MEETINGS = "fathom_meetings"
    UNROUTED_EMAILS = "unrouted_emails"  # inbound emails w/o a routable project (mig 122)
    AUTO_INGEST_RUNS = "auto_ingest_runs"
    SLACK_DIGEST_RUNS = "slack_digest_runs"  # per-channel digest outcomes (mig 168, #227)
    VENDORS = "vendors"  # cross-client partner registry (mig 169, RFP v3 §5)
    RFP_RESPONDENTS = "rfp_respondents"  # per-project RFP pipeline (mig 169, §3)
    ASSET_INGEST_RUNS = "asset_ingest_runs"
    RAG_ASSETS = "rag_assets"
    ASSET_CHUNKS = "asset_chunks"  # chunk text; embeddings FK-cascade on delete
    CLICKUP_TASK_PROPOSALS = "clickup_task_proposals"
    COMMITMENTS = "commitments"
    APP_CONFIG = "app_config"  # tenant-level key/value settings (jsonb)
    # The master prompt (mig 139). Judgment priors sent as `system=` on every
    # LLM call; NOT project canon, which stays as canon_of edges per spec v04.
    # Reads go through the `cp_prompt_resolve` RPC (see cp_engine.priors);
    # writes go through the team-gated definer fns — never a direct upsert.
    CP_PROMPT = "cp_prompt"
    CP_PROMPT_OVERRIDE = "cp_prompt_override"

    # public — spine
    SPINE_SUBSTANCE = "spine_substance"
    SPINE_CONTEXT = "spine_context"
    SPINE_ELEMENTS = "spine_elements"
    SPINE_SNAPSHOTS = "spine_snapshots"
    SPINE_INBOX = "spine_inbox"
    SPINE_PROMOTE_RUNS = "spine_promote_runs"
    SPINE_RELATIONS = "spine_relations"  # typed element->element edges (mig 117)
    SPINE_STEPS = "spine_steps"  # ordered progress trail inside an element (mig 119)
    # Weekly-sort pre-pass output (mig 142). One ACTIVE proposal per substance
    # row; a re-run supersedes rather than overwrites, so a prompt edit that
    # changes an answer is visible. Never writes spine_substance.lifetime — a
    # human confirming does that.
    SPINE_SORT_PROPOSALS = "spine_sort_proposals"

    # estimator schema
    EST_PROJECTS = "projects"
    EST_PHASES = "phases"
    EST_PHASE_ACTIVITIES = "phase_activities"
    EST_PHASE_DELIVERABLES = "phase_deliverables"
    EST_SCHEDULE_ITEMS = "schedule_items"


PROJECTS_SYNC_COLUMNS = (
    "id, number, full_job_name, name, mc_status, account_manager, "
    "is_internal, deal_stage, budget, dropbox_folder_url, updated_at, "
    "companies(code, name, kind), "
    "repos!project_id(repo_name, status, description, github_orgs!inner(name))"
)


PROJECTS_SLACK_COLUMNS = (
    "id, number, name, mc_status, is_internal, enable_slack, "
    "full_job_name, companies!inner(code)"
)


PROJECTS_ESTIMATE_COLUMNS = "id, start_date"


REPOS_SYNC_COLUMNS = (
    "id, repo_name, status, description, owner, updated_at, "
    "github_orgs!inner(name), "
    "companies!inner(code, name, kind)"
)


INITIATIVES_SYNC_COLUMNS = (
    "id, code, name, description, status, owner, updated_at, "
    "enable_slack, "
    "companies!inner(code, name, kind), "
    "repos!initiative_id(repo_name, status, description, github_orgs!inner(name))"
)


INITIATIVES_SLACK_COLUMNS = (
    "id, code, name, status, enable_slack, companies!inner(code)"
)


FATHOM_LIST_COLUMNS = (
    "id, title, meeting_date, project_tags, duration_minutes, meeting_type"
)


FATHOM_FULL_COLUMNS = FATHOM_LIST_COLUMNS + ", transcript, summary"


FATHOM_TRANSCRIPT_COLUMNS = "id, title, meeting_date, transcript, participants"


FATHOM_ARTIFACT_COLUMNS = (
    "id, title, meeting_date, summary, action_items, "
    "participants, duration_minutes, fathom_url, "
    "recording_id, project_tags, project_id, summary_embedded_at"
)


SPINE_LIST_COLUMNS = (
    "est_item_id, framing, layer, binding, status, serves, body, important, "
    "note, archived, scope, company_id, project_id, version_label, version_date"
)


SPINE_PULL_COLUMNS = (
    "est_item_id, framing, layer, binding, status, serves, sources, "
    "version_label, version_date, body, important, note, archived, scope, "
    "company_id, project_id"
)


SPINE_RESOLVE_COLUMNS = (
    "id, est_item_id, framing, status, important, note, rel_path, archived, "
    "scope, company_id, project_id, layer, version_label, version_date"
)


SPINE_STATUS_COLUMNS = (
    "est_item_id, status, version_date, version_label, binding, project_id"
)


SPINE_LINT_COLUMNS = (
    "est_item_id, framing, layer, binding, serves, important, body, "
    "sources, status, archived, scope, project_id, version_label, version_date"
)


SEAL_SWEEP_COLUMNS = (
    "est_item_id, framing, layer, status, archived, version_label, "
    "version_date, serves, project_id"
)


STUB_SWEEP_COLUMNS = (
    "est_item_id, framing, layer, status, archived, body, sources, serves, "
    "version_date, project_id"
)


SPINE_SOURCES_EDIT_COLUMNS = (
    "id, est_item_id, framing, status, archived, scope, company_id, "
    "project_id, sources"
)


RAG_ASSET_LIST_COLUMNS = (
    "id, title, source_type, status, created_at, file_hash, prev_asset_id, "
    "description, status_note, supersedes_asset_id"
)


RAG_ASSET_REFETCH_COLUMNS = (
    "id, title, source_provider, source_file_id, source_path, url"
)


EST_PROJECT_COLUMNS = "id, mc_project_id, name, is_default"


EST_PHASE_COLUMNS = "id, name, overview, position"


EST_ITEM_COLUMNS = "id, phase_id, name, short_description, library_item_id, position"


EST_SCHEDULE_COLUMNS = (
    "id, project_id, phase_id, label, start_week, duration, position, "
    "item_type, emphasis, work_item_id, work_item_kind, done"
)


_SUPABASE_KEYS = ("SUPABASE_URL", "SUPABASE_SERVICE_KEY")


_INGEST_KEYS = ("OPENAI_API_KEY", "VOYAGE_API_KEY")


_DROPBOX_KEYS = (
    "DROPBOX_APP_KEY",
    "DROPBOX_APP_SECRET",
    "DROPBOX_REFRESH_TOKEN",
    "DROPBOX_ACCESS_TOKEN",
)
