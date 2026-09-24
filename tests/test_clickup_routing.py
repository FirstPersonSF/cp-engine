"""Tests for cp_engine.clickup_routing — the unified project resolver.

This module replaced two hand-synced copies (ingest._resolve_proposal_project
and webhook/clickup_propose._resolve_project). The tests pin the reconciled
semantics, especially the two points the copies had diverged on.
"""
from unittest.mock import MagicMock

from cp_engine.clickup_routing import engagement_number, resolve_clickup_project


def _client(project_rows=None, binding_rows=None):
    client = MagicMock()

    def table(name):
        t = MagicMock()
        resp = MagicMock()
        if name == "projects":
            resp.data = project_rows or []
            t.select.return_value.eq.return_value.execute.return_value = resp
        elif name == "project_integrations":
            # Read-flip: clickup_list_id resolves from bindings.
            resp.data = binding_rows or []
            t.select.return_value.in_.return_value.execute.return_value = resp
        return t

    client.table.side_effect = table
    return client


def _clickup_binding(owner_col, owner_id, list_id):
    return {
        "project_id": None, "service": "clickup",
        "external_ref": {"id": list_id, "extra": {"list_id": list_id}},
        "label": "",
        owner_col: owner_id,
    }


ROW = {"id": "uuid-1", "enable_clickup": True}
BINDINGS = [_clickup_binding("project_id", "uuid-1", "list-9")]


def test_engagement_code_resolves_as_project_kind():
    result = resolve_clickup_project(
        _client(project_rows=[ROW], binding_rows=BINDINGS), "ggl-5136",
    )
    assert result == {
        "id": "uuid-1", "clickup_list_id": "list-9",
        "code": "ggl-5136", "kind": "project",
    }


def test_internal_workstream_code_resolves_as_project_kind():
    """#301: internal workstreams are numbered `projects` rows; the code
    resolves by number and stamps `kind="project"` like every other."""
    bindings = [_clickup_binding("project_id", "uuid-1", "list-77")]
    result = resolve_clickup_project(
        _client(project_rows=[ROW], binding_rows=bindings), "1pi-9005-mission-control",
    )
    assert result["kind"] == "project"
    assert result["code"] == "1pi-9005-mission-control"
    assert result["clickup_list_id"] == "list-77"


def test_display_name_spelling_resolves_too():
    """Any spelling `parse_code` accepts is one lookup by number."""
    result = resolve_clickup_project(
        _client(project_rows=[ROW], binding_rows=BINDINGS), "GGL 5136 Go Safety",
    )
    assert result is not None and result["kind"] == "project"


def test_no_rows_returns_none():
    assert resolve_clickup_project(_client(), "ggl-9999") is None


def test_bare_slug_is_not_a_code_and_never_queries():
    """A numberless slug (`mission-control`) names nothing since the
    initiatives table retired — no query is issued."""
    client = _client()
    assert resolve_clickup_project(client, "no-such-slug") is None
    client.table.assert_not_called()


def test_engagement_number_is_a_parse_code_wrapper():
    assert engagement_number("ggl-5136") == 5136
    assert engagement_number("ggl-5136-go-safety-website") == 5136
    assert engagement_number("sap-5171-vision-update-2026") == 5171
    assert engagement_number("1pi-9005-mission-control") == 9005
    assert engagement_number("mission-control") is None


def test_enable_clickup_false_returns_none():
    row = {**ROW, "enable_clickup": False}
    assert resolve_clickup_project(_client(project_rows=[row]), "ggl-5136") is None


def test_missing_enable_clickup_default_is_disabled():
    """Webhook semantics: absent key = disabled (the default)."""
    row = {"id": "uuid-1"}
    assert resolve_clickup_project(
        _client(project_rows=[row], binding_rows=BINDINGS), "ggl-5136",
    ) is None


def test_missing_enable_clickup_ok_treats_as_enabled():
    """Ingest semantics: mocks that omit the column still resolve."""
    row = {"id": "uuid-1"}
    result = resolve_clickup_project(
        _client(project_rows=[row], binding_rows=BINDINGS), "ggl-5136",
        missing_enable_clickup_ok=True,
    )
    assert result is not None and result["kind"] == "project"
    assert result["clickup_list_id"] == "list-9"


def test_wrappers_delegate():
    """Both historical entry points resolve through the shared function."""
    from cp_engine.ingest import _resolve_proposal_project

    row = {"id": "uuid-1"}  # no enable_clickup key
    # ingest wrapper: mock-tolerant
    assert _resolve_proposal_project(
        _client(project_rows=[row], binding_rows=BINDINGS), "ggl-5136",
    ) is not None

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
