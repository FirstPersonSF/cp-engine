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
    assert health.report([]).startswith("cp install: no findings from"), "never say healthy for checks that exist; say what ran"


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
    assert "→ claude plugin update cp-engine@cp-engine" in rep
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

    # Full mode reads the real machine and fetches /health; stub all of it so
    # this test asserts the exit contract, not this laptop's state.
    monkeypatch.setattr(health, "find_tenant_root", lambda *a, **k: None)
    monkeypatch.setattr(health, "fetch_hosted_health", lambda *a, **k: None)
    monkeypatch.setattr(health, "inventory", lambda **kw: {"cli": {"version": "0.0.0"}, "plugins": [],
                                                           "marketplace": {}, "tenant": {}, "hosted": None,
                                                           "install_record": None})
    monkeypatch.setattr(health, "collect", lambda **kw: [Finding(10, "x", "S.", "R.")])
    assert CliRunner().invoke(main, ["doctor"]).exit_code == 1
    monkeypatch.setattr(health, "collect", lambda **kw: [])
    r = CliRunner().invoke(main, ["doctor"])
    assert r.exit_code == 0 and "no findings" in r.output and "inventory" in r.output


# ── pin_floor / raise_pin_floor ──────────────────────────────────────────

from packaging.specifiers import SpecifierSet  # noqa: E402


def test_pin_floor_fires_on_the_thirteen_day_state():
    """CONTROL WITH MEANING. The tenant hook's own question — `0.108.1 in
    SpecifierSet("~= 0.42")` — was True every session for thirteen days.
    That is the green light that could not go red. This check asks the
    other question and fires on the same state."""
    assert Version("0.108.1") in SpecifierSet("~= 0.42"), "the old check said healthy"
    f = health.pin_floor("~= 0.42", "0.108.1")
    assert f is not None and f.code == "pin_floor"
    assert f.extra["target"] == "~= 0.108"
    assert "cxp sync" in f.remedy


@pytest.mark.parametrize("pin,cli,fires", [
    ("~= 0.120", "0.120.5", False),   # floor at the running minor: capable of failing
    ("~= 0.120", "0.121.0", True),    # one minor behind — sync has not run since a release
    ("~= 0.120", "0.119.0", False),   # engine BEHIND the pin is EngineVersionMismatch's job
    (">= 0.5, < 1", "0.120.5", True), # compound: floor is the highest lower bound (0.5)
    ("not a spec", "0.120.5", False),
    (None, "0.120.5", False),
    ("~= 0.120", None, False),
])
def test_pin_floor_matrix(pin, cli, fires):
    assert (health.pin_floor(pin, cli) is not None) is fires


_TOML = """# tenant config — hand comments must survive
[tenant]
name = "cp"

[engine]
version = "~= 0.42"   # bumped by hand until 2026-06-30, then not

[sync]
backend = "mc-2"
"""


def test_raise_pin_floor_moves_the_incident_pin_and_keeps_formatting():
    new_text, old, new = health.raise_pin_floor(_TOML, "0.120.5")
    assert (old, new) == ("~= 0.42", "~= 0.120")
    assert 'version = "~= 0.120"' in new_text
    assert "# tenant config — hand comments must survive" in new_text
    assert "# bumped by hand until 2026-06-30, then not" in new_text
    assert 'backend = "mc-2"' in new_text


def test_raise_pin_floor_never_lowers_and_is_idempotent():
    at_floor = _TOML.replace("~= 0.42", "~= 0.120")
    assert health.raise_pin_floor(at_floor, "0.120.5")[2] is None
    ahead = _TOML.replace("~= 0.42", "~= 0.121")
    text, old, new = health.raise_pin_floor(ahead, "0.120.5")
    assert new is None and text == ahead


def test_raise_pin_floor_honours_version_lock_and_leaves_compound_pins():
    locked = _TOML.replace('version = "~= 0.42"', 'version = "~= 0.42"\nversion_lock = true')
    assert health.raise_pin_floor(locked, "0.120.5")[2] is None
    compound = _TOML.replace("~= 0.42", ">= 0.5, < 1")
    text, old, new = health.raise_pin_floor(compound, "0.120.5")
    assert new is None and text == compound


def test_collect_includes_pin_floor_when_inside_a_tenant(tmp_path: Path):
    root = tmp_path / "tenant"; root.mkdir()
    (root / ".cp-engine.toml").write_text(_TOML)
    env = _fake_env(tmp_path, "0.120.5")
    findings = health.collect(cli_version="0.120.5", tenant_root=root, **env)
    assert [f.code for f in findings] == ["pin_floor"]
    assert health.brief(findings).startswith("[cp] This project's cp pin is behind")


