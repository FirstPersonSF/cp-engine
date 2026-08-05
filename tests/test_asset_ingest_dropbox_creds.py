"""#154: `cp ingest-assets` failed from a bare terminal because the CLI path
constructed `DropboxConnector()` directly and relied on ambient DROPBOX_* env
vars — the .env auto-load exports only SUPABASE_*. `_dropbox_connector` routes
every construction through `mc2_db.load_dropbox_creds` (best-effort, env wins)
so CLI, webhook, and MCP paths behave identically.
"""
from __future__ import annotations

import os
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

from cp_engine import asset_ingest

_DROPBOX_KEYS = (
    "DROPBOX_APP_KEY",
    "DROPBOX_APP_SECRET",
    "DROPBOX_REFRESH_TOKEN",
    "DROPBOX_ACCESS_TOKEN",
)


@dataclass
class _FakeConfig:
    root: Path
    local_repos: dict = field(default_factory=dict)


def _fake_connector_module(monkeypatch, captured: dict):
    class FakeConnector:
        def __init__(self):
            captured["env_at_construction"] = {
                k: os.environ.get(k) for k in _DROPBOX_KEYS
            }

    parent = types.ModuleType("cloud_storage")
    mod = types.ModuleType("cloud_storage.dropbox_connector")
    mod.DropboxConnector = FakeConnector
    parent.dropbox_connector = mod
    monkeypatch.setitem(sys.modules, "cloud_storage", parent)
    monkeypatch.setitem(sys.modules, "cloud_storage.dropbox_connector", mod)
    return FakeConnector


def _clear_dropbox_env(monkeypatch):
    for key in _DROPBOX_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_loads_dropbox_creds_from_dotenv_before_construction(tmp_path, monkeypatch):
    _clear_dropbox_env(monkeypatch)
    captured: dict = {}
    fake_cls = _fake_connector_module(monkeypatch, captured)

    backend = tmp_path / "mc-2" / "backend"
    backend.mkdir(parents=True)
    (backend / ".env").write_text(
        "DROPBOX_APP_KEY=app-key\n"
        "DROPBOX_APP_SECRET=app-secret\n"
        "DROPBOX_REFRESH_TOKEN=refresh-token\n"
    )
    config = _FakeConfig(root=tmp_path, local_repos={"mc-2": str(tmp_path / "mc-2")})
    monkeypatch.setattr("cp_engine.config.load", lambda root: config)
    monkeypatch.setattr(
        "cp_engine.capture_session.find_tenant_root", lambda p: tmp_path
    )

    connector = asset_ingest._dropbox_connector()

    assert isinstance(connector, fake_cls)
    assert captured["env_at_construction"]["DROPBOX_REFRESH_TOKEN"] == "refresh-token"
    assert captured["env_at_construction"]["DROPBOX_APP_KEY"] == "app-key"


def test_loader_failure_still_constructs(tmp_path, monkeypatch):
    # Outside a tenant repo `config.load` raises — the connector must still be
    # constructed so its own "No Dropbox credentials found" surfaces downstream.
    _clear_dropbox_env(monkeypatch)
    captured: dict = {}
    fake_cls = _fake_connector_module(monkeypatch, captured)

    def _boom(root):
        raise RuntimeError("not a tenant repo")

    monkeypatch.setattr("cp_engine.config.load", _boom)
    monkeypatch.setattr(
        "cp_engine.capture_session.find_tenant_root", lambda p: None
    )

    connector = asset_ingest._dropbox_connector()

    assert isinstance(connector, fake_cls)
    assert captured["env_at_construction"]["DROPBOX_REFRESH_TOKEN"] is None
