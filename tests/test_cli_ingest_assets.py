"""Tests for the 1P asset-ingest CLI verbs (Task C8).

These monkeypatch the asset_ingest glue (and the client builder) so nothing
touches real MC-2 / Drive / Dropbox. They exercise the CLI surface only:
argument validation, fan-out, summary formatting, exit codes.
"""

from __future__ import annotations

import cp_engine.asset_ingest as asset_ingest
import cp_engine.asset_ingest_cli as asset_ingest_cli
import cp_engine.cli as cli_mod
from click.testing import CliRunner

from cp_engine.asset_ingest import IngestRunResult
from cp_engine.cli import main


def _stub_client(monkeypatch):
    """Make every CLI verb that wants a client get a harmless sentinel."""
    sentinel = object()
    monkeypatch.setattr(cli_mod, "build_mc2_client", lambda: sentinel)
    return sentinel


def _stub_config(monkeypatch):
    """Stub the tenant-config load the --all enumeration now needs.

    The --all branch calls `_load_config_or_die()` to drive the canonical
    `read_projects`-based enumeration. Tests fake the enumeration itself
    (`active_ingestable_codes`), so the config object only needs to be a
    harmless sentinel that never gets touched.
    """
    sentinel = object()
    monkeypatch.setattr(cli_mod, "_load_config_or_die", lambda: sentinel)
    return sentinel


# ──────────────────────────────────────────────────────────────────────
#  ingest-assets — single project
# ──────────────────────────────────────────────────────────────────────


