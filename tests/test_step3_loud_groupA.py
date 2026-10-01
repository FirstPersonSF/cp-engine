"""Architecture plan step 3 — fail loudly, group A (CLI / MCP surfaces).

Each test drives a path that used to swallow a failure (a bare `except`,
`except Exception: pass`, or a `logger.warning` nobody reads outside
`cxp sync`) and asserts the failure now reaches whoever gets the result:
stderr for a CLI command, a `warnings` field for an MCP tool, the sync
warning counter for a sync-only helper, a result field for a migration.

Every test here was run against the pre-step-3 code and FAILED there
(see the step-3 report for which controls were verified).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import click
from click.testing import CliRunner

from cp_engine.cli import main


def _fake_tenant(tmp_path: Path) -> Path:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.11"\n'
        '[sync]\nbackend = "github-issues"\n',
        encoding="utf-8",
    )
    (tmp_path / "sprints").mkdir(exist_ok=True)
    return tmp_path


# ── cli_cmds/core.py ────────────────────────────────────────────────────


def test_upstream_lag_check_failure_is_said(monkeypatch, tmp_path, capsys):
    import cp_engine.cli_cmds.core as core

    def boom(root):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(core, "commits_behind_upstream", boom)
    core._warn_if_behind_upstream(tmp_path)
    err = capsys.readouterr().err
    assert "could not check" in err and "git exploded" in err


def test_render_advisory_pass_crash_is_said(monkeypatch, tmp_path):
    import cp_engine.cli_cmds.core as core

    @click.command()
    def fake_sync():
        click.echo("synced")

    monkeypatch.setattr(core, "sync", fake_sync)
    monkeypatch.setattr(core, "load", lambda p: SimpleNamespace(root=tmp_path))

    def boom(root):
        raise RuntimeError("lint crashed")

    monkeypatch.setattr(core, "_exec_summary_warnings", boom)
    result = CliRunner().invoke(main, ["render"])
    assert result.exit_code == 0, result.output  # still advisory
    assert "lint crashed" in result.stderr
    assert "incomplete" in result.stderr


def test_exec_summary_walk_reports_unreadable_cp_md(monkeypatch, tmp_path):
    import cp_engine.cli_cmds.core as core
    import cp_engine.tenant_walk as tw

    bad = tmp_path / "1p" / "acme-1-x" / "cp.md"
    bad.mkdir(parents=True)  # a directory: read_text raises IsADirectoryError
    monkeypatch.setattr(tw, "walk_tenant", lambda root, name: [bad])
    out = core._exec_summary_warnings(tmp_path)
    assert any("unreadable" in w and "acme-1-x" in w for w in out), out


# ── cli_cmds/planning.py ────────────────────────────────────────────────


def test_attention_digest_prints_logged_lookup_failure(monkeypatch, tmp_path):
    """The cross-project lookup degrades to [] via logger.warning — which no
    handler printed outside sync. The command now renders it to stderr."""
    import cp_engine.mc2_db as mc2_db

    _fake_tenant(tmp_path)
    monkeypatch.chdir(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("supabase down xp-lookup")

    monkeypatch.setattr(mc2_db, "get_client", boom)
    result = CliRunner().invoke(main, ["attention-digest"])
    assert result.exit_code == 0, result.output
    assert "supabase down xp-lookup" in result.stderr


def test_prep_planning_says_allocations_read_failed(monkeypatch, tmp_path):
    import cp_engine.mc2_db as mc2_db
    import cp_engine.sync as sync_mod

    _fake_tenant(tmp_path)
    monkeypatch.chdir(tmp_path)

    class _Backend:
        def read_projects(self, config):
            return ()

        def read_allocations(self, config, monday):
            raise RuntimeError("allocations table gone")

    monkeypatch.setattr(sync_mod, "_default_backend_factory", lambda b: _Backend())
    monkeypatch.setattr(mc2_db, "get_client", lambda *a, **k: None)
    result = CliRunner().invoke(main, ["prep-planning", "--summary"])
    assert "allocations table gone" in result.stderr, (result.output, result.stderr)
    assert "last-week" in result.stderr and "planning-week" in result.stderr


# ── cli_cmds/spine.py ───────────────────────────────────────────────────


def test_feeds_sweep_reports_lookup_failure_not_no_project(monkeypatch, tmp_path):
    """A failed id lookup used to print "No project resolves" — a false
    statement about a project that exists. The real failure now shows."""
    import cp_engine.mc2_db as mc2_db

    _fake_tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mc2_db, "get_client", lambda *a, **k: object())

    def boom(client, code):
        raise RuntimeError("PGRST timeout")

    monkeypatch.setattr(mc2_db, "_resolve_project_id", boom)
    result = CliRunner().invoke(main, ["feeds-sweep", "ibx-5153"])
    assert result.exit_code == 1
    assert "PGRST timeout" in result.stderr


# ── mcp_server.py ───────────────────────────────────────────────────────


def test_mcp_preflight_carries_mc2_context_failure(monkeypatch, tmp_path):
    import cp_engine.config as config_mod
    import cp_engine.mcp_server as srv

    monkeypatch.setattr(srv, "_tenant_root", lambda: tmp_path)
    monkeypatch.setattr(config_mod, "load", lambda root: SimpleNamespace(root=tmp_path))

    def boom(code):
        raise RuntimeError("MC-2 unreachable")

    monkeypatch.setattr(srv, "_resolve", boom)
    out = srv.preflight("sap-5198", "rfp")
    assert "error" not in out, out
    assert any("MC-2 unreachable" in w for w in out.get("warnings", [])), out


def _agreement_pull(monkeypatch):
    import cp_engine.mcp_server as m

    monkeypatch.setattr(m, "_resolve", lambda code: (object(), "pid", "cid"))
    monkeypatch.setattr(
        "cp_engine.project_sources.pull_spine",
        lambda c, pid, key, cid=None: {
            "est_item_id": "_authored/sow", "layer": "Agreement",
            "body": "human terms", "sources": ["s1"]},
    )
    monkeypatch.setattr(m, "_with_project_status", lambda r, *a: r)
    return m


def test_mcp_agreement_projection_failure_is_flagged(monkeypatch):
    m = _agreement_pull(monkeypatch)

    def boom(c, pid):
        raise RuntimeError("estimator 500")

    monkeypatch.setattr("cp_engine.estimate.fetch_estimate", boom)
    res = m.pull_spine_element("p", "sow")
    assert res["body"] == "human terms"
    assert res["derived_block"] is False
    assert any("estimator 500" in w for w in res["warnings"]), res


def test_mcp_agreement_meetings_failure_is_flagged(monkeypatch):
    from tests.test_agreement_projection import _estimate

    m = _agreement_pull(monkeypatch)
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate", lambda c, pid: _estimate())
    monkeypatch.setattr("cp_engine.estimate.fetch_schedule", lambda c, ids: [])

    def boom(c, pid):
        raise RuntimeError("meetings 503")

    monkeypatch.setattr("cp_engine.project_sources.list_project_meetings", boom)
    res = m.pull_spine_element("p", "sow")
    assert res["derived_block"] is True
    assert any("meetings 503" in w for w in res["warnings"]), res


# ── claude_settings.py (surfaces through sync's warning counter) ────────


def test_unparseable_settings_json_is_logged_as_warning(tmp_path, caplog):
    from cp_engine.claude_settings import install_into_tenant

    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="cp_engine"):
        install_into_tenant(tmp_path)
    assert any(
        "settings.json" in r.getMessage() and r.levelno >= logging.WARNING
        for r in caplog.records
    ), caplog.text


def test_unparseable_mcp_json_is_logged_as_warning(tmp_path, caplog):
    from cp_engine.claude_settings import install_into_tenant

    (tmp_path / ".mcp.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="cp_engine"):
        install_into_tenant(tmp_path)
    assert any(".mcp.json" in r.getMessage() for r in caplog.records), caplog.text


# ── hooks/check-cp-engine-version.py ────────────────────────────────────


def _load_hook():
    import importlib.util

    path = (Path(__file__).resolve().parents[1] / "src" / "cp_engine" / "hooks"
            / "check-cp-engine-version.py")
    spec = importlib.util.spec_from_file_location("check_cp_engine_version", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hook_notes_unreadable_mcp_json(tmp_path, capsys):
    hook = _load_hook()
    (tmp_path / ".mcp.json").write_text("{not json", encoding="utf-8")
    hook._repair_mcp_command(tmp_path)
    assert ".mcp.json" in capsys.readouterr().err


def test_hook_notes_failed_repair_write(tmp_path, capsys, monkeypatch):
    hook = _load_hook()
    p = tmp_path / ".mcp.json"
    p.write_text(json.dumps({"mcpServers": {"cp-sources": {"command": "cp"}}}))
    real_write = Path.write_text

    def ro(self, *a, **k):
        if self == p:
            raise PermissionError("read-only")
        return real_write(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", ro)
    hook._repair_mcp_command(tmp_path)
    err = capsys.readouterr().err
    assert "FAILED to repair" in err and "read-only" in err


# ── migrate_flat.py ─────────────────────────────────────────────────────


def test_migrate_flat_reports_unrewritten_cp_links(tmp_path, monkeypatch):
    import cp_engine.config as config_mod
    from tests.test_migrate_flat import _init_tenant, _scaffold_old_layout

    from cp_engine.migrate_flat import migrate_projects_flat

    root = _init_tenant(tmp_path)
    _scaffold_old_layout(root, "1p", ["ibx-5153"])

    def boom(root):
        raise RuntimeError("config broken")

    monkeypatch.setattr(config_mod, "load", boom)
    result = migrate_projects_flat(root)
    assert result.moved_dirs  # the load-bearing part still ran
    assert any("config broken" in w for w in result.warnings), result
