"""The two SessionStart hooks, after #296 step 2 — driven as subprocesses
against fake `cxp`/`uv` binaries and a throwaway tenant, mirroring
test_tenant_freshness_hook.py.

What is being proven:

  * The PLUGIN hook now OBSERVES before it DEFERS. Inside a tenant it used to
    `exit 0` before reading a single version; now it warns when the plugin is
    ahead of the CLI — the one direction only it can see — and still never
    installs there. A CONTROL runs the v0.120.5 (pre-change) script on the same
    fixture and requires silence, so the test cannot pass by accident.
  * The TENANT hook prints `cxp doctor --brief` to STDOUT — the line the
    session's context receives — and survives a CLI too old to have `doctor`.
  * The plugin's hooks.json has ONE entry, and the chaining script runs both
    scripts in a fixed order: same-matcher hooks run in parallel, so two
    entries had no ordering at all.
  * The bash warning is byte-identical to `cp_engine.health`'s wording for
    the same direction — the drift test between the two sides of the check.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cp_engine import health
from cp_engine.health import PluginInstall

REPO = Path(__file__).resolve().parents[1]
HOOKS = REPO / "plugin" / "hooks"
PLUGIN_HOOK = HOOKS / "sync-cli-version.sh"
CHAIN = HOOKS / "session-start.sh"
TENANT_HOOK = REPO / "src" / "cp_engine" / "hooks" / "check-cp-engine-version.py"

WARN_FIRST_LINE = (
    '[cp] Your cp tools are out of sync with each other and may save bad data. '
    'Say "update cp-engine" and this session will update them — then restart '
    "Claude Code (and /mcp) to pick it up."
)


# ── fixtures ──────────────────────────────────────────────────────────────


def _fake_bin(tmp: Path, cli_version: str, doctor_line: str = "", doctor_fails: bool = False) -> Path:
    """A PATH dir with a fake `cxp` (version + doctor) and a fake `uv` that
    only logs — so an install attempt is visible and harmless."""
    b = tmp / "bin"
    b.mkdir(exist_ok=True)
    log = tmp / "uv.log"
    cxp = b / "cxp"
    cxp.write_text(
        "#!/usr/bin/env bash\n"
        'case "${1:-}" in\n'
        f'  --version) echo "cxp, version {cli_version}";;\n'
        "  doctor)\n"
        + ("    echo 'Error: No such command doctor' >&2; exit 2;;\n" if doctor_fails else
           f"    [ -n '{doctor_line}' ] && printf '%s\\n' '{doctor_line}'; exit 0;;\n")
        + "  *) exit 1;;\n"
        "esac\n"
    )
    cxp.chmod(0o755)
    uv = b / "uv"
    uv.write_text(f'#!/usr/bin/env bash\necho "$@" >> "{log}"\nexit 0\n')
    uv.chmod(0o755)
    return b


def _tenant(tmp: Path, pin: str = "~= 0.1") -> Path:
    t = tmp / "tenant"
    t.mkdir(exist_ok=True)
    (t / ".cp-engine.toml").write_text(f'[engine]\nversion = "{pin}"\n')
    (t / ".mcp.json").write_text('{"mcpServers": {}}\n')
    return t


def _plugin_root(tmp: Path, version: str) -> Path:
    r = tmp / "plugin"
    r.mkdir(exist_ok=True)
    (r / "plugin.json").write_text(json.dumps({"name": "cp-engine", "version": version}))
    return r


def _env(tmp: Path, fake_bin: Path, plugin_root: Path | None = None) -> dict:
    # /usr/bin:/bin only — no jq, so the grep/sed fallback runs; no real
    # cxp or uv; a HOME with no marketplace clone so that block no-ops.
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp),
        "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
    }
    if plugin_root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = str(plugin_root)
    return env


def _run(script: Path, cwd: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(script)], cwd=cwd, env=env, input="{}",
        capture_output=True, text=True, timeout=30,
    )


# ── the plugin hook: observe, then defer ─────────────────────────────────


def test_plugin_ahead_inside_a_tenant_warns_and_does_not_install(tmp_path: Path):
    """The one direction only this script can see. Warn, then defer — the
    fake `uv` log must stay empty."""
    b = _fake_bin(tmp_path, "0.119.0")
    r = _run(PLUGIN_HOOK, _tenant(tmp_path), _env(tmp_path, b, _plugin_root(tmp_path, "0.120.5")))
    assert r.returncode == 0
    lines = r.stdout.splitlines()
    assert lines[0] == WARN_FIRST_LINE
    assert lines[1].strip() == "(plugin v0.120.5, engine v0.119.0)"
    assert not (tmp_path / "uv.log").exists(), "inside a tenant the plugin hook must not install"


def test_plugin_behind_inside_a_tenant_is_silent_here(tmp_path: Path):
    """CLI ahead of plugin is the TENANT hook's direction (via cxp doctor).
    This script says nothing about it — a stale plugin would not have this
    code anyway, and two hooks reporting one condition is noise."""
    b = _fake_bin(tmp_path, "0.119.0")
    r = _run(PLUGIN_HOOK, _tenant(tmp_path), _env(tmp_path, b, _plugin_root(tmp_path, "0.108.1")))
    assert r.returncode == 0 and r.stdout == ""


def test_equal_versions_inside_a_tenant_are_silent(tmp_path: Path):
    b = _fake_bin(tmp_path, "0.120.5")
    r = _run(PLUGIN_HOOK, _tenant(tmp_path), _env(tmp_path, b, _plugin_root(tmp_path, "0.120.5")))
    assert r.returncode == 0 and r.stdout == ""


def test_missing_cxp_inside_a_tenant_stays_silent_and_defers(tmp_path: Path):
    """No `cxp` on PATH → no observation possible → the old behaviour: defer."""
    empty = tmp_path / "emptybin"; empty.mkdir()
    r = _run(PLUGIN_HOOK, _tenant(tmp_path), _env(tmp_path, empty, _plugin_root(tmp_path, "0.120.5")))
    assert r.returncode == 0 and r.stdout == ""


def test_control_the_committed_hook_was_silent_on_the_same_fixture(tmp_path: Path):
    """The script as RELEASED in v0.120.5 — the last version before this
    change — on plugin-ahead inside a tenant. It exits at the deferral before
    reading a version. Pinned to a tag, not HEAD, so the control stays a
    control after this change is committed."""
    old = subprocess.run(
        ["git", "-C", str(REPO), "show", "v0.120.5:plugin/hooks/sync-cli-version.sh"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "Observe BEFORE deferring" not in old, "v0.120.5 predates the observe block by construction"
    script = tmp_path / "old-hook.sh"
    script.write_text(old)
    b = _fake_bin(tmp_path, "0.119.0")
    r = _run(script, _tenant(tmp_path), _env(tmp_path, b, _plugin_root(tmp_path, "0.120.5")))
    assert r.returncode == 0
    assert r.stdout == "", "the pre-change hook must be silent here, or this is not a control"


def test_bash_warning_matches_health_wording_exactly():
    """The drift test between the two sides of the check. The plugin hook is
    bash and cannot import health.py; this is what keeps them saying the same
    thing."""
    f = health.plugin_vs_cli([PluginInstall(version="0.120.5", scope="user")], "0.119.0")
    assert f is not None and f.extra["direction"] == "cli_behind"
    assert WARN_FIRST_LINE == f"[cp] {f.summary} {f.remedy}"
    src = PLUGIN_HOOK.read_text()
    assert WARN_FIRST_LINE in src, "the hook's literal must be updated with health.py's wording"


# ── the tenant hook: doctor --brief to stdout ────────────────────────────


def _run_tenant_hook(cwd: Path, env: dict) -> subprocess.CompletedProcess:
    # The venv interpreter: the hook needs tomllib (3.11+); `packaging` is
    # optional to it, and doctor runs before the pin comparison regardless.
    return subprocess.run(
        [sys.executable, str(TENANT_HOOK)], cwd=cwd, env=env, input="{}",
        capture_output=True, text=True, timeout=30,
    )


def test_tenant_hook_prints_doctor_brief_to_stdout(tmp_path: Path):
    line = "[cp] Your cp tools are out of sync with each other and may save bad data."
    b = _fake_bin(tmp_path, "0.120.5", doctor_line=line)
    r = _run_tenant_hook(_tenant(tmp_path), _env(tmp_path, b))
    assert r.returncode == 0
    assert r.stdout.strip() == line, "the one line must be on STDOUT — that is what reaches the session"


def test_tenant_hook_is_silent_when_doctor_is_silent(tmp_path: Path):
    b = _fake_bin(tmp_path, "0.120.5", doctor_line="")
    r = _run_tenant_hook(_tenant(tmp_path), _env(tmp_path, b))
    assert r.returncode == 0 and r.stdout == ""


def test_tenant_hook_survives_a_cli_too_old_to_have_doctor(tmp_path: Path):
    """A stale CLI has no `doctor`. The hook must not fail session start —
    that direction is the plugin hook's to report."""
    b = _fake_bin(tmp_path, "0.108.1", doctor_fails=True)
    r = _run_tenant_hook(_tenant(tmp_path), _env(tmp_path, b))
    assert r.returncode == 0 and r.stdout == ""


