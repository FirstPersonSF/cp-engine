# tests/test_hosted_set_source_status.py — the writer for the trust fields
# mig 164 added and mig 165 made writable.
#
# `status_note` is the field that says whether a source can be RELIED ON and
# QUOTED. It had no writer at all: `description` gets filled by the sync
# summariser, but a caveat — draft, embargoed, form-gated, superseded, dated —
# was set by hand or not at all. The failure it prevents is quoting an
# embargoed CEO draft in client work because the title looked innocuous.
#
# Same harness as test_hosted_rename_journal: the decorated verbs are MCP Tool
# objects in some builds; here @mcp_server.tool() returns the plain
# function, so these call it directly.
import importlib.util
import os
from pathlib import Path

import pytest

pytest.importorskip("jwt")
pytest.importorskip("mcp")
pytest.importorskip("supabase")

_SERVER_PATH = (
    Path(__file__).resolve().parents[1] / "prototypes" / "hosted-mcp" / "server.py"
)


@pytest.fixture(scope="module")
def srv():
    os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
    os.environ.setdefault("SUPABASE_ANON_KEY", "anon-key-for-tests")
    spec = importlib.util.spec_from_file_location("hosted_mcp_server", _SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Rpc:
    def __init__(self, recorder, result=1):
        self._rec = recorder
        self._result = result

    def execute(self):
        return type("R", (), {"data": self._result})()


class _Client:
    """Records the rpc call the verb makes."""

    def __init__(self, result=1):
        self.calls: list[tuple[str, dict]] = []
        self._result = result

    def rpc(self, name, params):
        self.calls.append((name, params))
        return _Rpc(self.calls, self._result)


def _wire(srv, monkeypatch, client, resolved):
    monkeypatch.setattr(srv, "user_client", lambda: client)
    monkeypatch.setattr(srv, "resolve_project_id", lambda c, code: "proj-1")
    monkeypatch.setattr(
        srv, "_resolve_source_asset",
        lambda c, pid, key, active_only=True: resolved,
    )
    monkeypatch.setattr(srv, "audit", lambda *a, **k: None)
    monkeypatch.setattr(srv, "caller_subject", lambda: "drew@firstperson.is")


def test_writes_the_caveat_through_the_guarded_function(srv, monkeypatch):
    client = _Client()
    _wire(srv, monkeypatch, client, {"id": "a-1", "title": "CEO rebrand draft"})

    out = srv.set_source_status(
        "slt-5196", "CEO rebrand draft",
        status_note="embargoed — never quote externally",
    )

    name, params = client.calls[0]
    # rag_assets has no authenticated UPDATE; the write MUST go through the
    # mig-165 guarded function, not a direct table update.
    assert name == "rag_asset_set_status"
    assert params["p_status_note"] == "embargoed — never quote externally"
    assert out["updated"] is True
    assert out["title"] == "CEO rebrand draft"


def test_an_omitted_field_is_passed_as_none_not_empty(srv, monkeypatch):
    """None leaves a column untouched; '' clears it. Confusing the two would
    silently wipe a generated description when setting only a caveat."""
    client = _Client()
    _wire(srv, monkeypatch, client, {"id": "a-1", "title": "Doc"})

    srv.set_source_status("slt-5196", "Doc", status_note="draft")

    _, params = client.calls[0]
    assert params["p_description"] is None
    assert "description" not in srv.set_source_status(
        "slt-5196", "Doc", status_note="draft"
    )


def test_setting_nothing_is_refused_before_any_call(srv, monkeypatch):
    client = _Client()
    _wire(srv, monkeypatch, client, {"id": "a-1", "title": "Doc"})

    out = srv.set_source_status("slt-5196", "Doc")

    assert "error" in out
    assert client.calls == [], "must not reach the database with nothing to set"


def test_it_resolves_non_active_rows(srv, monkeypatch):
    """An obsoleted stub is exactly the row that most needs a caveat saying
    why it is obsolete — the retire verbs' active-only default is wrong here."""
    client = _Client()
    seen = {}

    def _resolver(c, pid, key, active_only=True):
        seen["active_only"] = active_only
        return {"id": "a-1", "title": "Stale stub"}

    monkeypatch.setattr(srv, "user_client", lambda: client)
    monkeypatch.setattr(srv, "resolve_project_id", lambda c, code: "proj-1")
    monkeypatch.setattr(srv, "_resolve_source_asset", _resolver)
    monkeypatch.setattr(srv, "audit", lambda *a, **k: None)
    monkeypatch.setattr(srv, "caller_subject", lambda: "drew@firstperson.is")

    srv.set_source_status("slt-5196", "Stale stub", status_note="obsolete")

    assert seen["active_only"] is False


def test_an_ambiguous_title_returns_candidates_not_a_write(srv, monkeypatch):
    client = _Client()
    _wire(srv, monkeypatch, client, {"candidates": [{"id": "a"}, {"id": "b"}]})

    out = srv.set_source_status("slt-5196", "Recurring sync", status_note="draft")

    assert "candidates" in out
    assert client.calls == []


def test_a_failed_rpc_surfaces_an_error_and_never_raises(srv, monkeypatch):
    class _Boom(_Client):
        def rpc(self, name, params):
            raise RuntimeError("not a team member")

    client = _Boom()
    _wire(srv, monkeypatch, client, {"id": "a-1", "title": "Doc"})

    out = srv.set_source_status("slt-5196", "Doc", status_note="draft")

    assert "error" in out
    assert "not a team member" in out["error"]
