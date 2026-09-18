"""`cp_engine.health` — the one module behind `cxp doctor` (#296).

Every check is a pure function over data, so the incident's exact state can be
pinned without a drifted machine. Two tests here are CONTROLS against the
mechanism that existed before this module: they show the plugin hook's own
no-downgrade guard calling the incident fixture healthy, and they show the bash
comparison agreeing with the Python one — the drift test the plan requires so
the two copies cannot quietly diverge.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from click.testing import CliRunner
from packaging.version import Version

from cp_engine import health
from cp_engine.health import Finding, PluginInstall, Proc

REPO = Path(__file__).resolve().parents[1]
PLUGIN_HOOK = REPO / "plugin" / "hooks" / "sync-cli-version.sh"


def _plugins(*pairs: tuple[str, str] | tuple[str, str, str]) -> list[PluginInstall]:
    out = []
    for p in pairs:
        out.append(PluginInstall(version=p[0], scope=p[1], project_path=p[2] if len(p) > 2 else None))
    return out


# ── plugin_vs_cli ─────────────────────────────────────────────────────────


def test_plugin_behind_cli_warns_with_plugin_remedy():
    """Plugin stale, engine ahead — the transient state on Tony's machine the
    night of 09-17, and the direction the plugin hook's guard suppresses."""
    f = health.plugin_vs_cli(_plugins(("0.108.1", "user")), "0.119.0")
    assert f is not None
    assert f.code == "plugin_vs_cli"
    assert f.extra["direction"] == "plugin_behind"
    assert f.extra["command"] == "claude plugin update cp-engine@cp-engine"
    assert "restart" in f.remedy
    assert "plugin v0.108.1" in f.detail and "engine v0.119.0" in f.detail


def test_cli_behind_plugin_warns_with_reinstall_remedy():
    """The other direction. A single hard-coded remedy is wrong half the time."""
    f = health.plugin_vs_cli(
        _plugins(("0.120.5", "user")), "0.119.0",
        receipt_source=("git", "https://github.com/FirstPersonSF/cp-engine"),
    )
    assert f is not None
    assert f.extra["direction"] == "cli_behind"
    cmd = f.extra["command"]
    assert "--force --reinstall" in cmd, "bare --force is the silent no-op"
    assert "@v0.120.5" in cmd, "the target is the plugin's version, not HEAD"
    assert "/mcp" in f.remedy, "a reinstalled binary leaves cxp mcp on old bytecode"


def test_equal_versions_are_silent():
    assert health.plugin_vs_cli(_plugins(("0.120.5", "user")), "0.120.5") is None


def test_every_registered_scope_is_read():
    """Drew's machine: user 0.120.2 AND a project-scope 0.40.0 nobody knew
    about. A reader that stopped at the first entry called it healthy."""
    f = health.plugin_vs_cli(
        _plugins(("0.120.2", "user"), ("0.40.0", "project", "/Users/drewf/Documents/Python/ggl-5136")),
        "0.120.2",
    )
    assert f is not None
    assert f.extra["direction"] == "plugin_behind"
    assert "ggl-5136" in f.detail


def test_unparseable_version_means_no_opinion():
    assert health.plugin_vs_cli(_plugins(("0.0.0-golden", "user")), "0.120.5") is None
    assert health.plugin_vs_cli(_plugins(("0.120.5", "user")), None) is None
    assert health.plugin_vs_cli(_plugins(("0.120.5", "user")), "not-a-version") is None


def test_reinstall_command_follows_the_receipt():
    assert 'from "/Users/x/cp-engine"' in health.cli_reinstall_command(
        "0.120.5", ("directory", "/Users/x/cp-engine")
    )
    git = health.cli_reinstall_command("0.120.5", ("git", "https://example.test/cp-engine"))
    assert 'git+https://example.test/cp-engine@v0.120.5' in git
    # No receipt at all → the canonical repo, still pinned to the target tag.
    assert "FirstPersonSF/cp-engine.git@v0.120.5" in health.cli_reinstall_command("0.120.5", None)