# ── review fixes (2026-09-18 adversarial review of steps 1–2) ────────────


def test_D1_project_scoped_plugin_gets_a_project_scoped_remedy():
    """`claude plugin update` defaults to --scope user. The live finding on
    Drew's machine was a PROJECT-scoped entry: the bare command reported
    success and moved nothing, so the finding would re-fire every session
    with a remedy that 'worked'."""
    f = health.plugin_vs_cli(
        _plugins(("0.120.5", "user"), ("0.120.2", "project", "/Users/x/ggl-5136")), "0.120.5"
    )
    assert f is not None and f.extra["direction"] == "plugin_behind"
    cmd = f.extra["command"]
    assert "--scope project" in cmd and 'cd "/Users/x/ggl-5136"' in cmd
    assert f.extra["entries"][0]["project"] == "/Users/x/ggl-5136"
    # And the brief line carries the exact command for the agent.
    line = health.brief([f])
    assert "→ (cd \"/Users/x/ggl-5136\" && claude plugin update --scope project" in line


def test_D11_every_drifted_entry_has_its_own_command_in_the_report():
    f = health.plugin_vs_cli(
        _plugins(("0.40.0", "project", "/p1"), ("0.120.2", "user"), ("0.100.0", "project", "/p2")),
        "0.120.5",
    )
    rep = health.report([f])
    assert rep.count("→ ") == 3
    assert "plugin v0.40.0 (project for /p1)" in rep and 'cd "/p1"' in rep
    assert "plugin v0.120.2 (user)" in rep and "→ claude plugin update cp-engine@cp-engine" in rep
    assert "plugin v0.100.0 (project for /p2)" in rep and 'cd "/p2"' in rep
    # Worst (oldest) first, and it is the one the detail names.
    assert "plugin v0.40.0" in f.detail
    assert [e["label"] for e in f.extra["entries"]][0].startswith("plugin v0.40.0")


def test_D3_is_cxp_mcp_ignores_greps_and_arguments():
    assert not health.is_cxp_mcp("grep cxp mcp /var/log/x")
    assert not health.is_cxp_mcp("vim /Users/x/cxp mcp notes.txt")
    assert not health.is_cxp_mcp("/x/python /Users/t/.local/bin/cxp mcp-something")
    assert health.is_cxp_mcp("/x/python /Users/t/.local/bin/cxp mcp")
    assert health.is_cxp_mcp("cxp mcp")
    assert health.is_cxp_mcp("cxp mcp --verbose")


@pytest.mark.parametrize("a,b", [("0.120.5", "0.120.5rc1"), ("0.121.0", "0.121.0.dev1")])
def test_D4_prereleases_are_no_opinion_on_both_sides(a: str, b: str):
    """`sort -V` puts a suffix ABOVE the plain version; packaging puts rc/dev
    BELOW it. Rather than pick a winner, both sides decline: bash's
    `_is_plain_version` and this module's `plain_version` refuse anything
    that is not digits-and-dots, and the decision is tested against the
    bash literally."""
    assert health.plain_version(b) is None and health.plain_version(a) is not None
    assert health.plugin_vs_cli(_plugins((b, "user")), a) is None
    assert health.plugin_vs_cli(_plugins((a, "user")), b) is None
    src = PLUGIN_HOOK.read_text()
    fn = next(l for l in src.splitlines() if l.startswith("_is_plain_version()"))
    for v, ok in ((a, True), (b, False)):
        r = subprocess.run(["bash", "-c", f'{fn}; _is_plain_version "{v}"'], capture_output=True)
        assert (r.returncode == 0) is ok, (v, ok)


def test_stale_mcp_fires_whenever_mcp_server_would_warn():
    """The relation that holds between the two staleness checks: mcp_server
    warns when the on-disk version differs from the server's frozen one — a
    version change means a reinstall, which rewrote the receipt AFTER the
    server started. So that state is a subset of what stale_mcp flags."""
    server_started = _epoch("Thu Sep 17 12:49:12 2026")
    reinstall_that_changed_version = _epoch("Thu Sep 17 23:39:59 2026")
    procs = [Proc(pid=1, started=server_started, command="/x/python /y/cxp mcp")]
    assert health.stale_mcp(procs, reinstall_that_changed_version) is not None


def test_D12_clean_report_names_the_checks_that_ran():
    rep = health.report([])
    assert "healthy" not in rep
    for c in health.CHECKS:
        assert c in rep


