-- 04_webhook_runs.sql — 2026-09-30. Replayable. NOT YET APPLIED.
--
-- Architecture plan step 3 (fail loudly). Owner: cp-engine-webhook per mc-2
-- backend/migrations/SCHEMA_OWNERSHIP.md. Apply via MCP apply_migration
-- (ledger name: webhook_runs) per MIGRATIONS.md rule 1; this file is the
-- owning-repo copy per rule 2.
--
-- One row per webhook POST on EVERY path, written by
-- webhook/run_ledger.RunLedgerMiddleware (and a second '#background' row when
-- a 202 route's background tail finishes). Thirteen of seventeen routes wrote
-- no run record at all, and the four that did wrote none when the request
-- died before the pipeline (signature, JSON, duplicate short-circuit).
-- Read by `cxp health` (failures in the last 24h).

create table if not exists public.webhook_runs (
    id uuid primary key default gen_random_uuid(),
    route text not null,
    status text not null
        check (status in ('ok', 'partial', 'failed', 'rejected', 'accepted')),
    http_status integer,
    project_code text,
    error text,
    detail jsonb not null default '{}'::jsonb,
    duration_ms integer,
    correlation_id text,
    created_at timestamptz not null default now()
);

create index if not exists webhook_runs_created_at_idx
    on public.webhook_runs (created_at desc);
create index if not exists webhook_runs_status_created_idx
    on public.webhook_runs (status, created_at desc);

-- Service-role writes only (the webhook); no anon/authenticated access.
alter table public.webhook_runs enable row level security;
revoke all on public.webhook_runs from anon, authenticated;
