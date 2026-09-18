"""A missing estimator COLUMN must fail the sync, not mirror every project
unbound behind a warning (#291).

#284's failure mode: mc-2 migration 183 drops `is_default`; PostgREST answers
a filter on a missing column with 42703; `sync.py` caught it per project and
carried on. Forty-five identical warnings, exit 0, every spine unbound. The
per-project swallow is right for a per-project condition and wrong for a
schema move, which is the same for every project.
"""

from types import SimpleNamespace

import pytest

from cp_engine import sync
from cp_engine.sync import EstimateSchemaDrift, SyncError, _fetch_estimate_or_none


class _APIError(Exception):
    """Shape of `postgrest.exceptions.APIError`: a `.code` and a message."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


_PROJECT = SimpleNamespace(code="ggl-5168", mc2_id="p1")


def test_a_missing_column_raises_through_the_best_effort_swallow(monkeypatch):
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate", lambda c, pid: (_ for _ in ()).throw(
        _APIError("42703", "column estimator.projects.is_default does not exist")))
    with pytest.raises(EstimateSchemaDrift) as info:
        _fetch_estimate_or_none(None, _PROJECT)
    assert "ggl-5168" in str(info.value)
    assert "is_default" in str(info.value)
    assert isinstance(info.value, SyncError), (
        "must be a SyncError so `cxp sync` prints 'Sync failed' and exits 1"
    )


def test_a_message_only_column_error_is_still_drift(monkeypatch):
    """An older client surfaces the SQLSTATE only in the text."""
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate", lambda c, pid: (_ for _ in ()).throw(
        RuntimeError("column projects.on_schedule does not exist")))
    with pytest.raises(EstimateSchemaDrift):
        _fetch_estimate_or_none(None, _PROJECT)


def test_a_per_project_failure_still_degrades_to_unbound(monkeypatch, caplog):
    """The swallow stays for what it was built for: an unreachable estimator,
    a timeout, a project with no estimate."""
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate", lambda c, pid: (_ for _ in ()).throw(
        ConnectionError("estimator unreachable")))
    with caplog.at_level("WARNING"):
        assert _fetch_estimate_or_none(None, _PROJECT) is None
    assert "estimate fetch failed for ggl-5168" in caplog.text


def test_no_estimate_is_not_an_error(monkeypatch):
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate", lambda c, pid: None)
    assert _fetch_estimate_or_none(None, _PROJECT) is None


def test_the_substance_mirror_reraises_drift_rather_than_skipping():
    """The helper raises INSIDE a second best-effort `except Exception` (the
    substance-mirror one). That handler must let a SyncError through, or the
    drift is swallowed one layer up and this whole fix is a no-op."""
    import ast
    import inspect

    src = inspect.getsource(sync._sync_tenant_inner)
    tree = ast.parse(src)
    reraises = [
        h for h in ast.walk(tree)
        if isinstance(h, ast.ExceptHandler)
        and any(isinstance(n, ast.Raise) and n.exc is None for n in ast.walk(h))
        and "SyncError" in ast.unparse(h)
    ]
    assert reraises, "no best-effort handler in _sync_tenant_inner re-raises SyncError"