def test_ingest_assets_single_summary(monkeypatch):
    _stub_client(monkeypatch)

    def fake_ingest(code, **kwargs):
        assert code == "ibx-5153"
        return IngestRunResult(created=2, versioned=1, skipped=3, deduped=4, failed=0)

    monkeypatch.setattr(asset_ingest, "ingest_project_assets", fake_ingest)

    result = CliRunner().invoke(main, ["ingest-assets", "ibx-5153"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "created=2" in out
    assert "versioned=1" in out
    assert "skipped=3" in out
    assert "deduped=4" in out
    assert "failed=0" in out


def test_ingest_assets_failures_exit_nonzero(monkeypatch):
    _stub_client(monkeypatch)

    def fake_ingest(code, **kwargs):
        return IngestRunResult(
            created=1,
            failed=2,
            failures=[("deck.pptx", "parse error"), ("notes.txt", "timeout")],
        )

    monkeypatch.setattr(asset_ingest, "ingest_project_assets", fake_ingest)

    result = CliRunner().invoke(main, ["ingest-assets", "ibx-5153"])
    assert result.exit_code != 0
    assert "deck.pptx" in result.output
    assert "parse error" in result.output
    assert "notes.txt" in result.output


def test_ingest_assets_unknown_project_exits_nonzero(monkeypatch):
    _stub_client(monkeypatch)

    def fake_ingest(code, **kwargs):
        # Mirror asset_ingest's "no matching MC-2 project" result: zero counts,
        # project_found=False.
        return IngestRunResult(project_found=False)

    monkeypatch.setattr(asset_ingest, "ingest_project_assets", fake_ingest)

    result = CliRunner().invoke(main, ["ingest-assets", "nope-9999"])
    assert result.exit_code != 0, result.output
    assert "nope-9999" in result.output
    assert "no MC-2 project resolved" in result.output


# ──────────────────────────────────────────────────────────────────────
#  ingest-assets --all
# ──────────────────────────────────────────────────────────────────────


def test_ingest_assets_all_fans_out(monkeypatch):
    _stub_client(monkeypatch)
    _stub_config(monkeypatch)

    monkeypatch.setattr(
        asset_ingest_cli,
        "active_ingestable_codes",
        lambda config: ["ibx-5153", "ggl-5168", "peb-5170"],
    )

    called: list[str] = []

    def fake_ingest(code, **kwargs):
        called.append(code)
        return IngestRunResult(created=1, skipped=1, deduped=1)

    monkeypatch.setattr(asset_ingest, "ingest_project_assets", fake_ingest)

    result = CliRunner().invoke(main, ["ingest-assets", "--all"])
    assert result.exit_code == 0, result.output
    assert called == ["ibx-5153", "ggl-5168", "peb-5170"]
    # Per-project breakdown + aggregate.
    assert "ibx-5153" in result.output
    assert "ggl-5168" in result.output
    assert "peb-5170" in result.output
    # Aggregate created across 3 projects = 3; deduped likewise rolls up.
    assert "created=3" in result.output
    assert "deduped=3" in result.output


def test_ingest_assets_all_one_project_failing_does_not_stop_others(monkeypatch):
    _stub_client(monkeypatch)
    _stub_config(monkeypatch)

    monkeypatch.setattr(
        asset_ingest_cli,
        "active_ingestable_codes",
        lambda config: ["a-1", "b-2", "c-3"],
    )

    called: list[str] = []

    def fake_ingest(code, **kwargs):
        called.append(code)
        if code == "b-2":
            raise RuntimeError("boom on b")
        return IngestRunResult(created=1)

    monkeypatch.setattr(asset_ingest, "ingest_project_assets", fake_ingest)

    result = CliRunner().invoke(main, ["ingest-assets", "--all"])
    # All three attempted despite b-2 blowing up.
    assert called == ["a-1", "b-2", "c-3"]
    # Whole-project error → non-zero exit.
    assert result.exit_code != 0
    assert "boom on b" in result.output


def test_ingest_assets_requires_code_or_all(monkeypatch):
    _stub_client(monkeypatch)
    # Neither.
    result = CliRunner().invoke(main, ["ingest-assets"])
    assert result.exit_code != 0
    assert "exactly one" in result.output.lower() or "--all" in result.output.lower()


def test_ingest_assets_rejects_both_code_and_all(monkeypatch):
    _stub_client(monkeypatch)
    result = CliRunner().invoke(main, ["ingest-assets", "ibx-5153", "--all"])
    assert result.exit_code != 0


def test_ingest_assets_scope_internal_is_noop(monkeypatch):
    _stub_client(monkeypatch)

    called: list[str] = []
    monkeypatch.setattr(
        asset_ingest,
        "ingest_project_assets",
        lambda code, **kw: called.append(code) or IngestRunResult(),
    )

    result = CliRunner().invoke(main, ["ingest-assets", "--all", "--scope", "fpsf"])
    assert result.exit_code == 0, result.output
    assert called == []
    assert "client-only" in result.output.lower()


# ──────────────────────────────────────────────────────────────────────
#  promote / demote
# ──────────────────────────────────────────────────────────────────────


def test_promote_cmd_reports_result(monkeypatch):
    _stub_client(monkeypatch)

    monkeypatch.setattr(asset_ingest, "promote_asset", lambda client, aid: True)
    result = CliRunner().invoke(main, ["promote-asset", "abc-123"])
    assert result.exit_code == 0
    assert "Promoted" in result.output
    assert "abc-123" in result.output

    monkeypatch.setattr(asset_ingest, "promote_asset", lambda client, aid: False)
    result = CliRunner().invoke(main, ["promote-asset", "abc-123"])
    assert result.exit_code == 0
    assert "already account" in result.output.lower()


def test_demote_cmd_reports_result(monkeypatch):
    _stub_client(monkeypatch)

    monkeypatch.setattr(asset_ingest, "demote_asset", lambda client, aid: True)
    result = CliRunner().invoke(main, ["demote-asset", "abc-123"])
    assert result.exit_code == 0
    assert "Demoted" in result.output

    monkeypatch.setattr(asset_ingest, "demote_asset", lambda client, aid: False)
    result = CliRunner().invoke(main, ["demote-asset", "abc-123"])
    assert result.exit_code == 0
    assert "not account" in result.output.lower()


# ──────────────────────────────────────────────────────────────────────
#  list-promotable
# ──────────────────────────────────────────────────────────────────────


def test_list_promotable_cmd_empty(monkeypatch):
    _stub_client(monkeypatch)

    monkeypatch.setattr(
        asset_ingest,
        "resolve_project_folders",
        lambda client, code: _folders(project_id="pid-1"),
    )
    monkeypatch.setattr(asset_ingest, "list_promotable", lambda client, pid: [])

    result = CliRunner().invoke(main, ["list-promotable", "ibx-5153"])
    assert result.exit_code == 0, result.output
    assert "(no promotable assets)" in result.output


def test_list_promotable_cmd_table(monkeypatch):
    _stub_client(monkeypatch)

    monkeypatch.setattr(
        asset_ingest,
        "resolve_project_folders",
        lambda client, code: _folders(project_id="pid-1"),
    )
    monkeypatch.setattr(
        asset_ingest,
        "list_promotable",
        lambda client, pid: [
            {
                "id": "a1",
                "title": "Strategy Deck",
                "url": None,
                "classifier_decision": "promote",
                "meta": {},
            }
        ],
    )

    result = CliRunner().invoke(main, ["list-promotable", "ibx-5153"])
    assert result.exit_code == 0, result.output
    assert "a1" in result.output
    assert "Strategy Deck" in result.output
    assert "promote" in result.output


# ──────────────────────────────────────────────────────────────────────
#  archive / unarchive
# ──────────────────────────────────────────────────────────────────────


def test_archive_cmd_reports_count(monkeypatch):
    _stub_client(monkeypatch)

    monkeypatch.setattr(
        asset_ingest,
        "resolve_project_folders",
        lambda client, code: _folders(project_id="pid-1"),
    )
    monkeypatch.setattr(asset_ingest, "archive_project_assets", lambda client, pid: 3)

    result = CliRunner().invoke(main, ["archive-project-assets", "ibx-5153"])
    assert result.exit_code == 0, result.output
    assert "3" in result.output
    assert "ibx-5153" in result.output


def test_unarchive_cmd_reports_count(monkeypatch):
    _stub_client(monkeypatch)

    monkeypatch.setattr(
        asset_ingest,
        "resolve_project_folders",
        lambda client, code: _folders(project_id="pid-1"),
    )
    monkeypatch.setattr(
        asset_ingest, "unarchive_project_assets", lambda client, pid: 5
    )

    result = CliRunner().invoke(main, ["unarchive-project-assets", "ibx-5153"])
    assert result.exit_code == 0, result.output
    assert "5" in result.output


def test_resolve_miss_exits_nonzero(monkeypatch):
    _stub_client(monkeypatch)
    monkeypatch.setattr(
        asset_ingest, "resolve_project_folders", lambda client, code: None
    )
    result = CliRunner().invoke(main, ["list-promotable", "nope-9999"])
    assert result.exit_code != 0


# ──────────────────────────────────────────────────────────────────────
#  helpers
# ──────────────────────────────────────────────────────────────────────


def _folders(project_id: str):
    from cp_engine.asset_ingest import ProjectFolders

    return ProjectFolders(
        project_id=project_id,
        company_id="cid-1",
        company_kind="client",
        google_drive_folder_id=None,
        mc_dropbox_folder_id=None,
        enable_google_drive=False,
        enable_dropbox=False,
    )
