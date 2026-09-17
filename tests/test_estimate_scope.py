"""Which estimate counts, after `is_default` is dropped (#284).

THE ORDERING CONSTRAINT THIS PROTECTS. mc-2 migration 183 drops
`estimator.projects.is_default`. PostgREST answers a filter or select on a
missing column with a 42703 ERROR, not an empty set — and `sync.py` catches
that exception, logs it, and carries on. So the failure mode is silent and
total: every project's spine substance mirrors UNBOUND while the sync still
reports success, and `/cp-prep` quietly loses its estimate figures.

mc-2 is holding the migration until a cp-engine release carrying this ships
and the team is installed on it.

THE BRIDGE IS LOAD-BEARING. Measured against production 2026-09-17: 52
estimate rows across 45 jobs, **zero approved**, and `on_schedule` partitions
them exactly as `is_default` did (45 True / 7 False). An approved-only rule
would empty every spine the day it shipped.
"""

from __future__ import annotations

import ast
from pathlib import Path

from cp_engine.estimate_scope import rendered_estimate, rendered_estimates


class _Q:
    """Enough PostgREST chaining to capture the query and return rows."""

    def __init__(self, rows, seen):
        self._rows, self._seen = rows, seen

    def select(self, cols):
        self._seen["columns"] = cols
        return self

    def eq(self, col, val):
        self._seen.setdefault("filters", []).append((col, val))
        return self

    def order(self, col, **kw):
        self._seen["order"] = col
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


class _Client:
    def __init__(self, rows):
        self.rows, self.seen = rows, {}

    def schema(self, name):
        self.seen["schema"] = name
        return self

    def table(self, name):
        self.seen["table"] = name
        return _Q(self.rows, self.seen)


def _row(**kw):
    base = {
        "id": "e1", "name": "Estimate", "status": "pending",
        "on_schedule": True, "created_at": "2026-01-01T00:00:00Z",
    }
    base.update(kw)
    return base


# ── The rule ──────────────────────────────────────────────────────────


def test_approved_wins_when_the_job_has_any() -> None:
    c = _Client([
        _row(id="old", status="pending", on_schedule=True),
        _row(id="sold", status="approved", on_schedule=False),
    ])
    assert [e["id"] for e in rendered_estimates(c, "j1")] == ["sold"]


def test_several_approved_all_count_and_stay_in_order() -> None:
    """A job may carry several sold estimates that SUM — that is why
    `is_default` was dropped rather than renamed."""
    c = _Client([
        _row(id="a", status="approved", created_at="2026-01-01T00:00:00Z"),
        _row(id="b", status="approved", created_at="2026-02-01T00:00:00Z"),
    ])
    assert [e["id"] for e in rendered_estimates(c, "j1")] == ["a", "b"]


def test_the_bridge_carries_jobs_with_nothing_approved() -> None:
    """THE PRODUCTION CASE TODAY. Zero estimates are approved, so this branch
    is the one every one of the 45 live jobs takes."""
    c = _Client([
        _row(id="on", status="pending", on_schedule=True),
        _row(id="off", status="pending", on_schedule=False),
    ])
    assert [e["id"] for e in rendered_estimates(c, "j1")] == ["on"]


def test_abandoned_counts_nowhere() -> None:
    """Not money, not schedule, not the spine — even when on_schedule is set."""
    c = _Client([_row(id="dead", status="abandoned", on_schedule=True)])
    assert rendered_estimates(c, "j1") == []
    assert rendered_estimate(c, "j1") is None


def test_a_job_with_no_estimates_resolves_to_none() -> None:
    """The no-estimate-yet case the binder reads as 'nothing to bind to'."""
    assert rendered_estimate(_Client([]), "j1") is None


def test_single_estimate_callers_get_the_oldest_approved() -> None:
    c = _Client([
        _row(id="first", status="approved", created_at="2026-01-01T00:00:00Z"),
        _row(id="second", status="approved", created_at="2026-02-01T00:00:00Z"),
    ])
    assert rendered_estimate(c, "j1")["id"] == "first"


# ── The migration-safety control ──────────────────────────────────────


