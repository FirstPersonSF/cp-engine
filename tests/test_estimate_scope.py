"""Which estimate counts, after `is_default` is dropped (#284, #292).

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
would empty every spine the day it shipped. That measurement is repeatable:
`scripts/estimate_scope_reconcile.py` (tested at the bottom of this file).

THE FAKES PROJECT. Every fake here returns only the columns the query
`select()`ed, the way PostgREST does. A read of an unselected key raises in
the test exactly as it would in production — the over-generous fakes that let
#284's `mc_project_id` regression through are the thing this file fixes.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

from cp_engine.estimate_scope import rendered_estimate, rendered_estimates

_REPO = Path(__file__).resolve().parent.parent


def _project(row: dict, columns: str) -> dict:
    """What PostgREST hands back for `select(columns)`: those keys, no others."""
    return {k: row[k] for k in (c.strip() for c in columns.split(",")) if k}


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
        data = [_project(r, self._seen["columns"]) for r in self._rows]
        return type("R", (), {"data": data})()


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
        "id": "e1", "mc_project_id": "j1", "name": "Estimate", "status": "pending",
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

    `scripts/estimate_scope_reconcile.py` is the one deliberate exception and
    lives outside this walk: comparing against the flag is its whole job.
    """
    src_root = _REPO / "src" / "cp_engine"
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


# ── The mirror ────────────────────────────────────────────────────────

# mc-2's rule, vendored verbatim so the mirror is checked on EVERY machine and
# in CI — the earlier test hardcoded one laptop's clone path and skipped
# everywhere else. Copied from
#   mc-2:backend/src/lib/estimate_scope.py @ 90fcaca (2026-09-16)
# When mc-2 changes the function, update the text AND the SHA together; the
# live check below is what tells you to.
_MC2_SHA = "90fcaca"
_MC2_PATH = Path("backend/src/lib/estimate_scope.py")
_MC2_CLONE = Path.home() / "Documents" / "Python" / "mc-2"
_MC2_RENDERED_ESTIMATES = '''\
def rendered_estimates(db, job_id: str) -> list[dict[str, Any]]:
    """The estimates a client portal or the spine should render, oldest first.

    Approved estimates when the job has any.

    THE BRIDGE, and it is deliberate rather than a fallback that hides a fault. Status
    exists only from this migration, so on the day it ships NO estimate in production is
    approved and an approved-only rule would empty every portal and every job spine. Until
    a job has an approved estimate, this returns the estimates flagged `on_schedule` —
    which migration 181 seeded from the old `is_default` — so those surfaces show exactly
    what they showed before the rebuild.

    RETIRE IT when live jobs carry approved estimates: delete the second branch, and these
    surfaces follow status alone. The reconciliation report counts jobs still relying on
    the bridge, so the trigger is measurable rather than a matter of opinion.
    """
    rows = _estimates(db, job_id)
    approved = [e for e in rows if e.get("status") == "approved"]
    if approved:
        return approved
    return [e for e in rows if e.get("status") != "abandoned" and e.get("on_schedule")]
'''


def _function_source(src: str, name: str) -> str:
    tree = ast.parse(src)
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == name
    )
    return ast.get_source_segment(src, fn)


def _rule_shape(src: str, name: str) -> str:
    """The function body as a normalised AST dump.

    Docstring dropped; parameters renamed positionally (mc-2 says `db, job_id`,
    cp-engine says `client, mc_project_id` — the same rule under different
    names must compare equal, and a reordered tiebreak must not).
    """
    tree = ast.parse(src)
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == name
    )
    rename = {a.arg: f"_p{i}" for i, a in enumerate(fn.args.args)}

    class _Rename(ast.NodeTransformer):
        def visit_Name(self, node):  # noqa: N802 — NodeTransformer API
            if node.id in rename:
                node.id = rename[node.id]
            return node

    body = fn.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.dump(_Rename().visit(n)) for n in body)


