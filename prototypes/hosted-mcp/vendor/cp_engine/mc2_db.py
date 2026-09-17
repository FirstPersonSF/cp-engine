"""VENDORED from `cp_engine.mc2_db` — the table-name constants only.

The hosted container ships no `cp_engine` package (see the Dockerfile), and
importing the real module would pull `config`/`packaging` and the rest of the
CLI's dependency tree into a server that installs six packages.

`Tables` is pure constants with no imports, so it is copied WHOLE rather than
excerpted — a hand-picked subset is how two spellings of a table name start.
A drift test asserts this matches the source class exactly.
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