def test_the_query_never_mentions_the_dropped_column() -> None:
    """THE CONTROL. `is_default` must not appear in the filter OR the columns.

    A select on a dropped column fails exactly like a filter on one, and both
    land in the same silent `except` in sync.py.
    """
    c = _Client([_row()])
    rendered_estimates(c, "j1")
    assert "is_default" not in c.seen["columns"], (
        "estimate_scope selects a column migration 183 drops"
    )
    assert all(col != "is_default" for col, _ in c.seen["filters"]), (
        "estimate_scope filters on a column migration 183 drops"
    )
    assert c.seen["schema"] == "estimator"
    assert c.seen["order"] == "created_at", "callers rely on oldest-first"


def test_no_module_in_the_package_still_reads_is_default() -> None:
    """THE REAL CONTROL — the whole package, not just this module.

    Three call sites read `is_default`; a fix that repointed two of them would
    leave the third failing silently after the migration, and the log line it
    produces is one the tenant ALREADY sees for an unrelated reason (#240), so
    nobody would read it as new.
    """
    src_root = Path(__file__).resolve().parent.parent / "src" / "cp_engine"
    offenders = []
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
            and isinstance(n.body[0].value.value, str)
        }
        # Only executable code counts. Prose EXPLAINING the dropped column is
        # exactly what this change adds, and flagging it would make the test
        # fight its own fix.
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "is_default" in node.value and id(node) not in docstrings:
                    offenders.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.Attribute) and node.attr == "is_default":
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "these still reference the dropped column `is_default`: "
        f"{offenders} — after mc-2 migration 183 each is a 42703 error that "
        "sync.py swallows, mirroring every spine unbound"
    )


def test_est_project_columns_matches_the_post_migration_shape() -> None:
    """The shared constant is also vendored into the hosted container, so a
    stale spelling here reaches two runtimes."""
    from cp_engine import mc2_db

    assert "is_default" not in mc2_db.EST_PROJECT_COLUMNS
    for col in ("status", "on_schedule"):
        assert col in mc2_db.EST_PROJECT_COLUMNS


def test_it_mirrors_mc2s_rule_rather_than_inventing_one() -> None:
    """cp-engine is a READER of the estimator schema.

    A second opinion about which estimate counts is how two systems come to
    disagree about what a job sold. If mc-2's rule changes, this fails.
    """
    mc2 = Path("/Users/drewf/Documents/Python/mc-2/backend/src/lib/estimate_scope.py")
    if not mc2.exists():  # mc-2 not checked out here — not a cp-engine failure
        import pytest

        pytest.skip("mc-2 clone not present")
    tree = ast.parse(mc2.read_text(encoding="utf-8"))
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "rendered_estimates"
    )
    body = ast.unparse(fn)
    assert "'approved'" in body and "on_schedule" in body and "'abandoned'" in body, (
        "mc-2's rendered_estimates no longer reads the way cp_engine."
        "estimate_scope mirrors it — reconcile before shipping"
    )


def test_the_resolver_selects_every_column_its_callers_require() -> None:
    """THE CONTROL FOR THE BUG THIS FIX INTRODUCED.

    `_SCOPE_COLUMNS` started as "what the RULE reads" — id, name, status,
    on_schedule, created_at. Every unit test passed, because the fake rows
    carried `mc_project_id` whether the query asked for it or not. Against
    live data all 45 jobs raised:

        ValueError: estimator project row missing required key 'mc_project_id'

    `fetch_estimate` builds its Estimate from the row this returns, so the
    resolver's column list is part of its contract with callers — not an
    internal detail. This asserts against the KEYS THE CALLER REQUIRES, read
    out of `estimate.py` rather than restated here, so a new required field
    fails this instead of production.
    """
    from cp_engine import estimate_scope

    est_src = (
        Path(__file__).resolve().parent.parent
        / "src" / "cp_engine" / "estimate.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(est_src)

    required: set[str] = set()
    for node in ast.walk(tree):
        # `_required(project_row, "<key>", "project")`
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_required"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "project_row"
            and isinstance(node.args[1], ast.Constant)
        ):
            required.add(node.args[1].value)

    assert required, "no _required(project_row, ...) calls found — has the shape changed?"
    selected = {c.strip() for c in estimate_scope._SCOPE_COLUMNS.split(",")}
    missing = required - selected
    assert not missing, (
        f"estimate_scope selects {sorted(selected)} but fetch_estimate requires "
        f"{sorted(required)} — missing {sorted(missing)}; this raises on every "
        "job while unit fakes that carry the field keep passing"
    )
