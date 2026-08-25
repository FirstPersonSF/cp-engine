-- 02_auto_ingest_runs_recovery_stamp.sql — 2026-08-24. Replayable.
--
-- cp-engine #220: `auto_ingest_runs` records what the pipeline ATTEMPTED,
-- never what has since been RECOVERED. Owner: cp-engine-webhook per mc-2
-- backend/migrations/SCHEMA_OWNERSHIP.md. Applied via MCP apply_migration
-- (ledger name: auto_ingest_runs_recovery_stamp) per MIGRATIONS.md rule 1;
-- this file is the owning-repo copy per rule 2.
--
-- ── The problem ──────────────────────────────────────────────────────
-- `cxp rerun-failed-ingests` selects the #194 stranded class:
-- status='success' carrying a non-empty `errors` array. Nothing in the row
-- changes when the content is restored, so a recovered run stays in scope
-- forever. Observed 2026-08-23: after a --limit 1 trial recovered one
-- meeting and committed it, the very next dry run still offered that run.
--
-- The duplicate is NOT caught by the usual safety net. `execute_plan`'s
-- content-hash dedupe makes an IDENTICAL bullet a no-op, but a replay
-- re-asks the model — the wording differs, the hash differs, and the
-- bullet lands twice. Silent, visible only by reading the sprint file.
--
-- Until now the only defenses were operator memory and --skip-meeting,
-- both of which fail the moment someone runs the bare command.
--
-- This is the same shape as #218/#219 and the resolve-risk bug: a surface
-- that looks authoritative while unable to distinguish two states — here
-- "never recovered" from "already recovered".
--
-- ── The columns ──────────────────────────────────────────────────────
-- `recovered_at` is the state; `recovered_by` is the provenance, because
-- the two recovery routes have different trust properties. A 'replay' was
-- regenerated from the transcript by the verb; a 'hand' recovery was
-- written by a person reading the meeting. Both mean "do not re-offer",
-- but only the first is reproducible, so keeping them apart matters when
-- auditing what the tenant actually contains.
--
-- Nullable with no default: the absence of a stamp is the meaningful
-- state (never recovered), and backfilling every historical row with a
-- timestamp would assert a recovery that never happened.

alter table public.auto_ingest_runs
    add column if not exists recovered_at timestamptz,
    add column if not exists recovered_by text;

comment on column public.auto_ingest_runs.recovered_at is
    'When this run''s dropped content was restored. NULL = never recovered; '
    'the selection default in cxp rerun-failed-ingests excludes non-NULL '
    '(override with --include-recovered). cp-engine #220.';

comment on column public.auto_ingest_runs.recovered_by is
    'How it was recovered: ''replay'' (regenerated from transcript by '
    'cxp rerun-failed-ingests) or ''hand'' (written by a person). Both '
    'suppress re-offering; only ''replay'' is reproducible. cp-engine #220.';

-- Partial index: the verb's hot path is "stranded AND not yet recovered".
-- Partial on the NULL side because that is the set being scanned, and it
-- shrinks as recovery progresses.
create index if not exists auto_ingest_runs_unrecovered_idx
    on public.auto_ingest_runs (created_at)
    where recovered_at is null;

-- ── Backfill: the 2026-08-23 recovery pass ───────────────────────────
-- Derived from the runs table itself (stranded rows with a meeting date in
-- the 08-10..08-18 window), cross-checked against the eight `recover:`
-- commits on the cp tenant's main branch. 22 rows in the window: 19 were
-- replayed by the verb, 3 were hand-recovered on 08-19 and deliberately
-- excluded from the replay by --exclude (slt-5196, ibx-5153) — they have
-- the same re-offer problem and are stamped here too.
--
-- Timestamps are the recovery dates, not now(): stamping today's date
-- would misreport when the content was restored, which is the same class
-- of error the verb's own meeting-date anchoring exists to prevent.
--
-- Idempotent: the WHERE guard means a re-run touches nothing.

update public.auto_ingest_runs
   set recovered_at = timestamptz '2026-08-23 00:00:00+00',
       recovered_by = 'replay'
 where recovered_at is null
   and id in (
    'd020eb01-7088-42cb-b1df-b91067abd831',  -- ibx-5192  08-10
    '1a044200-1721-4950-813b-cb0683b943cb',  -- ggl-5197  08-10
    'e522556a-c96a-49b9-a477-149c3d74555a',  -- ibx-5192  08-10 (--limit 1 trial)
    '8809ef1c-1a1b-4051-be89-dc661f0ea944',  -- slt-5195  08-11
    '15a87cd0-8ee6-46b9-ab8b-b39c08dc1d02',  -- ibx-5192  08-11
    'baf39bde-010e-4d67-acf5-955ce107b594',  -- ibx-5192  08-11
    '2602e136-9b1c-42ed-82e2-33c0f6b2469f',  -- ggl-5197  08-11
    '24c9a2c7-8f3c-499c-8014-a730b052567f',  -- sap-5171  08-12
    '201cee2c-ea8e-4eaa-b200-15757e7018bb',  -- ibx-5192  08-12
    '8d24c812-6c09-4704-9b33-c8aff22bb4df',  -- ibx-5192  08-12
    '7d78bf1f-3adf-4f00-a220-f087d69eea4e',  -- ibx-5192  08-12
    '7b422e5b-0b45-4bd0-a5e8-2932949a641b',  -- ggl-5197  08-13
    '07499ddd-039b-4cf8-9955-19b5a0525773',  -- ibx-5192  08-13
    '804a5921-1542-460b-b8fb-d01aabad4ccf',  -- slt-5195  08-13
    '7aca4475-8f4e-4f5e-9e72-8a6c5158e058',  -- ibx-5192  08-13
    'cd373632-fb1d-4cf6-89d1-83bedc8f39b0',  -- ggl-5197  08-14
    '89f60b72-596a-4c2a-9f97-f4cb8470c34f',  -- sap-5174  08-14
    '29a13526-272d-45aa-ae5f-09698319433c',  -- ggl-5151  08-18
    '519d92fe-01f2-4fc0-9d58-e3cadf8afb36'   -- ggl-5197  08-18
   );

update public.auto_ingest_runs
   set recovered_at = timestamptz '2026-08-19 00:00:00+00',
       recovered_by = 'hand'
 where recovered_at is null
   and id in (
    'bdfb9ad6-3e02-49c7-bee9-58672e4d68a1',  -- slt-5196  08-18
    '1c8afa17-2403-47a2-a8d9-2e178f608325',  -- slt-5196  08-18
    '5ba0ea62-ee6c-4c8d-9b55-52825eb968ad'   -- ibx-5153  08-18
   );

-- The remaining ~105 stranded rows carry meeting dates before 2026-08-10
-- and are deliberately left UNSTAMPED. Drew's two-week rule retired them
-- as noise rather than work — but "retired by decision" is not the same
-- fact as "recovered", and recording it as recovery would be a lie in the
-- data. The verb's --since default (14 days) is what keeps them out of
-- scope, and that is the honest mechanism.
