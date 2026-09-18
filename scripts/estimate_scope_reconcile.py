#!/usr/bin/env python3
"""Report what `estimate_scope` renders for every job, job by job (#292, #291).

THE CLAIM THIS MAKES REPEATABLE. `cp_engine.estimate_scope.rendered_estimates`
is the one rule for which of a job's estimates count, mirrored from mc-2.
Since #291 every cp-engine consumer reads the whole admitted set (a job can
carry several sold estimates that render together — the 5195 shape), so the
question worth re-asking is no longer "does the pick match `is_default`" (that
column is gone; migration 183 landed 2026-09-17) but:

  * how many jobs render ONE estimate, how many render SEVERAL — the several
    are where cp-engine and mc-2 used to disagree, silently;
  * how many still resolve through the `on_schedule` bridge (no approved
    estimate) — mc-2's own retirement trigger for the bridge;
  * how many carry a pending estimate NOT rendered — the additions waiting
    for approval, i.e. the next place the multi-estimate path gets exercised.

Read-only. `--dry-run` prints the queries it would issue and connects to
nothing. Otherwise it obtains a client exactly as the rest of cp-engine does
(`cp_engine.mc2_db.get_client`, creds from env then the tenant config), runs
two SELECTs with explicit columns, and prints a report. It never writes.

Usage (from a cp tenant working dir, or anywhere with SUPABASE_* in env):

    uv run python scripts/estimate_scope_reconcile.py            # live report
    uv run python scripts/estimate_scope_reconcile.py --dry-run  # queries only
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Runnable as a plain script (no editable install): put the package on the path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cp_engine.estimate_scope import _SCOPE_COLUMNS, rendered_estimates  # noqa: E402
from cp_engine.mc2_db import Tables  # noqa: E402

ESTIMATE_COLUMNS = _SCOPE_COLUMNS
JOB_COLUMNS = "id, code"

EXIT_OK = 0


# ── The core: pure over rows already in memory ────────────────────────


@dataclass(frozen=True)
class JobResult:
    code: str
    mc_project_id: str
    rendered_ids: tuple[str, ...]
    rendered_names: tuple[str, ...]
    via_bridge: bool
    pending_unrendered: tuple[str, ...]  # names of estimates waiting on approval

    @property
    def multi(self) -> bool:
        return len(self.rendered_ids) > 1


@dataclass
class Report:
    jobs_: list[JobResult] = field(default_factory=list)

    @property
    def jobs(self) -> int:
        return len(self.jobs_)

    @property
    def single(self) -> list[JobResult]:
        return [r for r in self.jobs_ if len(r.rendered_ids) == 1]

    @property
    def multi(self) -> list[JobResult]:
        return [r for r in self.jobs_ if r.multi]

    @property
    def empty(self) -> list[JobResult]:
        return [r for r in self.jobs_ if not r.rendered_ids]

    @property
    def bridge_jobs(self) -> int:
        return sum(1 for r in self.jobs_ if r.via_bridge)

    @property
    def awaiting(self) -> list[JobResult]:
        return [r for r in self.jobs_ if r.pending_unrendered]


class _RowsClient:
    """Just enough PostgREST chaining for `rendered_estimates` to run over
    rows that were already fetched, so the report exercises the real rule
    rather than a second spelling of it."""

    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows
        self._filters: list[tuple[str, Any]] = []
        self._order: str | None = None

    def schema(self, _name):
        return self

    def table(self, _name):
        return self

    def select(self, _columns):
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def order(self, col, **_kw):
        self._order = col
        return self

    def execute(self):
        rows = [
            r for r in self._rows
            if all(r.get(c) == v for c, v in self._filters)
        ]
        if self._order:
            key = self._order
            rows.sort(key=lambda r: (r.get(key) is None, r.get(key) or ""))
        self._filters, self._order = [], None
        return type("R", (), {"data": rows})()


def reconcile(
    estimate_rows: Iterable[dict[str, Any]],
    job_codes: dict[str, str] | None = None,
) -> Report:
    """Run the real rule over every job in `rows` and classify the result."""
    rows = list(estimate_rows)
    job_codes = job_codes or {}
    by_job: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_job[r["mc_project_id"]].append(r)

    client = _RowsClient(rows)
    report = Report()
    for mc_project_id in sorted(by_job):
        job_rows = by_job[mc_project_id]
        rendered = rendered_estimates(client, mc_project_id)
        rendered_ids = {r["id"] for r in rendered}
        report.jobs_.append(JobResult(
            code=job_codes.get(mc_project_id, mc_project_id),
            mc_project_id=mc_project_id,
            rendered_ids=tuple(r["id"] for r in rendered),
            rendered_names=tuple(r.get("name") for r in rendered),
            via_bridge=not any(r.get("status") == "approved" for r in job_rows),
            pending_unrendered=tuple(
                r.get("name") or r["id"] for r in job_rows
                if r["id"] not in rendered_ids and r.get("status") not in ("abandoned", "approved")
            ),
        ))
    return report


def render(report: Report) -> str:
    lines = [
        f"jobs with estimates: {report.jobs}",
        f"render one estimate:      {len(report.single)}",
        f"render several (#291):    {len(report.multi)}",
        f"render none:              {len(report.empty)}",
        f"resolving through the on_schedule bridge (no approved estimate): "
        f"{report.bridge_jobs}",
        f"carrying a pending estimate not yet rendered: {len(report.awaiting)}",
    ]
    if report.multi:
        lines.append("")
        lines.append("jobs rendering several estimates (oldest first):")
        for r in report.multi:
            names = ", ".join(f"{n!r} [{i}]" for n, i in zip(r.rendered_names, r.rendered_ids))
            lines.append(f"  {r.code}: {names}")
    if report.awaiting:
        lines.append("")
        lines.append("pending, not rendered (approve to add to the job's spine):")
        for r in report.awaiting:
            lines.append(f"  {r.code}: {', '.join(repr(n) for n in r.pending_unrendered)}")
    return "\n".join(lines)


# ── The live half: two explicit-column SELECTs, no writes ─────────────


def _queries() -> list[str]:
    return [
        f"estimator.{Tables.EST_PROJECTS}: SELECT {ESTIMATE_COLUMNS}",
        f"public.{Tables.PROJECTS}: SELECT {JOB_COLUMNS} WHERE id IN (<job ids above>)",
    ]


def fetch(client) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Every estimate row plus a code map for the jobs they belong to."""
    estimates = (
        client.schema("estimator")
        .table(Tables.EST_PROJECTS)
        .select(ESTIMATE_COLUMNS)
        .execute()
        .data
        or []
    )
    job_ids = sorted({r["mc_project_id"] for r in estimates if r.get("mc_project_id")})
    codes: dict[str, str] = {}
    if job_ids:
        jobs = (
            client.schema("public")
            .table(Tables.PROJECTS)
            .select(JOB_COLUMNS)
            .in_("id", job_ids)
            .execute()
            .data
            or []
        )
        codes = {j["id"]: j["code"] for j in jobs if j.get("code")}
    return estimates, codes


def _client():
    """The client every other cp-engine entry point uses: env creds first,
    then the tenant's `.cp-engine.toml` if we are standing in one."""
    from cp_engine import config as cp_config
    from cp_engine.mc2_db import get_client

    cfg = None
    try:
        cfg = cp_config.load(Path.cwd())
    except Exception:  # noqa: BLE001 — not in a tenant dir; env-only is fine
        cfg = None
    return get_client(cfg)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the queries this would run and exit without connecting",
    )
    args = ap.parse_args(argv)

    if args.dry_run:
        print("would run (read-only):")
        for q in _queries():
            print(f"  {q}")
        return EXIT_OK

    estimates, codes = fetch(_client())
    print(render(reconcile(estimates, codes)))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