# ── install_record (step 4) ──────────────────────────────────────────────

_LOCAL = """# cp-engine local config — gitignored, per-machine.
[repos]
"mc-2" = "/Users/x/mc-2"

[local-repos]
"cp" = "/Users/x/cp"
"""


def test_install_record_absent_fires_lowest_severity():
    f = health.install_record(None, "0.120.5", _plugins(("0.120.5", "user")))
    assert f is not None and f.code == "install_record"
    assert f.severity == health.SEV_INSTALL_RECORD
    assert "cxp sync" in f.remedy


def test_install_record_matching_is_silent():
    rec = {"cli": {"version": "0.120.5"}, "plugin": {"version": "0.120.5"}}
    assert health.install_record(rec, "0.120.5", _plugins(("0.120.5", "user"))) is None


def test_install_record_stale_fires_and_names_what_moved():
    """An install happened that nothing recorded — the September shape."""
    rec = {"cli": {"version": "0.108.1"}, "plugin": {"version": "0.108.1"}, "installer": "agent"}
    f = health.install_record(rec, "0.120.5", _plugins(("0.120.5", "user")))
    assert f is not None and "recorded" in f.summary
    assert "cli 0.108.1 → 0.120.5" in f.detail and "plugin 0.108.1 → 0.120.5" in f.detail
    assert f.extra["installer"] == "agent"


def test_write_install_record_round_trips_and_keeps_other_tables(tmp_path: Path):
    p = tmp_path / ".cp-engine.local.toml"
    p.write_text(_LOCAL)
    rec = health.build_install_record(
        cli_version="0.120.5", plugins=_plugins(("0.120.5", "user"), ("0.120.2", "project", "/p")),
        receipt_source=("git", "https://example.test/r"), tenant_root=tmp_path,
        pin="~= 0.120", hosted_url="https://cp.example.test/mcp", installer="agent",
    )
    health.write_install_record(p, rec)
    text = p.read_text()
    assert "# cp-engine local config — gitignored, per-machine." in text
    assert '"mc-2" = "/Users/x/mc-2"' in text
    assert "[install]" in text and 'installer = "agent"' in text
    got = health.read_install_record(tmp_path)
    assert got["cli"]["version"] == "0.120.5" and got["cli"]["source"] == "git+https://example.test/r"
    assert got["plugin"] == {"version": "0.120.5", "scope": "user"}
    assert got["other_plugins"][0]["project"] == "/p"
    assert got["tenant"]["pin"] == "~= 0.120"
    assert got["hosted"]["url"] == "https://cp.example.test/mcp"
    health.write_install_record(p, rec)
    assert p.read_text().count("[install]") == 1, "a rewrite replaces the table, never duplicates it"


def test_collect_checks_install_record_only_where_a_local_file_exists(tmp_path: Path):
    root = tmp_path / "tenant"; root.mkdir()
    (root / ".cp-engine.toml").write_text('[engine]\nversion = "~= 0.120"\n')
    env = _fake_env(tmp_path, "0.120.5")
    # No local file (CI, hosted-only): no opinion.
    assert health.collect(cli_version="0.120.5", tenant_root=root, **env) == []
    # Local file present, no [install]: the finding, lowest severity.
    (root / ".cp-engine.local.toml").write_text(_LOCAL)
    findings = health.collect(cli_version="0.120.5", tenant_root=root, **env)
    assert [f.code for f in findings] == ["install_record"]


# ── hosted_vs_local + inventory (step 5) ─────────────────────────────────


def test_hosted_health_url_and_version_parsing():
    assert health.hosted_health_url("https://cp.mc-2.1p.is/mcp") == "https://cp.mc-2.1p.is/health"
    assert health.hosted_health_url("https://cp.mc-2.1p.is/mcp/") == "https://cp.mc-2.1p.is/health"
    assert health.hosted_health_url(None) is None
    assert health.parse_hosted_version("hosted-cp/0.120.5") == "0.120.5"
    assert health.parse_hosted_version("hosted-cp-spike/0.0.6") == "0.0.6"
    assert health.parse_hosted_version(None) is None


def test_hosted_vs_local_fires_in_both_directions_and_names_the_deploy():
    behind = health.hosted_vs_local("0.120.2", "0.120.5", "dcc61c3a449c")
    assert behind is not None and behind.code == "hosted_vs_local"
    assert "behind" in behind.summary and "railway up" in behind.remedy
    assert "hosted v0.120.2 build dcc61c3a449c, engine v0.120.5" == behind.detail
    ahead = health.hosted_vs_local("0.121.0", "0.120.5")
    assert ahead is not None and "ahead" in ahead.summary and "update cp-engine" in ahead.remedy
    assert health.hosted_vs_local("0.120.5", "0.120.5") is None
    assert health.hosted_vs_local(None, "0.120.5") is None
    assert health.hosted_vs_local("0.0.6-spike", "0.120.5") is None, "non-plain: no opinion"


