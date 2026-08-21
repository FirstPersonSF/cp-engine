"""`load_supabase_creds` resolves through three tiers but only tier 1 reads the
environment. Until #211 it returned tier-2/tier-3 creds without exporting them,
so a later `get_client(config=None)` caller re-entered the tier-1-only branch,
found a bare environment, and raised "no tenant config available, so the
1Password and mc-2/backend/.env fallbacks don't apply" — moments after those
fallbacks had succeeded. Every `cp sync` / `cp render` printed it, followed by
"some content may not have been written", which named nothing.

The fix mirrors `_load_ingest_creds` / `load_dropbox_creds`: publish resolved
creds into `os.environ`, an already-set var winning.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cp_engine.mc2_db import load_supabase_creds
from cp_engine.sync import BackendUnavailable


@dataclass
class _FakeConfig:
    root: Path
    local_repos: dict = field(default_factory=dict)


def _write_env(tmp_path: Path, body: str) -> _FakeConfig:
    backend = tmp_path / "mc-2" / "backend"
    backend.mkdir(parents=True)
    (backend / ".env").write_text(body)
    return _FakeConfig(root=tmp_path, local_repos={"mc-2": str(tmp_path / "mc-2")})


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)


def test_dotenv_creds_are_exported_to_environ(tmp_path):
    config = _write_env(
        tmp_path, "SUPABASE_URL=https://from-file\nSUPABASE_SERVICE_KEY=svc-file\n"
    )

    url, key = load_supabase_creds(config)

    assert (url, key) == ("https://from-file", "svc-file")
    # The regression: without the export these were unset.
    assert os.environ["SUPABASE_URL"] == "https://from-file"
    assert os.environ["SUPABASE_SERVICE_KEY"] == "svc-file"


def test_a_later_configless_call_now_succeeds(tmp_path):
    """The actual #211 sequence: tenant-aware resolve, then a fail-soft caller
    arriving with config=None. It used to raise; it must now resolve."""
    config = _write_env(
        tmp_path, "SUPABASE_URL=https://from-file\nSUPABASE_SERVICE_KEY=svc-file\n"
    )
    load_supabase_creds(config)

    url, key = load_supabase_creds(None)

    assert (url, key) == ("https://from-file", "svc-file")


def test_export_does_not_clobber_an_explicit_export(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://from-shell")
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "svc-shell")
    config = _write_env(
        tmp_path, "SUPABASE_URL=https://from-file\nSUPABASE_SERVICE_KEY=svc-file\n"
    )

    url, key = load_supabase_creds(config)

    # Tier 1 already satisfied resolution; CI / shell exports still win.
    assert (url, key) == ("https://from-shell", "svc-shell")
    assert os.environ["SUPABASE_URL"] == "https://from-shell"


def test_configless_with_no_creds_still_raises(tmp_path):
    """The fail-soft contract is unchanged: a genuinely credential-less context
    (the webhook) must still fail, so get_client(required=False) degrades."""
    with pytest.raises(BackendUnavailable):
        load_supabase_creds(None)
