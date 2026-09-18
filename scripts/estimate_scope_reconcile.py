#!/usr/bin/env python3
"""Reconcile `rendered_estimate` against `is_default`, job by job (#292).

THE CLAIM THIS MAKES REPEATABLE. `cp_engine.estimate_scope` stops reading
`estimator.projects.is_default` before mc-2 migration 183 drops it, and the
bridge it reads instead (`on_schedule`, seeded by migration 181 from the old
flag) was verified against production on 2026-09-17 as an exact match: 45 live
jobs, every one resolving to precisely the estimate `is_default` named. That
verification lived in two docstrings and nowhere else — nothing could re-run
it. This can.

WHAT IT COMPARES. For every job carrying at least one estimate row: the
estimate `rendered_estimate` picks (the REAL function, run over the fetched
rows — not a restatement of the rule) against the estimate(s) flagged
`is_default`. Same when the ids match; differs otherwise. It also counts how
many jobs still resolve through the bridge (zero approved estimates), which is
the retirement trigger mc-2 names in its own docstring.

WHEN TO RUN IT. Before migration 183 lands — it SELECTs `is_default`, so after
the drop the query is a 42703 and the script says so and exits 3. That is
correct: once the column is gone there is nothing left to reconcile against.

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

from cp_engine.estimate_scope import _SCOPE_COLUMNS, rendered_estimate  # noqa: E402
from cp_engine.mc2_db import Tables  # noqa: E402

# The resolver's own columns plus the flag being reconciled against — the one
# place in cp-engine that may still spell `is_default`, because comparing
# against it is the whole point. `tests/test_estimate_scope.py` scans
# `src/cp_engine` for the token; this lives under `scripts/` on purpose.
ESTIMATE_COLUMNS = _SCOPE_COLUMNS + ", is_default"
JOB_COLUMNS = "id, code"

EXIT_OK = 0
EXIT_DIFFERS = 1
EXIT_COLUMN_GONE = 3


# ── The core: pure over rows already in memory ────────────────────────


@dataclass(frozen=True)
class JobResult:
    code: str
    mc_project_id: str
    rendered_id: str | None
    rendered_name: str | None
    default_ids: tuple[str, ...]
    default_names: tuple[str, ...]
    via_bridge: bool

    @property
    def same(self) -> bool:
        """Same when the rendered estimate IS the default one — by id.

        Names are printed for a human; they are not the comparison. Two
        estimates on one job may both be called "Estimate 1", and a name match
        would hide the resolver picking the wrong row.
        """
        return self.default_ids == ((self.rendered_id,) if self.rendered_id else ())


@dataclass
class Report:
    same: list[JobResult] = field(default_factory=list)
    differs: list[JobResult] = field(default_factory=list)

    @property
    def jobs(self) -> int:
        return len(self.same) + len(self.differs)

    @property
    def bridge_jobs(self) -> int:
        return sum(1 for r in self.same + self.differs if r.via_bridge)


class _RowsClient:
    """Just enough PostgREST chaining for `rendered_estimate` to run over rows
    that were already fetched, so the report exercises the real rule rather
    than a second spelling of it."""

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
    """Compare the resolver's pick with `is_default` for every job in `rows`.

    `estimate_rows` are `estimator.projects` rows carrying `ESTIMATE_COLUMNS`.
    `job_codes` maps `mc_project_id` -> `projects.code` for the printout; a job
    with no mapping is reported by its id. A job is "live" here if it carries
    at least one estimate row — that is the population the 2026-09-17
    verification counted (45).
    """
    rows = list(estimate_rows)
    job_codes = job_codes or {}
    by_job: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_job[r["mc_project_id"]].append(r)

    client = _RowsClient(rows)
    report = Report()
    for mc_project_id in sorted(by_job):
        job_rows = by_job[mc_project_id]
        picked = rendered_estimate(client, mc_project_id)
        defaults = [r for r in job_rows if r.get("is_default")]
        result = JobResult(
            code=job_codes.get(mc_project_id, mc_project_id),
            mc_project_id=mc_project_id,
            rendered_id=picked["id"] if picked else None,
            rendered_name=picked.get("name") if picked else None,
            default_ids=tuple(r["id"] for r in defaults),
            default_names=tuple(r.get("name") for r in defaults),
            via_bridge=not any(r.get("status") == "approved" for r in job_rows),
        )
        (report.same if result.same else report.differs).append(result)
    return report


def render(report: Report) -> str:
    lines = [
        f"jobs with estimates: {report.jobs}",
        f"same:    {len(report.same)}",
        f"differs: {len(report.differs)}",
        f"resolving through the on_schedule bridge (no approved estimate): "
        f"{report.bridge_jobs}",
    ]
    if report.differs:
        lines.append("")
        lines.append("differing jobs (code: rendered -> is_default):")
        for r in report.differs:
            rendered = (
                f"{r.rendered_name!r} [{r.rendered_id}]" if r.rendered_id else "None"
            )
            defaults = (
                ", ".join(f"{n!r} [{i}]" for n, i in zip(r.default_names, r.default_ids))
                or "None"
            )
            lines.append(f"  {r.code}: {rendered} -> {defaults}")
    return "\n".join(lines)


# ── The live half: two explicit-column SELECTs, no writes ─────────────


def _queries() -> list[str]:
    return [
        f"estimator.{Tables.EST_PROJECTS}: SELECT {ESTIMATE_COLUMNS}",
        f"public.{Tables.PROJECTS}: SELECT {JOB_COLUMNS} WHERE id IN (<job ids above>)",
    ]


def fetch(client) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Every estimate row plus a code map for the jobs they belong to."""
    try:
        estimates = (
            client.schema("estimator")
            .table(Tables.EST_PROJECTS)
            .select(ESTIMATE_COLUMNS)
            .execute()
            .data
            or []
        )
    except Exception as exc:  # noqa: BLE001 — the one error we translate
        msg = str(exc)
        if "is_default" in msg and ("42703" in msg or "does not exist" in msg):
            print(
                "estimator.projects.is_default is gone — migration 183 has landed, "
                "so there is no flag left to reconcile against.",
                file=sys.stderr,
            )
            raise SystemExit(EXIT_COLUMN_GONE) from None
        raise

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
    report = reconcile(estimates, codes)
    print(render(report))
    return EXIT_DIFFERS if report.differs else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