def test_tenant_hook_runs_doctor_even_on_the_healthy_pin_path(tmp_path: Path):
    """The healthy early return is where thirteen days of drift hid. Doctor
    must run BEFORE it, not after."""
    line = "[cp] finding"
    b = _fake_bin(tmp_path, "0.120.5", doctor_line=line)
    # Pin satisfied by 0.120.5 → the hook takes `if ok: return 0`.
    r = _run_tenant_hook(_tenant(tmp_path, pin="~= 0.100"), _env(tmp_path, b))
    assert r.returncode == 0 and r.stdout.strip() == line


# ── one entry, fixed order ───────────────────────────────────────────────


def test_hooks_json_has_exactly_one_session_start_entry_pointing_at_the_chain():
    data = json.loads((HOOKS / "hooks.json").read_text())
    entries = data["hooks"]["SessionStart"]
    assert len(entries) == 1
    cmds = entries[0]["hooks"]
    assert len(cmds) == 1, "parallel hooks have no order; one entry, one script"
    assert cmds[0]["command"].endswith("/hooks/session-start.sh")


def test_chain_runs_both_scripts_in_order_and_never_fails(tmp_path: Path):
    src = CHAIN.read_text()
    assert src.index("sync-cli-version.sh") < src.index("tenant-freshness.sh")
    b = _fake_bin(tmp_path, "0.119.0")
    # A tenant that is not a git repo: freshness exits 0 silently; the CLI
    # observe block still fires, proving the chain reached the first script.
    r = _run(CHAIN, _tenant(tmp_path), _env(tmp_path, b, _plugin_root(tmp_path, "0.120.5")))
    assert r.returncode == 0
    assert r.stdout.splitlines()[0] == WARN_FIRST_LINE