def test_read_receipt_both_shapes(tmp_path: Path):
    tony = tmp_path / "tony.toml"
    tony.write_text(
        '[tool]\nrequirements = [{ name = "cp-engine", git = '
        '"https://github.com/FirstPersonSF/cp-engine?rev=v0.120.2" }]\n'
    )
    assert health.read_receipt_source(tony) == ("git", "https://github.com/FirstPersonSF/cp-engine")
    drew = tmp_path / "drew.toml"
    drew.write_text('[tool]\nrequirements = [{ name = "cp-engine", directory = "/Users/drewf/cp-engine" }]\n')
    assert health.read_receipt_source(drew) == ("directory", "/Users/drewf/cp-engine")
    assert health.read_receipt_source(tmp_path / "missing.toml") is None


def test_read_installed_plugins_returns_every_entry(tmp_path: Path):
    p = tmp_path / "installed_plugins.json"
    p.write_text(json.dumps({"version": 2, "plugins": {
        "cp-engine@cp-engine": [
            {"scope": "user", "version": "0.120.2", "installPath": "/x"},
            {"scope": "project", "version": "0.40.0", "installPath": "/y", "projectPath": "/proj"},
        ],
        "other@m": [{"scope": "user", "version": "9.9.9"}],
    }}))
    got = health.read_installed_plugins(p)
    assert [(g.version, g.scope, g.project_path) for g in got] == [
        ("0.120.2", "user", None), ("0.40.0", "project", "/proj"),
    ]
    assert health.read_installed_plugins(tmp_path / "nope.json") == []


# ── CONTROLS against the pre-existing mechanism ──────────────────────────


def _bash_guard_highest(plugin: str, installed: str) -> str:
    """Run the plugin hook's EXACT no-downgrade comparison, read from the
    script rather than copied, so this test cannot drift from it."""
    src = PLUGIN_HOOK.read_text()
    line = next(l for l in src.splitlines() if l.strip().startswith("_highest=$(printf"))
    script = f'PLUGIN_VERSION="{plugin}"; INSTALLED_VERSION="{installed}"; {line.strip()}; echo "$_highest"'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True).stdout.strip()


def test_control_the_hooks_guard_calls_the_incident_fixture_healthy():
    """The mechanism that existed: on plugin 0.108.1 / CLI 0.119.0 the guard
    sees the installed CLI as the highest and takes its 'ahead → no-op' exit.
    This module warns on the same fixture. That is the whole defect."""
    assert _bash_guard_highest("0.108.1", "0.119.0") == "0.119.0", "guard fires → exit 0 → silent"
    assert health.plugin_vs_cli(_plugins(("0.108.1", "user")), "0.119.0") is not None


@pytest.mark.parametrize("a,b", [
    ("0.108.1", "0.119.0"), ("0.120.5", "0.119.0"), ("0.9.0", "0.10.0"),
    ("0.120.10", "0.120.9"), ("0.42.0", "0.120.5"), ("1.0.0", "0.999.0"),
])
def test_bash_and_python_version_ordering_agree(a: str, b: str):
    """The drift test: the plugin hook's `sort -V` and this module's
    `packaging.Version` must pick the same 'highest', or the two sides of the
    plugin-vs-CLI check can disagree about direction."""
    bash_highest = _bash_guard_highest(a, b)
    py_highest = str(max(Version(a), Version(b)))
    assert bash_highest == py_highest


# ── stale_mcp ─────────────────────────────────────────────────────────────

# Tony's machine, 2026-09-18, verbatim shape of `ps -eo pid,lstart,command`.
_PS = """\
  PID STARTED                      COMMAND
79265 Fri Sep 18 11:37:37 2026 /x/cp-engine/bin/python /Users/tonywelch/.local/bin/cxp mcp
 4573 Thu Sep 17 12:49:12 2026 /x/cp-engine/bin/python /Users/tonywelch/.local/bin/cxp mcp
13691 Thu Sep 17 13:03:59 2026 /x/cp-engine/bin/python /Users/tonywelch/.local/bin/cxp mcp
  999 Thu Sep 17 09:00:00 2026 /usr/bin/python3 /Users/tonywelch/.local/bin/cxp sync
"""


def _epoch(s: str) -> float:
    return time.mktime(time.strptime(s, "%a %b %d %H:%M:%S %Y"))


def test_parse_ps_reads_lstart_rows():
    procs = health.parse_ps(_PS)
    assert [p.pid for p in procs] == [79265, 4573, 13691, 999]
    assert procs[1].started == _epoch("Thu Sep 17 12:49:12 2026")


