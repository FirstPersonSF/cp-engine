"""Tests for cp_engine.clickup_routing — the unified project resolver.

This module replaced two hand-synced copies (ingest._resolve_proposal_project
and webhook/clickup_propose._resolve_project). The tests pin the reconciled
semantics, especially the two points the copies had diverged on.
"""
from unittest.mock import MagicMock

import pytest
from postgrest.exceptions import APIError

from cp_engine.clickup_routing import resolve_clickup_project


def _client(project_rows=None, initiative_rows=None, initiative_exc=None):
    client = MagicMock()

    def table(name):
        t = MagicMock()
        resp = MagicMock()
        if name == "projects":
            resp.data = project_rows or []
            t.select.return_value.eq.return_value.execute.return_value = resp
        elif name == "initiatives":
            if initiative_exc is not None:
                t.select.return_value.eq.return_value.execute.side_effect = initiative_exc
            else:
                resp.data = initiative_rows or []
                t.select.return_value.eq.return_value.execute.return_value = resp
        return t

    client.table.side_effect = table
    return client


ROW = {"id": "uuid-1", "clickup_list_id": "list-9", "enable_clickup": True}


def test_engagement_code_resolves_as_project_kind():
    result = resolve_clickup_project(_client(project_rows=[ROW]), "ggl-5136")
    assert result == {
        "id": "uuid-1", "clickup_list_id": "list-9",
        "code": "ggl-5136", "kind": "project",
    }


def test_initiative_slug_resolves_as_initiative_kind():
    row = {**ROW, "code": "mission-control"}
    result = resolve_clickup_project(_client(initiative_rows=[row]), "mission-control")
    assert result["kind"] == "initiative"
    assert result["code"] == "mission-control"


def test_no_rows_returns_none():
    assert resolve_clickup_project(_client(), "ggl-9999") is None
    assert resolve_clickup_project(_client(), "no-such-slug") is None


def test_enable_clickup_false_returns_none():
    row = {**ROW, "enable_clickup": False}
    assert resolve_clickup_project(_client(project_rows=[row]), "ggl-5136") is None


def test_missing_enable_clickup_default_is_disabled():
    """Webhook semantics: absent key = disabled (the default)."""
    row = {"id": "uuid-1", "clickup_list_id": "list-9"}
    assert resolve_clickup_project(_client(project_rows=[row]), "ggl-5136") is None


def test_missing_enable_clickup_ok_treats_as_enabled():
    """Ingest semantics: mocks that omit the column still resolve."""
    row = {"id": "uuid-1", "clickup_list_id": "list-9"}
    result = resolve_clickup_project(
        _client(project_rows=[row]), "ggl-5136", missing_enable_clickup_ok=True,
    )
    assert result is not None and result["kind"] == "project"


def test_initiative_apierror_swallowed():
    """Missing ClickUp columns on initiatives -> None, not a crash."""
    exc = APIError({"message": "column initiatives.enable_clickup does not exist"})
    assert resolve_clickup_project(
        _client(initiative_exc=exc), "mission-control",
    ) is None


def test_initiative_other_exceptions_propagate():
    """Reconciled divergence: genuine failures are no longer swallowed."""
    with pytest.raises(ConnectionError):
        resolve_clickup_project(
            _client(initiative_exc=ConnectionError("network down")), "mission-control",
        )


def test_wrappers_delegate():
    """Both historical entry points resolve through the shared function."""
    from cp_engine.ingest import _resolve_proposal_project

    row = {"id": "uuid-1", "clickup_list_id": "list-9"}  # no enable_clickup key
    # ingest wrapper: mock-tolerant
    assert _resolve_proposal_project(_client(project_rows=[row]), "ggl-5136") is not None

    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "clickup_propose", Path(__file__).resolve().parents[1] / "webhook" / "clickup_propose.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # webhook wrapper: absent key = disabled
    assert mod._resolve_project(_client(project_rows=[row]), "ggl-5136") is None
    assert mod._resolve_project(_client(project_rows=[ROW]), "ggl-5136") is not None