# ── review fixes (2026-09-18 adversarial review of steps 1–2) ────────────

_ALL_SCRIPTS = (PLUGIN_HOOK, CHAIN, HOOKS / "tenant-freshness.sh")


@pytest.mark.parametrize("script", _ALL_SCRIPTS, ids=lambda p: p.name)
def test_D9_every_hook_script_parses_and_is_executable(script: Path):
    """The chain's `|| true` would hide a syntax error in a child forever.
    This is the gate that `|| true` needs."""
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert os.access(script, os.X_OK), f"{script.name} is not executable"


def test_D6_outside_a_tenant_plugin_ahead_prints_one_message_not_two(tmp_path: Path):
    """Outside a tenant the install path handles plugin-ahead itself and
    prints its own line. Observing there too meant two messages for one
    condition — one asking the user to request an update the hook was
    already performing. The observe block is therefore in-tenant only."""
    b = _fake_bin(tmp_path, "0.119.0")
    outside = tmp_path / "not-a-tenant"; outside.mkdir()
    r = _run(PLUGIN_HOOK, outside, _env(tmp_path, b, _plugin_root(tmp_path, "0.120.5")))
    assert r.returncode == 0
    assert WARN_FIRST_LINE not in r.stdout, "the observe line must not print outside a tenant"
    assert "Updating" in r.stdout or "updated" in r.stdout, "the install path should own this case"
    assert (tmp_path / "uv.log").exists(), "and it should actually try to install"