def test_it_mirrors_mc2s_rule_rather_than_inventing_one() -> None:
    """cp-engine is a READER of the estimator schema.

    A second opinion about which estimate counts is how two systems come to
    disagree about what a job sold. The comparison is structural: the whole
    body, not three tokens — a reordered branch or a changed tiebreak fails.
    """
    ours = (_REPO / "src" / "cp_engine" / "estimate_scope.py").read_text(encoding="utf-8")
    assert _rule_shape(ours, "rendered_estimates") == _rule_shape(
        _MC2_RENDERED_ESTIMATES, "rendered_estimates"
    ), (
        f"cp_engine.estimate_scope.rendered_estimates no longer matches mc-2's "
        f"(fixture @ {_MC2_SHA}) — reconcile before shipping"
    )


def test_the_vendored_fixture_is_not_stale() -> None:
    """On a machine with mc-2 checked out, the fixture must match the live
    file byte-for-byte — otherwise the SHA above is a claim about a copy that
    no longer exists. CI has no clone and skips THIS test only; the mirror
    test above still runs there against the fixture."""
    live = _MC2_CLONE / _MC2_PATH
    if not live.exists():
        pytest.skip("mc-2 clone not present — fixture comparison still ran above")
    actual = _function_source(live.read_text(encoding="utf-8"), "rendered_estimates")
    assert actual.rstrip() == _MC2_RENDERED_ESTIMATES.rstrip(), (
        f"mc-2's rendered_estimates has changed since {_MC2_SHA}: update "
        "_MC2_RENDERED_ESTIMATES and _MC2_SHA in this file (git log -1 --format=%h "
        f"-- {_MC2_PATH} in mc-2), then check the mirror test still passes"
    )


# ── The column contract ───────────────────────────────────────────────


