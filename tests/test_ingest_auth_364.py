"""#364: a local `cxp ingest-assets` whose Dropbox creds were rejected printed
`failed=0` and exited 0 — the dead source was recorded as a note and never
counted. Also: DROPBOX_* had no 1Password tier (only a `.env` copy that
drifted from the service creds), and `capture-session --no-commit` claimed it
"also committed" files it never committed.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from click.testing import CliRunner

from cp_engine import asset_ingest, asset_ingest_cli, mc2_db
from cp_engine.asset_ingest import IngestRunResult, ProjectFolders, list_files
from cp_engine.cli import main

_DROPBOX_KEYS = mc2_db._DROPBOX_KEYS


class _DeadDropbox:
    """A connector whose every use raises, like a rejected refresh token."""

    def __getattr__(self, name):
        raise RuntimeError("AuthError('invalid_access_token')")


def _folders(**overrides) -> ProjectFolders:
    base = dict(
        project_id="p",
        company_id="c",
        company_kind="client",
        google_drive_folder_id=None,
        mc_dropbox_folder_id="/Clients/IBX/5153",
        enable_dropbox=True,
        enable_google_drive=False,
    )
    base.update(overrides)
    return ProjectFolders(**base)


# ── a dead source is an error, not a note ────────────────────────────


def test_dead_source_note_is_marked_as_an_error():
    refs, notes = list_files(_folders(), dropbox_connector=_DeadDropbox())

    assert refs == []
    assert [n["source"] for n in notes] == ["dropbox"]
    assert notes[0].get("kind") == "error"


def test_config_gap_note_is_not_an_error():
    _, notes = list_files(_folders(mc_dropbox_folder_id=None))

    assert notes and all(n.get("kind") != "error" for n in notes)


def test_run_result_exposes_source_errors():
    run = IngestRunResult(source_notes=[
        {"source": "filter", "note": "allowlist matched 3 of 3"},
        {"source": "dropbox", "note": "RuntimeError: boom", "kind": "error"},
    ])

    assert [e["source"] for e in run.source_errors] == ["dropbox"]


def test_single_project_cli_fails_when_a_source_is_unreachable(monkeypatch):
    dead = IngestRunResult(source_notes=[
        {"source": "dropbox", "note": "AuthenticationError: invalid_access_token",
         "kind": "error"},
    ])
    monkeypatch.setattr(asset_ingest, "ingest_project_assets", lambda *a, **k: dead)

    result = CliRunner().invoke(main, ["ingest-assets", "ibx-5153"])

    assert result.exit_code == 1
    assert "dropbox" in result.output.lower()
    assert "unreachable" in result.output.lower()


def test_fan_out_counts_an_unreachable_source_as_a_failure(monkeypatch):
    dead = IngestRunResult(source_notes=[
        {"source": "dropbox", "note": "AuthenticationError: x", "kind": "error"},
    ])
    monkeypatch.setattr(asset_ingest, "ingest_project_assets", lambda *a, **k: dead)

    result = asset_ingest_cli.fan_out_ingest(client=object(), codes=["ibx-5153"])

    assert result.any_failures
    assert result.outcomes[0].source_errors


# ── DROPBOX_* from 1Password ─────────────────────────────────────────


@dataclass
class _Config:
    root: Path
    local_repos: dict = field(default_factory=dict)


def _clear_env(monkeypatch):
    for key in _DROPBOX_KEYS:
        monkeypatch.delenv(key, raising=False)


def _tenant_with_stale_dotenv(tmp_path: Path, local_toml: str) -> _Config:
    backend = tmp_path / "mc-2" / "backend"
    backend.mkdir(parents=True)
    (backend / ".env").write_text(
        "DROPBOX_APP_KEY=stale-key\n"
        "DROPBOX_APP_SECRET=stale-secret\n"
        "DROPBOX_REFRESH_TOKEN=stale-refresh\n"
    )
    (tmp_path / ".cp-engine.local.toml").write_text(local_toml)
    return _Config(root=tmp_path, local_repos={"mc-2": str(tmp_path / "mc-2")})


_OP_BLOCK = (
    "[dropbox]\n"
    'app_key_ref = "op://v/Dropbox/app_key"\n'
    'app_secret_ref = "op://v/Dropbox/app_secret"\n'
    'refresh_token_ref = "op://v/Dropbox/refresh_token"\n'
)


def test_dropbox_op_refs_win_over_the_dotenv_copy(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    config = _tenant_with_stale_dotenv(tmp_path, _OP_BLOCK)
    monkeypatch.setattr(mc2_db, "_op_read", lambda ref: "op-" + ref.rsplit("/", 1)[1])

    source = mc2_db.load_dropbox_creds(config)

    assert os.environ["DROPBOX_REFRESH_TOKEN"] == "op-refresh_token"
    assert os.environ["DROPBOX_APP_KEY"] == "op-app_key"
    assert source and "1Password" in source


def test_no_op_block_keeps_the_dotenv_tier(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    config = _tenant_with_stale_dotenv(tmp_path, "")

    source = mc2_db.load_dropbox_creds(config)

    assert os.environ["DROPBOX_REFRESH_TOKEN"] == "stale-refresh"
    assert source and ".env" in source


def test_env_still_wins_over_op_refs(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("DROPBOX_REFRESH_TOKEN", "from-env")
    monkeypatch.setenv("DROPBOX_APP_KEY", "k")
    monkeypatch.setenv("DROPBOX_APP_SECRET", "s")
    config = _tenant_with_stale_dotenv(tmp_path, _OP_BLOCK)

    def _never(ref):
        raise AssertionError("op must not be called when env has the creds")

    monkeypatch.setattr(mc2_db, "_op_read", _never)

    mc2_db.load_dropbox_creds(config)

    assert os.environ["DROPBOX_REFRESH_TOKEN"] == "from-env"


def test_a_failing_op_ref_raises_rather_than_using_the_stale_copy(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    config = _tenant_with_stale_dotenv(tmp_path, _OP_BLOCK)

    def _locked(ref):
        raise RuntimeError("1Password locked")

    monkeypatch.setattr(mc2_db, "_op_read", _locked)

    try:
        mc2_db.load_dropbox_creds(config)
    except Exception as exc:  # noqa: BLE001
        assert "1Password" in str(exc)
    else:
        raise AssertionError("a configured op ref that fails must raise")
    assert os.environ.get("DROPBOX_REFRESH_TOKEN") != "stale-refresh"


def test_dead_dropbox_note_names_where_the_creds_came_from(monkeypatch):
    monkeypatch.setattr(asset_ingest, "_dropbox_creds_source",
                        "mc-2/backend/.env", raising=False)

    _, notes = list_files(_folders(), dropbox_connector=_DeadDropbox())

    assert "mc-2/backend/.env" in notes[0]["note"]


# ── capture-session --no-commit says nothing was committed ───────────


def test_no_commit_does_not_claim_extra_files_were_committed(tmp_path, monkeypatch):
    cfg = tmp_path / "gitconfig"
    cfg.write_text("[user]\n\tname = Someone\n\temail = s@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "cp"
    root.mkdir()
    (root / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n[sync.mc_2]\nsupabase_project_ref = "stub"\n'
    )
    wd = root / "1p" / "google" / "ggl-5168-activation"
    wd.mkdir(parents=True)
    (wd / "cp.md").write_text("# Activation\n\n**Last session:** _<date>_\n")
    (wd / "notes-v01.md").write_text("draft\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    summary = tmp_path / "summary.md"
    summary.write_text("## Session: 2026-10-26 10:00, Someone\n\nThings.\n")
    monkeypatch.chdir(root)

    result = CliRunner().invoke(main, [
        "capture-session", "ggl-5168", "--summary-file", str(summary), "--no-commit",
    ])

    assert result.exit_code == 0, result.output
    assert "Also committed" not in result.output


# ── a run that finds files loads the pipeline's OpenAI/Voyage keys ────


def test_real_files_load_ingest_creds_before_building_the_pipeline(monkeypatch, tmp_path):
    # Found 10-26: once Dropbox listed for real, the run died building the
    # pipeline with "OpenAIError: Missing credentials" — the ingest path
    # loaded SUPABASE_* but never OPENAI/VOYAGE (only the promote path did).
    from cp_engine.asset_ingest import FileRef, ingest_project_assets

    folders = _folders()
    ref = FileRef(source="dropbox", id="id:1", name="brief.md", path="/x/brief.md",
                  mime_type="text/markdown", size=10, modified="2026-10-26")
    monkeypatch.setattr(asset_ingest, "resolve_project_folders", lambda c, code: folders)
    monkeypatch.setattr(asset_ingest, "list_files",
                        lambda *a, **k: ([ref], []))
    monkeypatch.setattr(mc2_db, "load_supabase_creds", lambda cfg=None: ("u", "k"))
    monkeypatch.setattr("cp_engine.config.load", lambda root: object())
    calls: list[str] = []
    monkeypatch.setattr(mc2_db, "load_ingest_creds", lambda cfg: calls.append("ingest"))
    monkeypatch.setattr(asset_ingest, "_configure_pipeline_once", lambda: None)

    class _Stop(Exception):
        pass

    def _factory(*a, **k):
        calls.append("factory")
        raise _Stop

    try:
        ingest_project_assets("ibx-5153", client=object(), pipeline_factory=_factory,
                              tmp_root=tmp_path)
    except _Stop:
        pass

    assert calls[:2] == ["ingest", "factory"]


# ── OPENAI/VOYAGE from 1Password ──────────────────────────────────────

_INGEST_OP_BLOCK = (
    "[ingest]\n"
    'openai_api_key_ref = "op://v/OpenAI/cp_engine"\n'
    'voyage_api_key_ref = "op://v/Voyage/cp_engine"\n'
)


def _tenant_with_stale_ingest_dotenv(tmp_path: Path, local_toml: str) -> _Config:
    backend = tmp_path / "mc-2" / "backend"
    backend.mkdir(parents=True)
    (backend / ".env").write_text("OPENAI_API_KEY=stale-openai\nVOYAGE_API_KEY=stale-voyage\n")
    (tmp_path / ".cp-engine.local.toml").write_text(local_toml)
    return _Config(root=tmp_path, local_repos={"mc-2": str(tmp_path / "mc-2")})


def test_ingest_op_refs_win_over_the_dotenv_copy(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    config = _tenant_with_stale_ingest_dotenv(tmp_path, _INGEST_OP_BLOCK)
    monkeypatch.setattr(mc2_db, "_op_read", lambda ref: "op-" + ref.split("/")[3])

    mc2_db.load_ingest_creds(config)

    assert os.environ["VOYAGE_API_KEY"] == "op-Voyage"
    assert os.environ["OPENAI_API_KEY"] == "op-OpenAI"


def test_ingest_env_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai")
    monkeypatch.setenv("VOYAGE_API_KEY", "env-voyage")
    config = _tenant_with_stale_ingest_dotenv(tmp_path, _INGEST_OP_BLOCK)
    monkeypatch.setattr(mc2_db, "_op_read",
                        lambda ref: (_ for _ in ()).throw(AssertionError("no op")))

    mc2_db.load_ingest_creds(config)

    assert os.environ["VOYAGE_API_KEY"] == "env-voyage"


def test_ingest_without_op_block_keeps_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    config = _tenant_with_stale_ingest_dotenv(tmp_path, "")

    mc2_db.load_ingest_creds(config)

    assert os.environ["VOYAGE_API_KEY"] == "stale-voyage"


# ── Drive from 1Password, loaded before every ingest connector ────────


def test_drive_op_ref_fills_the_service_account_json(tmp_path, monkeypatch):
    for k in mc2_db.DRIVE_CRED_KEYS:
        monkeypatch.delenv(k, raising=False)
    backend = tmp_path / "mc-2" / "backend"
    backend.mkdir(parents=True)
    (backend / ".env").write_text('GOOGLE_SERVICE_ACCOUNT_JSON={"stale": true}\n')
    (tmp_path / ".cp-engine.local.toml").write_text(
        '[drive]\nservice_account_json_ref = "op://v/Google/cp_engine_json"\n'
    )
    config = _Config(root=tmp_path, local_repos={"mc-2": str(tmp_path / "mc-2")})
    monkeypatch.setattr(mc2_db, "_op_read", lambda ref: '{"type": "service_account"}')

    source = mc2_db.load_drive_creds(config)

    assert os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] == '{"type": "service_account"}'
    assert source and "1Password" in source


def test_list_files_builds_drive_through_the_creds_loading_helper(monkeypatch):
    built: list[str] = []

    class _OkDrive:
        def _list_files_with_pagination(self, *a, **k):
            return []

    def _helper():
        built.append("drive")
        return _OkDrive()

    monkeypatch.setattr(asset_ingest, "_drive_connector", _helper, raising=False)

    _, notes = list_files(_folders(enable_google_drive=True, enable_dropbox=False,
                                   google_drive_folder_id="drive-1"))

    assert built == ["drive"]
    assert not [n for n in notes if n.get("kind") == "error"]