def test_collect_checks_hosted_only_when_asked(tmp_path: Path):
    root = tmp_path / "tenant"; root.mkdir()
    (root / ".cp-engine.toml").write_text('[engine]\nversion = "~= 0.120"\n')
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"cp-hosted": {"url": "https://h.test/mcp"}}}))
    env = _fake_env(tmp_path, "0.120.5")
    calls: list[str] = []

    def fake_fetch(url):
        calls.append(url)
        return {"server_version": "hosted-cp/0.120.2", "build": "abc"}

    # Default: never fetches — this is the --brief / SessionStart path.
    assert health.collect(cli_version="0.120.5", tenant_root=root, hosted_fetch=fake_fetch, **env) == []
    assert calls == []
    # Opt-in: fetches the /health URL derived from .mcp.json and reports.
    findings = health.collect(cli_version="0.120.5", tenant_root=root, check_hosted=True, hosted_fetch=fake_fetch, **env)
    assert calls == ["https://h.test/health"]
    assert [f.code for f in findings] == ["hosted_vs_local"]
    # A failed fetch is no opinion, not a finding.
    assert health.collect(cli_version="0.120.5", tenant_root=root, check_hosted=True, hosted_fetch=lambda u: None, **env) == []


def _git_repo_with_one_commit(d: Path) -> Path:
    d.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(d), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q", "--initial-branch=main"], ["commit", "-q", "--allow-empty", "-m", "x"]):
        subprocess.run(["git", "-C", str(d), *args], check=True, env=env, capture_output=True)
    return d


def test_inventory_and_report_render_what_is_installed(tmp_path: Path):
    root = tmp_path / "tenant"; root.mkdir()
    (root / ".cp-engine.toml").write_text('[engine]\nversion = "~= 0.120"\n')
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"cp-hosted": {"url": "https://h.test/mcp"}}}))
    (root / ".cp-engine.local.toml").write_text("[repos]\n")
    health.write_install_record(root / ".cp-engine.local.toml",
                                {"recorded_at": "2026-09-18T00:00:00Z", "installer": "agent", "cli": {"version": "0.120.5"}})
    env = _fake_env(tmp_path, "0.120.5")
    clone = _git_repo_with_one_commit(tmp_path / "marketplace")
    inv = health.inventory(installed_plugins_path=env["installed_plugins_path"], receipt_path=env["receipt_path"],
                           cli_version="0.120.5", tenant_root=root, marketplace_clone=clone,
                           hosted={"server_version": "hosted-cp/0.120.5", "build": "abc"})
    assert inv["cli"] == {"version": "0.120.5", "source": "git+https://example.test/r"}
    assert inv["plugins"] == [{"version": "0.120.5", "scope": "user", "project": None}]
    assert inv["marketplace"]["head"] and inv["marketplace"]["fetched"].endswith("ago")
    assert inv["tenant"]["pin"] == "~= 0.120" and inv["tenant"]["hosted_url"] == "https://h.test/mcp"
    assert inv["install_record"]["installer"] == "agent"
    text = health.report([], inv)
    assert "cp install — inventory" in text
    assert "engine     v0.120.5   from git+https://example.test/r" in text
    assert "plugin     v0.120.5   (user)" in text
    assert "hosted     hosted-cp/0.120.5   build abc" in text
    assert "recorded   2026-09-18T00:00:00Z by agent" in text
    assert text.rstrip().endswith("(plugin_vs_cli, pin_floor, stale_mcp, install_record, hosted_vs_local).")


def test_marketplace_fetch_age_is_no_opinion_for_a_non_repo(tmp_path: Path):
    assert health.marketplace_fetch_age(tmp_path / "nope") == (None, None)


def test_doctor_brief_never_touches_the_network(monkeypatch):
    """--brief is the SessionStart path: sub-second, offline-safe."""
    from cp_engine.cli import main

    def boom(*a, **k):
        raise AssertionError("--brief must not fetch")

    monkeypatch.setattr(health, "fetch_hosted_health", boom)
    monkeypatch.setattr(health, "collect", lambda **kw: [])
    r = CliRunner().invoke(main, ["doctor", "--brief"])
    assert r.exit_code == 0 and r.output == ""