def _project_row_reads(src: str) -> set[str]:
    """Every key `estimate.py` reads off `project_row`, in all three shapes:
    `_required(project_row, "k")`, `project_row.get("k", ...)`, `project_row["k"]`.

    The first version scanned only `_required`; `.get("name", default)` and
    `["id"]` went unscanned, so a dropped `name` would render the default
    name in production while both tests passed.
    """
    tree = ast.parse(src)
    keys: set[str] = set()

    def _is_project_row(node) -> bool:
        return isinstance(node, ast.Name) and node.id == "project_row"

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_required"
            and len(node.args) >= 2
            and _is_project_row(node.args[0])
            and isinstance(node.args[1], ast.Constant)
        ):
            keys.add(node.args[1].value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and _is_project_row(node.func.value)
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            keys.add(node.args[0].value)
        elif (
            isinstance(node, ast.Subscript)
            and _is_project_row(node.value)
            and isinstance(node.slice, ast.Constant)
        ):
            keys.add(node.slice.value)
    return keys


def test_the_resolver_selects_every_column_its_callers_require() -> None:
    """THE CONTROL FOR THE BUG THIS FIX INTRODUCED.

    `_SCOPE_COLUMNS` started as "what the RULE reads" — id, name, status,
    on_schedule, created_at. Every unit test passed, because the fake rows
    carried `mc_project_id` whether the query asked for it or not. Against
    live data all 45 jobs raised:

        ValueError: estimator project row missing required key 'mc_project_id'

    `fetch_estimate` builds its Estimate from the row this returns, so the
    resolver's column list is part of its contract with callers — not an
    internal detail. This asserts against the KEYS THE CALLER READS, taken
    out of `estimate.py` rather than restated here, so a new field fails this
    instead of production.
    """
    from cp_engine import estimate_scope

    est_src = (_REPO / "src" / "cp_engine" / "estimate.py").read_text(encoding="utf-8")
    required = _project_row_reads(est_src)

    assert {"id", "mc_project_id", "name"} <= required, (
        f"scan found {sorted(required)} — has estimate.py's read shape changed?"
    )
    selected = {c.strip() for c in estimate_scope._SCOPE_COLUMNS.split(",")}
    missing = required - selected
    assert not missing, (
        f"estimate_scope selects {sorted(selected)} but fetch_estimate reads "
        f"{sorted(required)} — missing {sorted(missing)}; this raises on every "
        "job while unit fakes that carry the field keep passing"
    )


# ── The reconciliation script ─────────────────────────────────────────

_SCRIPT = _REPO / "scripts" / "estimate_scope_reconcile.py"


def _load_reconcile():
    spec = importlib.util.spec_from_file_location("estimate_scope_reconcile", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["estimate_scope_reconcile"] = mod
    spec.loader.exec_module(mod)
    return mod


def _est(job, id, *, name="Estimate 1", status="pending", on_schedule=True,
         is_default=False, created_at="2026-01-01T00:00:00Z"):
    return {
        "id": id, "mc_project_id": job, "name": name, "status": status,
        "on_schedule": on_schedule, "created_at": created_at,
        "is_default": is_default,
    }


def test_reconcile_counts_same_and_differs_and_names_the_differing_jobs() -> None:
    """The 45-job verification, as a function over rows instead of a docstring."""
    rec = _load_reconcile()
    rows = [
        # Bridge job: on_schedule == is_default → same.
        _est("j1", "e1", is_default=True),
        _est("j1", "e1-draft", name="Draft", on_schedule=False),
        # Approved job: the approved row is also the default → same.
        _est("j2", "e2", status="approved", is_default=True),
        # Bridge job where the flags disagree → differs, and the report
        # carries both names so a human can see which row each side picked.
        _est("j3", "e3-on", name="Scoped", on_schedule=True),
        _est("j3", "e3-def", name="Original", on_schedule=False, is_default=True),
        # No default flagged and nothing on schedule → both None → same.
        _est("j4", "e4", on_schedule=False),
    ]
    report = rec.reconcile(rows, {"j1": "ggl-1", "j2": "ibx-2", "j3": "sap-3"})

    assert report.jobs == 4
    assert [r.code for r in report.same] == ["ggl-1", "ibx-2", "j4"]
    assert [r.code for r in report.differs] == ["sap-3"]
    assert report.bridge_jobs == 3  # j2 is the only job with an approved row

    d = report.differs[0]
    assert (d.rendered_id, d.rendered_name) == ("e3-on", "Scoped")
    assert (d.default_ids, d.default_names) == (("e3-def",), ("Original",))

    out = rec.render(report)
    assert "same:    3" in out and "differs: 1" in out
    assert "sap-3: 'Scoped' [e3-on] -> 'Original' [e3-def]" in out


def test_reconcile_compares_ids_not_names() -> None:
    """Two estimates called "Estimate 1" on one job: a name match would hide
    the resolver picking the wrong row."""
    rec = _load_reconcile()
    rows = [
        _est("j1", "a", on_schedule=True, is_default=False),
        _est("j1", "b", on_schedule=False, is_default=True),
    ]
    report = rec.reconcile(rows)
    assert [r.mc_project_id for r in report.differs] == ["j1"]


def test_reconcile_runs_the_real_rule_not_a_restatement() -> None:
    """Abandoned-but-on_schedule is the case a hand-rolled comparison would
    get wrong; the script must resolve it exactly as `rendered_estimate` does."""
    rec = _load_reconcile()
    rows = [_est("j1", "dead", status="abandoned", on_schedule=True, is_default=True)]
    report = rec.reconcile(rows)
    assert report.differs and report.differs[0].rendered_id is None


def test_reconcile_selects_the_resolvers_columns_plus_the_flag() -> None:
    """The script must read what the resolver reads (or the real rule cannot
    run over its rows) plus `is_default` — and nothing under `src/` may."""
    from cp_engine.estimate_scope import _SCOPE_COLUMNS

    rec = _load_reconcile()
    cols = {c.strip() for c in rec.ESTIMATE_COLUMNS.split(",")}
    assert cols == {c.strip() for c in _SCOPE_COLUMNS.split(",")} | {"is_default"}
    assert "*" not in rec.ESTIMATE_COLUMNS and "*" not in rec.JOB_COLUMNS


def test_reconcile_dry_run_touches_no_client(capsys) -> None:
    rec = _load_reconcile()
    assert rec.main(["--dry-run"]) == rec.EXIT_OK
    out = capsys.readouterr().out
    assert "is_default" in out and "would run" in out
