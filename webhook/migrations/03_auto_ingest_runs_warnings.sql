-- 03_auto_ingest_runs_warnings.sql — 2026-09-30. Replayable. APPLIED 2026-10-01 (ledger: auto_ingest_runs_warnings).
--
-- Architecture plan step 3 (fail loudly). Owner: cp-engine-webhook per mc-2
-- backend/migrations/SCHEMA_OWNERSHIP.md. Apply via MCP apply_migration
-- (ledger name: auto_ingest_runs_warnings) per MIGRATIONS.md rule 1; this
-- file is the owning-repo copy per rule 2.
--
-- `errors` drives status ('failed' when any project errored) and the
-- stranded-run selection in `cxp rerun-failed-ingests`. Side-step failures
-- (meeting-row fetch, cross-project roster, transcript persist, meeting link,
-- retrospective / inbox card, commitments proposal, meeting artifacts) must
-- NOT flip status or be offered for replay — but until now they were only
-- logged, so the row read clean while a side-step had silently failed.
-- They now land here. Until this column exists the webhook folds them into
-- plan_summary under the key "_warnings" (non-dict, so replay counting skips
-- it) — column-tolerant, like correlation_id was.

alter table public.auto_ingest_runs
    add column if not exists warnings text[];

comment on column public.auto_ingest_runs.warnings is
    'Side-step failures that did not fail the run (fetch/persist/link/'
    'propose/artifact). Non-empty = the run is PARTIAL in cxp health. '
    'Architecture plan step 3.';
