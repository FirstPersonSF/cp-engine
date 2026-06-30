"""The promote/ingest pipeline reads OPENAI_API_KEY / VOYAGE_API_KEY via
`os.getenv`, but nothing loaded them into the environment — `_load_supabase_creds`
deliberately loads only the SUPABASE_* keys. `_load_ingest_creds` fills that gap:
it resolves the ingest keys (env first, then `<mc-2 clone>/backend/.env`) and
exports any found into `os.environ`, so the pipeline's client factory sees them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from cp_engine.sync_mc2 import _load_ingest_creds


@dataclass
class _FakeConfig:
    root: Path
    local_repos: dict = field(default_factory=dict)


def _write_env(tmp_path: Path, body: str) -> _FakeConfig:
    backend = tmp_path / "mc-2" / "backend"
    backend.mkdir(parents=True)
    (backend / ".env").write_text(body)
    return _FakeConfig(root=tmp_path, local_repos={"mc-2": str(tmp_path / "mc-2")})


def test_loads_ingest_keys_from_dotenv_into_env(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    config = _write_env(
        tmp_path,
        "SUPABASE_URL=https://x\nOPENAI_API_KEY=sk-openai\nVOYAGE_API_KEY=vk-voyage\n",
    )

    _load_ingest_creds(config)

    assert os.environ["OPENAI_API_KEY"] == "sk-openai"
    assert os.environ["VOYAGE_API_KEY"] == "vk-voyage"


def test_does_not_clobber_existing_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-shell")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    config = _write_env(
        tmp_path, "OPENAI_API_KEY=sk-from-file\nVOYAGE_API_KEY=vk-file\n"
    )

    _load_ingest_creds(config)

    # An explicit shell/CI export wins over the file.
    assert os.environ["OPENAI_API_KEY"] == "sk-from-shell"
    assert os.environ["VOYAGE_API_KEY"] == "vk-file"


def test_no_clone_configured_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = _FakeConfig(root=tmp_path, local_repos={})

    # No mc-2 clone → nothing to load → must not raise.
    _load_ingest_creds(config)

    assert os.environ.get("OPENAI_API_KEY") is None
