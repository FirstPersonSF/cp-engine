-- 196_drop_retired_cp_tables.sql   (proposed name — renumber to the next free
-- slot in mc-2/backend/migrations/ when it moves there; NOT applied)
--
-- Architecture plan step 5a (cp-engine branch arch/step5a-retire, 2026-10-01):
-- four tables nothing reads or writes any more.
--
--   project_members, account_members
--       Created out-of-band, backfilled as files by mig 123, EMPTY and
--       deliberately unwired (no policy or function points at them). The
--       per-project scoping they were the substrate for was never built and
--       the roster is still uniform. When it stops being uniform, design the
--       membership from scratch; mig 123's header keeps the rollout order.
--   worksets (+ mig 163's note_author / note_dated)
--       One row ever (ibx-5153:copywriting, opened 4 times on 08-27). The
--       hosted open_workset / list_worksets / describe_workset tools and
--       CLAUDE.md's Mode 4 were retired in step 5a.
--   spine_snapshots
--       One row (06-13). `cxp snapshot` / `cxp snapshots`, the sync-time index
--       mirror and the project-code re-homer were retired in step 5a. The one
--       frozen file on disk stays in the tenant tree as hand-owned history;
--       it never needed this index to be read.
--
-- READERS CHECKED on the step-5a branch before writing this (grep, whole
-- word): cp-engine src/, webhook/, prototypes/ (hosted server), plugin/,
-- actions/, tests/ — zero references to any of the four. mc-2 backend/src,
-- frontend/src, backend/tests — zero. Other repos on the same database
-- (fathom-meeting-sync, meeting-synthesizer, 1p-component-library,
-- cp-email-worker, auth-email-hook, 1p-dashboard, estimator) — zero.
-- (storyos has its own `project_members`, in its own database.)
-- mc-2 migrations: only the files that created/altered them (062, 078, 123,
-- 161, 163, the 2026-07-03 baseline). No view, function or policy outside
-- the tables' own policies refers to them in any migration file.
--
-- Because two of these were created out-of-band, the live database may hold
-- something the files do not show. So this migration CHECKS before it drops:
--   * the membership tables must still be empty (a populated one means
--     someone started building scoping — stop and ask);
--   * no function body in public may name any of the four;
--   * DROP ... RESTRICT (the default) fails on any dependent view or foreign
--     key, rather than CASCADE silently taking it with it.
-- The tables' own RLS policies, indexes and grants drop with them.

begin;

do $$
declare
    n bigint;
    hits text;
begin
    select count(*) into n from public.project_members;
    if n > 0 then
        raise exception 'project_members has % row(s) — per-project scoping was started; do not drop', n;
    end if;
    select count(*) into n from public.account_members;
    if n > 0 then
        raise exception 'account_members has % row(s) — per-project scoping was started; do not drop', n;
    end if;

    select string_agg(p.oid::regprocedure::text, ', ') into hits
      from pg_proc p
      join pg_namespace ns on ns.oid = p.pronamespace
     where ns.nspname in ('public', 'core', 'governance', 'ingest', 'search', 'estimator')
       and p.prosrc ~* '\m(project_members|account_members|worksets|spine_snapshots)\M';
    if hits is not null then
        raise exception 'function(s) still reference a table this drops: %', hits;
    end if;
end $$;

drop table if exists public.project_members restrict;
drop table if exists public.account_members restrict;
drop table if exists public.worksets restrict;
drop table if exists public.spine_snapshots restrict;

commit;

-- Down: not reversible as data (worksets and spine_snapshots held one row
-- each; the membership tables were empty). The DDL to recreate them is in
-- mig 123 (project_members, account_members), 161 + 163 (worksets) and the
-- 2026-07-03 baseline + mig 078 (spine_snapshots).
