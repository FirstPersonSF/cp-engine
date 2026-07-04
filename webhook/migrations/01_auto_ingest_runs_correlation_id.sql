-- arch-phase-3 (cp-engine #28): correlation id per webhook delivery,
-- threaded webhook receipt -> clone -> plan -> commit -> push. Owner:
-- cp-engine-webhook per mc-2 backend/migrations/SCHEMA_OWNERSHIP.md.
-- Applied 2026-07-03 via MCP apply_migration (ledger name:
-- auto_ingest_runs_correlation_id) per MIGRATIONS.md rule 1; this file
-- is the owning-repo copy per rule 2.
alter table public.auto_ingest_runs
    add column if not exists correlation_id text;