def test_stale_mcp_names_the_processes_older_than_the_install():
    """Install written Sep 17 23:39 → two of three servers predate it. The
    `cxp sync` process is not a server and must not be counted."""
    install = _epoch("Thu Sep 17 23:39:59 2026")
    f = health.stale_mcp(health.parse_ps(_PS), install)
    assert f is not None
    assert f.code == "stale_mcp"
    assert f.extra["pids"] == [4573, 13691]
    assert "2 cp tool servers" in f.summary
    assert "/mcp" in f.remedy


def test_stale_mcp_is_silent_when_all_servers_postdate_the_install():
    install = _epoch("Thu Sep 17 12:00:00 2026")
    assert health.stale_mcp(health.parse_ps(_PS), install) is None


def test_stale_mcp_no_receipt_means_no_opinion():
    assert health.stale_mcp(health.parse_ps(_PS), None) is None


def test_is_cxp_mcp_matches_the_server_and_not_other_cxp_verbs():
    assert health.is_cxp_mcp("/x/python /Users/t/.local/bin/cxp mcp")
    assert health.is_cxp_mcp("cxp mcp")
    assert not health.is_cxp_mcp("/x/python /Users/t/.local/bin/cxp sync")
    assert not health.is_cxp_mcp("/x/python /Users/t/.local/bin/cxp mcp-something")


# ── collect / brief / report ─────────────────────────────────────────────


def _fake_env(tmp_path: Path, plugin_version: str, procs_ps: str = "") -> dict:
    ip = tmp_path / "installed_plugins.json"
    ip.write_text(json.dumps({"plugins": {"cp-engine@cp-engine": [{"scope": "user", "version": plugin_version}]}}))
    rp = tmp_path / "uv-receipt.toml"
    rp.write_text('[tool]\nrequirements = [{ name = "cp-engine", git = "https://example.test/r?rev=v1" }]\n')
    return dict(installed_plugins_path=ip, receipt_path=rp, procs=health.parse_ps(procs_ps))


def test_collect_is_silent_when_healthy(tmp_path: Path):
    assert health.collect(cli_version="0.120.5", **_fake_env(tmp_path, "0.120.5")) == []
    assert health.brief([]) == ""
    assert health.report([]) == "cp install: healthy."


def test_collect_sorts_most_severe_first_and_brief_counts_the_rest(tmp_path: Path):
    env = _fake_env(tmp_path, "0.108.1", _PS)
    # Make every server stale relative to the receipt we just wrote.
    import os
    os.utime(env["receipt_path"], (time.time(), time.time()))
    findings = health.collect(cli_version="0.119.0", **env)
    assert [f.code for f in findings] == ["plugin_vs_cli", "stale_mcp"]
    line = health.brief(findings)
    assert line.startswith("[cp] Your cp tools are out of sync")
    assert "(+1 more: cxp doctor)" in line
    assert "(plugin v0.108.1 (user), engine v0.119.0)" in line
    rep = health.report(findings)
    assert "command: claude plugin update cp-engine@cp-engine" in rep
    assert "pid 4573, 13691, 79265" in rep  # all three predate a receipt written just now


# ── the CLI face ─────────────────────────────────────────────────────────


def test_doctor_brief_always_exits_zero_so_stdout_reaches_the_session(monkeypatch):
    """A SessionStart hook's stdout is added to the session's context only on
    exit 0; non-zero routes to stderr as an error. So --brief never fails."""
    from cp_engine.cli import main

    finding = Finding(severity=10, code="plugin_vs_cli", summary="S.", remedy="R.", detail="d")
    monkeypatch.setattr(health, "collect", lambda **kw: [finding])
    r = CliRunner().invoke(main, ["doctor", "--brief"])
    assert r.exit_code == 0
    assert r.output.startswith("[cp] S. R.")

    monkeypatch.setattr(health, "collect", lambda **kw: [])
    r = CliRunner().invoke(main, ["doctor", "--brief"])
    assert r.exit_code == 0 and r.output == ""


def test_doctor_full_exits_one_on_findings(monkeypatch):
    from cp_engine.cli import main

    monkeypatch.setattr(health, "collect", lambda **kw: [Finding(10, "x", "S.", "R.")])
    assert CliRunner().invoke(main, ["doctor"]).exit_code == 1
    monkeypatch.setattr(health, "collect", lambda **kw: [])
    r = CliRunner().invoke(main, ["doctor"])
    assert r.exit_code == 0 and "healthy" in r.output