def _tools_dir(tmp: Path, *names: str) -> Path:
    """A PATH dir holding ONLY the named system tools, so `command -v` for
    anything else fails — the way to force a specific fallback branch."""
    d = tmp / ("tools-" + "-".join(names)); d.mkdir(exist_ok=True)
    for n in names:
        for cand in (Path("/usr/bin") / n, Path("/bin") / n):
            if cand.exists():
                (d / n).symlink_to(cand); break
    return d


def _run_plugin_version(tmp: Path, plugin_json: Path, path_dir: Path) -> str:
    src = PLUGIN_HOOK.read_text()
    start = src.index("_plugin_version() {"); end = src.index("\n}\n", start) + 3
    fn = src[start:end]
    r = subprocess.run(
        ["/bin/bash", "-c", f'PLUGIN_JSON="{plugin_json}"\n{fn}\n_plugin_version'],  # absolute: bash is not on the restricted PATH
        capture_output=True, text=True, env={"PATH": str(path_dir)},
    )
    return r.stdout.strip()


def test_D5_plugin_version_reader_takes_the_top_level_key(tmp_path: Path):
    """A nested `"version"` key ahead of the real one fools the grep/sed
    fallback — it takes the first matching LINE. The shared reader tries jq,
    then python3 on the TOP-LEVEL key, then grep/sed last. Each branch is
    forced by a PATH that omits the tools before it."""
    root = tmp_path / "plugin"; root.mkdir()
    pj = root / "plugin.json"
    pj.write_text('{\n  "schema": { "version": "2" },\n  "name": "cp-engine",\n  "version": "0.120.5"\n}\n')
    # jq present → jq reads the top-level key.
    if Path("/usr/bin/jq").exists():
        assert _run_plugin_version(tmp_path, pj, _tools_dir(tmp_path, "jq", "python3", "grep", "sed", "head")) == "0.120.5"
    # No jq → python3 reads the top-level key.
    assert _run_plugin_version(tmp_path, pj, _tools_dir(tmp_path, "python3", "grep", "sed", "head")) == "0.120.5"
    # No jq, no python3 → grep/sed, which takes the first LINE and gets it
    # WRONG. Pinned on purpose: this is why it is the last resort, not the
    # first, and why a machine with neither tool is worth knowing about.
    assert _run_plugin_version(tmp_path, pj, _tools_dir(tmp_path, "grep", "sed", "head")) == "2"


def test_D4_prerelease_plugin_is_silent_in_the_observe_block(tmp_path: Path):
    b = _fake_bin(tmp_path, "0.120.5")
    r = _run(PLUGIN_HOOK, _tenant(tmp_path), _env(tmp_path, b, _plugin_root(tmp_path, "0.121.0rc1")))
    assert r.returncode == 0 and r.stdout == ""


def test_chain_drains_stdin_and_both_children_run(tmp_path: Path):
    """D10: one stdin convention. D9: the freshness child must actually run —
    the earlier chain test only proved the FIRST child's output appeared."""
    src = CHAIN.read_text()
    assert "cat >/dev/null" in src and "</dev/null" not in src
    # Make the freshness child observable: a tenant that is a git repo with
    # a bogus upstream produces no output but must not break the chain; we
    # prove it ran by giving it a broken `git` that logs its invocation.
    b = _fake_bin(tmp_path, "0.120.5")
    (b / "git").write_text(f'#!/usr/bin/env bash\necho "git $@" >> "{tmp_path}/git.log"\nexit 1\n')
    (b / "git").chmod(0o755)
    r = _run(CHAIN, _tenant(tmp_path), _env(tmp_path, b, _plugin_root(tmp_path, "0.120.5")))
    assert r.returncode == 0
    assert (tmp_path / "git.log").exists(), "tenant-freshness.sh never ran"
