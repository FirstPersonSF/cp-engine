"""Which of a job's estimates cp-engine should read (#284).

MIRRORS `mc-2:backend/src/lib/estimate_scope.py`. The rule is Mission
Control's, not ours — cp-engine is a reader of the estimator schema, so a
second opinion about which estimate counts is a bug waiting for someone to
notice the two systems disagree about what a job sold.

WHAT CHANGED. Before the estimator rebuild a job had ONE estimate that
mattered, flagged `is_default`. A job can now carry several sold estimates
that sum, so a single default answers nothing and mc-2 migration 183 drops
the column. PostgREST answers a filter on a missing column with a 42703
ERROR, not an empty set — and `sync.py` catches that exception, logs, and
carries on, so every project's spine substance would mirror UNBOUND while
the sync still reported success. Quiet and total.

THE BRIDGE IS DELIBERATE, NOT A FALLBACK THAT HIDES A FAULT. `status` exists
only from migration 181, and measured against production 2026-09-17: 52
estimate rows across 45 jobs, **zero approved**. An approved-only rule would
empty every spine on the day it shipped. Until a job approves something, this
returns the estimates flagged `on_schedule` — which migration 181 seeded from
the old `is_default`, verified as an exact match (45 True / 7 False, the same
partition `is_default` carried). So the day this ships, every surface shows
precisely what it showed before.

RETIRE IT when live jobs carry approved estimates: delete the second branch
and these surfaces follow status alone.

`abandoned` counts nowhere — not money, not schedule, not the spine.
"""

from __future__ import annotations

from typing import Any

from cp_engine.mc2_db import Tables

# Explicit columns (never `SELECT *`): identity, the two fields the rule
# reads, `created_at` for the oldest-first ordering, and `mc_project_id` —
# the join key back to public.projects.
#
# `mc_project_id` is here because CALLERS consume the row this returns, not
# just the id: `fetch_estimate` builds its Estimate from it and requires the
# key. Selecting only what the RULE reads raised on all 45 live jobs
# ("estimator project row missing required key 'mc_project_id'") while every
# unit test passed, because the fakes carried the field the real query no
# longer asked for. A resolver's column list is part of its contract.
_SCOPE_COLUMNS = "id, mc_project_id, name, status, on_schedule, created_at"


def _estimates(client, mc_project_id: str) -> list[dict[str, Any]]:
    """Every estimate row on the job, oldest first."""
    return (
        client.schema("estimator")
        .table(Tables.EST_PROJECTS)
        .select(_SCOPE_COLUMNS)
        .eq("mc_project_id", mc_project_id)
        .order("created_at")
        .execute()
        .data
        or []
    )


def rendered_estimates(client, mc_project_id: str) -> list[dict[str, Any]]:
    """The estimates the spine and planning surfaces should read, oldest first.

    Approved estimates when the job has any; otherwise the `on_schedule`
    bridge described in the module docstring.
    """
    rows = _estimates(client, mc_project_id)
    approved = [e for e in rows if e.get("status") == "approved"]
    if approved:
        return approved
    return [
        e for e in rows
        if e.get("status") != "abandoned" and e.get("on_schedule")
    ]


def rendered_estimate(client, mc_project_id: str) -> dict[str, Any] | None:
    """The single estimate today's one-estimate callers should read, or None.

    `fetch_estimate` and the two planning lookups each resolve exactly one
    estimate. Keeping that signature is deliberate: a job carrying SEVERAL
    approved estimates is a real future case, but summing them changes what
    every downstream figure means, and doing that silently inside a
    compatibility fix is how a scope change ships unreviewed. Oldest approved
    wins; the multi-estimate case is tracked separately.
    """
    rows = rendered_estimates(client, mc_project_id)
    return rows[0] if rows else None
