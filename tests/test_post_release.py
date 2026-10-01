"""Tests for `scripts/post_release.py` and its wiring into `scripts/release.py`
(architecture plan step 7: the release does the whole release).

Every external command is faked: `_FakeEnv` answers subprocess calls by the
command's shape, serves scripted HTTP bodies per URL, and never sleeps. The
command log is the assertion surface — "mc-2's `release` branch is never
touched" is checked against every argv the pipeline produced.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import post_release as pr  # noqa: E402


def _load_release():
    spec = importlib.util.spec_from_file_location("release_script_s7", SCRIPTS / "release.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["release_script_s7"] = mod
    spec.loader.exec_module(mod)
    return mod


def _cp(cmd, code=0, out="", err=""):
    return subprocess.CompletedProcess(cmd, code, out, err)


class _FakeEnv(pr.Env):
    """Answers by handler: the first `(predicate, response)` that matches wins.

    A response is a CompletedProcess-returning callable `(cmd, cwd, env)`.
    Unmatched commands succeed with empty output — and are still logged."""

    def __init__(self, handlers=(), http=None):
        self.handlers = list(handlers)
        self.http = {k: list(v) for k, v in (http or {}).items()}
        self.calls: list[tuple[list[str], Path | None, dict | None]] = []
        self.t = 0.0

    def run(self, cmd, *, cwd=None, env=None):
        self.calls.append((list(cmd), cwd, env))
        for pred, resp in self.handlers:
            if pred(cmd):
                return resp(cmd, cwd, env)
        return _cp(cmd)

    def get_json(self, url, timeout=10):
        q = self.http.get(url)
        if not q:
            return None
        return q.pop(0) if len(q) > 1 else q[0]

    def sleep(self, s):
        self.t += s

    def now(self):
        return self.t

    def argvs(self):
        return [c[0] for c in self.calls]


def _has(*parts):
    return lambda cmd: all(p in cmd for p in parts)


def _opts(**kw):
    kw.setdefault("version", "0.131.0")
    kw.setdefault("poll_timeout", 30)
    kw.setdefault("poll_interval", 10)
    return pr.Options(**kw)


# ── selection, dependencies, resume ──────────────────────────────────────


def test_skip_and_only_select_steps():
    assert pr.selected(_opts()) == list(pr.STEPS)
    assert "mc2-pin" not in pr.selected(_opts(skip={"mc2-pin"}))
    assert pr.selected(_opts(only={"plugins", "tenant-pin"})) == ["plugins", "tenant-pin"]
    with pytest.raises(ValueError, match="unknown"):
        pr.selected(_opts(skip={"nope"}))


def _stub_steps(monkeypatch, outcomes):
    calls = []

    def make(step):
        def f(ctx):
            calls.append(step)
            o = outcomes.get(step, "fine")
            if isinstance(o, Exception):
                raise o
            return o
        return f

    monkeypatch.setattr(pr, "STEP_FUNCS", {s: make(s) for s in pr.STEPS})
    return calls


def test_a_failing_step_blocks_its_dependents_and_prints_the_resume_command(monkeypatch):
    calls = _stub_steps(monkeypatch, {
        "local-install": pr.StepFailed("uv exited 1", "uv tool install --force --reinstall ..."),
    })
    lines = []
    results = pr.run_pipeline(_opts(), _FakeEnv(), out=lines.append)
    by = {r.step: r.status for r in results}
    assert by["local-install"] == "failed"
    assert by["plugins"] == "blocked" and by["tenant-pin"] == "blocked"
    # independent steps still ran
    assert {"hosted-deploy", "webhook-verify", "mc2-pin"} <= set(calls)
    assert "plugins" not in calls and "tenant-pin" not in calls
    text = "\n".join(lines)
    assert ("scripts/release.py --resume-post 0.131.0 --only local-install "
            "--only plugins --only tenant-pin") in text
    assert "finish by hand: uv tool install --force --reinstall" in text
    assert not pr.ok(results)


def test_only_runs_just_that_step_and_an_unselected_dependency_does_not_block(monkeypatch):
    calls = _stub_steps(monkeypatch, {})
    results = pr.run_pipeline(_opts(only={"plugins"}), _FakeEnv(), out=lambda s: None)
    assert calls == ["plugins"]
    assert {r.step: r.status for r in results}["plugins"] == "ok"
    assert pr.ok(results)


def test_skip_is_reported_as_skipped(monkeypatch):
    calls = _stub_steps(monkeypatch, {})
    results = pr.run_pipeline(_opts(skip={"hosted-deploy"}), _FakeEnv(), out=lambda s: None)
    assert "hosted-deploy" not in calls
    assert {r.step: r.status for r in results}["hosted-deploy"] == "skipped"


def test_a_crashing_step_is_a_failed_step_not_a_lost_summary(monkeypatch):
    _stub_steps(monkeypatch, {"webhook-verify": KeyError("boom")})
    lines = []
    results = pr.run_pipeline(_opts(), _FakeEnv(), out=lines.append)
    assert {r.step: r.status for r in results}["webhook-verify"] == "failed"
    assert any("post-release summary" in ln for ln in lines)


def test_redact_hides_credentials():
    s = pr.redact("fatal: https://x-access-token:ghp_abcdefghijklmnopqrstuvwx@github.com/a/b token=sekrit")
    assert "ghp_" not in s and "sekrit" not in s and "x-access-token" not in s


# ── local-install ─────────────────────────────────────────────────────────


def test_local_install_is_a_noop_when_the_cli_is_already_current():
    env = _FakeEnv([(_has("cxp", "--version"), lambda c, *_: _cp(c, 0, "cxp, version 0.131.0\n"))])
    assert pr.step_local_install(pr.Ctx(_opts(), env)).startswith("already")
    assert not any(a[0] == "uv" for a in env.argvs())


def test_local_install_reinstalls_and_verifies():
    versions = iter(["cxp, version 0.130.0\n", "cxp, version 0.131.0\n"])
    env = _FakeEnv([(_has("cxp", "--version"), lambda c, *_: _cp(c, 0, next(versions)))])
    assert pr.step_local_install(pr.Ctx(_opts(), env)) == "cxp 0.131.0"
    uv = [a for a in env.argvs() if a[0] == "uv"][0]
    assert "--reinstall" in uv and "--force" in uv


def test_local_install_fails_when_the_cli_does_not_move():
    env = _FakeEnv([(_has("cxp", "--version"), lambda c, *_: _cp(c, 0, "cxp, version 0.130.0\n"))])
    with pytest.raises(pr.StepFailed, match="reports 0.130.0") as e:
        pr.step_local_install(pr.Ctx(_opts(), env))
    assert "uv tool install --force --reinstall" in e.value.manual


# ── plugins ───────────────────────────────────────────────────────────────


def _plugins_file(tmp_path, entries):
    p = tmp_path / "installed_plugins.json"
    p.write_text(json.dumps({"plugins": {"cp-engine@cp-engine": entries}}))
    return p


def test_plugins_updates_user_and_every_project_scope(tmp_path):
    proj = tmp_path / "events-calendar"
    proj.mkdir()
    path = _plugins_file(tmp_path, [
        {"scope": "user", "version": "0.130.0"},
        {"scope": "project", "projectPath": str(proj), "version": "0.130.0"},
    ])

    def update(cmd, cwd, env):
        data = json.loads(path.read_text())
        for e in data["plugins"]["cp-engine@cp-engine"]:
            if ("project" in cmd) == (e["scope"] == "project"):
                e["version"] = "0.131.0"
        path.write_text(json.dumps(data))
        return _cp(cmd)

    env = _FakeEnv([(lambda c: "update" in c and "marketplace" not in c, update)])
    detail = pr.step_plugins(pr.Ctx(_opts(installed_plugins=path), env))
    assert "user 0.131.0" in detail and "project events-calendar 0.131.0" in detail
    argvs = env.argvs()
    assert argvs[0] == ["claude", "plugin", "marketplace", "update", "cp-engine"]
    proj_call = [c for c in env.calls if "project" in c[0]][0]
    assert proj_call[1] == proj  # issued from inside the project, or it moves nothing


def test_plugins_fails_with_the_command_that_reaches_the_stale_entry(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    path = _plugins_file(tmp_path, [{"scope": "project", "projectPath": str(proj), "version": "0.40.0"}])
    with pytest.raises(pr.StepFailed) as e:
        pr.step_plugins(pr.Ctx(_opts(installed_plugins=path), _FakeEnv()))
    assert "--scope project" in e.value.manual and str(proj) in e.value.manual


def test_plugins_already_current_runs_nothing(tmp_path):
    path = _plugins_file(tmp_path, [{"scope": "user", "version": "0.131.0"}])
    env = _FakeEnv()
    assert pr.step_plugins(pr.Ctx(_opts(installed_plugins=path), env)).startswith("already")
    assert env.calls == []


# ── hosted-deploy / webhook-verify ────────────────────────────────────────


def _fake_root(tmp_path, server_version="hosted-cp/0.131.0"):
    d = tmp_path / "cp-engine" / "prototypes" / "hosted-mcp"
    d.mkdir(parents=True)
    (d / "server.py").write_text(f'SERVER_VERSION = "{server_version}"\n')
    return tmp_path / "cp-engine"


def test_hosted_deploy_is_idempotent_when_already_live(tmp_path):
    env = _FakeEnv(http={pr.HOSTED_HEALTH: [{"server_version": "hosted-cp/0.131.0", "commit": "abc"}]})
    detail = pr.step_hosted_deploy(pr.Ctx(_opts(repo_root=_fake_root(tmp_path)), env))
    assert detail.startswith("already live")
    assert env.calls == []


def test_hosted_deploy_deploys_then_polls_until_live(tmp_path):
    env = _FakeEnv(http={pr.HOSTED_HEALTH: [
        {"server_version": "hosted-cp/0.130.0"}, {"server_version": "hosted-cp/0.130.0"},
        {"server_version": "hosted-cp/0.131.0", "commit": "def"},
    ]})
    detail = pr.step_hosted_deploy(pr.Ctx(_opts(repo_root=_fake_root(tmp_path)), env))
    assert detail.startswith("live hosted-cp/0.131.0")
    assert env.argvs()[0][0].endswith("prototypes/hosted-mcp/deploy.sh")


def test_hosted_deploy_times_out_with_the_railway_command(tmp_path):
    env = _FakeEnv(http={pr.HOSTED_HEALTH: [{"server_version": "hosted-cp/0.130.0"}]})
    with pytest.raises(pr.StepFailed, match="still reports hosted-cp/0.130.0") as e:
        pr.step_hosted_deploy(pr.Ctx(_opts(repo_root=_fake_root(tmp_path)), env))
    assert "railway deployment list --service hosted-mcp" in e.value.manual


def test_hosted_deploy_refuses_a_checkout_that_is_not_the_release(tmp_path):
    env = _FakeEnv(http={pr.HOSTED_HEALTH: [{"server_version": "hosted-cp/0.130.0"}]})
    root = _fake_root(tmp_path, server_version="hosted-cp/0.130.0")
    with pytest.raises(pr.StepFailed, match="not v0.131.0"):
        pr.step_hosted_deploy(pr.Ctx(_opts(repo_root=root), env))
    assert env.calls == []


def test_webhook_verify_polls_and_times_out():
    ok_env = _FakeEnv(http={pr.WEBHOOK_HEALTH: [{"cp_engine_version": "0.130.0"},
                                                {"cp_engine_version": "0.131.0", "commit": "c"}]})
    assert pr.step_webhook_verify(pr.Ctx(_opts(), ok_env)).startswith("live 0.131.0")
    stale = _FakeEnv(http={pr.WEBHOOK_HEALTH: [{"cp_engine_version": "0.130.0"}]})
    with pytest.raises(pr.StepFailed, match="0.130.0"):
        pr.step_webhook_verify(pr.Ctx(_opts(), stale))


# ── tenant-pin ────────────────────────────────────────────────────────────

_TENANT_TOML = '[tenant]\nname = "cp"\n\n[engine]\nversion = "~= 0.130"  # floor\n'


def _tenant(tmp_path, name="cp", toml=_TENANT_TOML, script=True):
    t = tmp_path / name
    t.mkdir()
    (t / ".cp-engine.toml").write_text(toml)
    if script:
        (t / ".github" / "scripts").mkdir(parents=True)
        (t / ".github" / "scripts" / "push-with-rebase.sh").write_text("#!/bin/bash\n")
    return t


def _tenant_env(remote_toml):
    return _FakeEnv([
        (_has("rev-parse", "--abbrev-ref"), lambda c, *_: _cp(c, 0, "main\n")),
        (_has("show", "origin/main:.cp-engine.toml"), lambda c, *_: _cp(c, 0, remote_toml)),
    ])


def test_tenant_pin_on_a_patch_release_writes_nothing(tmp_path):
    t = _tenant(tmp_path)
    env = _tenant_env(_TENANT_TOML)
    detail = pr.step_tenant_pin(pr.Ctx(_opts(version="0.130.1", previous="0.130.0", tenants=[t]), env))
    assert detail.startswith("n/a")
    assert env.calls == []
    assert (t / ".cp-engine.toml").read_text() == _TENANT_TOML


def test_tenant_pin_on_a_minor_release_raises_commits_and_pushes_with_rebase(tmp_path):
    t = _tenant(tmp_path)
    env = _tenant_env(_TENANT_TOML)
    detail = pr.step_tenant_pin(pr.Ctx(_opts(previous="0.130.2", tenants=[t]), env))
    assert detail == "cp ~= 0.130 → ~= 0.131"
    text = (t / ".cp-engine.toml").read_text()
    assert 'version = "~= 0.131"' in text and "# floor" in text  # comment preserved
    argvs = env.argvs()
    commit = [a for a in argvs if "commit" in a][0]
    assert commit[-2:] == ["--", ".cp-engine.toml"]  # only the pin, never the person's work
    push = [c for c in env.calls if c[0][0] == "bash"][0]
    assert push[0][1].endswith("push-with-rebase.sh")
    assert push[2]["PUSH_BRANCH"] == "main"


def test_tenant_pin_is_idempotent_when_origin_already_has_it(tmp_path):
    t = _tenant(tmp_path)
    env = _tenant_env(_TENANT_TOML.replace("0.130", "0.131"))
    detail = pr.step_tenant_pin(pr.Ctx(_opts(previous="0.130.2", tenants=[t]), env))
    assert detail == "cp already ~= 0.131"
    assert not any("commit" in a for a in env.argvs())


def test_tenant_pin_refuses_a_tenant_off_main(tmp_path):
    t = _tenant(tmp_path)
    env = _FakeEnv([(_has("rev-parse", "--abbrev-ref"), lambda c, *_: _cp(c, 0, "feature\n"))])
    with pytest.raises(pr.StepFailed, match="not on main"):
        pr.step_tenant_pin(pr.Ctx(_opts(previous="0.130.0", tenants=[t]), env))


def test_resume_without_a_previous_version_treats_x_y_0_as_the_minor_bump():
    assert pr.is_minor_bump("0.131.0", None)
    assert not pr.is_minor_bump("0.131.2", None)
    assert pr.is_minor_bump("0.131.0", "0.130.4")
    assert not pr.is_minor_bump("0.131.1", "0.131.0")


def test_tenants_are_discovered_as_siblings_of_the_cp_engine_clone(tmp_path):
    root = tmp_path / "cp-engine"
    root.mkdir()
    _tenant(tmp_path, "cp")
    _tenant(tmp_path, "not-a-tenant", toml='[project]\nname = "x"\n')
    (tmp_path / "plain").mkdir()
    env = _FakeEnv([(_has("--git-common-dir"), lambda c, *_: _cp(c, 0, f"{root}/.git\n"))])
    found = pr.discover_tenants(pr.Ctx(_opts(repo_root=root), env))
    assert found == [(tmp_path / "cp").resolve()]


def test_explicit_tenants_override_discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("CP_RELEASE_TENANTS", "/nowhere")
    ctx = pr.Ctx(_opts(tenants=[tmp_path]), _FakeEnv())
    assert pr.discover_tenants(ctx) == [tmp_path.resolve()]


# ── mc2-pin ───────────────────────────────────────────────────────────────

_REQS = """fastapi
cp-engine @ git+https://github.com/FirstPersonSF/cp-engine.git@v0.126.3
spine-authoring @ git+https://github.com/FirstPersonSF/1p-component-library.git@811781e#subdirectory=services/spine-authoring
note-dm-format @ git+https://github.com/FirstPersonSF/1p-component-library.git@a9ab75e#subdirectory=utilities/note-dm-format
"""
_ENGINE_PYPROJECT = """dependencies = [
    "spine-authoring @ git+https://github.com/FirstPersonSF/1p-component-library.git@abc1234#subdirectory=services/spine-authoring",
    "note-dm-format @ git+https://github.com/FirstPersonSF/1p-component-library.git@a9ab75e#subdirectory=utilities/note-dm-format",
]
"""
_WORKFLOW = """jobs:
  pytest:
    steps:
      - uses: actions/setup-python@v5
        with:
          python-version: "3.13"
      - name: Install
        working-directory: backend
        run: pip install -r requirements.txt
      - name: Run tests
        working-directory: backend
        run: |
          pytest tests/ -q \\
            --ignore=tests/test_a_router.py \\
            --ignore=tests/test_b_router.py
"""


def test_bump_mc2_requirements_moves_cp_engine_and_the_shared_pins():
    out = pr.bump_mc2_requirements(_REQS, "0.131.0", pr.component_pins(_ENGINE_PYPROJECT))
    assert "cp-engine.git@v0.131.0" in out
    assert "@abc1234#subdirectory=services/spine-authoring" in out
    assert "@a9ab75e#subdirectory=utilities/note-dm-format" in out
    assert "fastapi" in out


def test_parse_gate_reads_the_ignore_list_and_working_directory():
    argv, wd, py = pr.parse_gate(_WORKFLOW)
    assert argv == ["pytest", "tests/", "-q", "--ignore=tests/test_a_router.py",
                    "--ignore=tests/test_b_router.py"]
    assert wd == "backend" and py == "3.13"


def test_parse_gate_against_mc2s_real_workflow():
    wf = REPO_ROOT.parent / "mc-2" / pr.MC2_WORKFLOW
    if not wf.is_file():
        pytest.skip("no mc-2 checkout beside cp-engine")
    argv, wd, py = pr.parse_gate(wf.read_text())
    assert argv[:2] == ["pytest", "tests/"] and wd == "backend"
    assert sum(a.startswith("--ignore=") for a in argv) >= 10


def _venv(root: Path, rel: str, version: str):
    v = root / rel
    (v / "bin").mkdir(parents=True)
    (v / "bin" / "python").write_text("")
    (v / "bin" / "pytest").write_text("")
    (v / "pyvenv.cfg").write_text(f"home = /x\nversion = {version}\n")
    return v


def test_find_mc2_python_prefers_cis_python_version(tmp_path):
    _venv(tmp_path, "backend/.venv", "3.14.3")
    want = _venv(tmp_path, "backend/venv", "3.13.3")
    assert pr.find_mc2_python(tmp_path, "3.13", None) == want / "bin" / "python"
    with pytest.raises(ValueError, match="no mc-2 venv on Python 3.12"):
        pr.find_mc2_python(tmp_path, "3.12", None)


def _mc2(tmp_path):
    mc2 = tmp_path / "mc-2"
    (mc2 / "backend").mkdir(parents=True)
    (mc2 / pr.MC2_REQUIREMENTS).write_text(_REQS)
    _venv(mc2, "backend/venv", "3.13.3")
    return mc2


def _mc2_env(tmp_path, *, gate_code=0, dev_commits=("old", "f" * 40), remote_reqs=_REQS):
    def worktree_add(cmd, cwd, env):
        wt = Path(cmd[cmd.index("--detach") + 1])
        (wt / "backend").mkdir(parents=True, exist_ok=True)
        (wt / pr.MC2_REQUIREMENTS).write_text(remote_reqs)
        (wt / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        (wt / pr.MC2_WORKFLOW).write_text(_WORKFLOW)
        return _cp(cmd)

    def worktree_remove(cmd, cwd, env):
        shutil.rmtree(cmd[-1], ignore_errors=True)
        return _cp(cmd)

    gate_out = "1027 passed in 61.0s\n" if gate_code == 0 else "1 failed, 1026 passed in 60.0s\n"
    return _FakeEnv(
        [
            (_has("show", "v0.131.0:pyproject.toml"), lambda c, *_: _cp(c, 0, _ENGINE_PYPROJECT)),
            (_has("show", f"origin/main:{pr.MC2_REQUIREMENTS}"), lambda c, *_: _cp(c, 0, remote_reqs)),
            (_has("worktree", "add"), worktree_add),
            (_has("worktree", "remove"), worktree_remove),
            (lambda c: c[0].endswith("/pytest"), lambda c, *_: _cp(c, gate_code, gate_out)),
            (_has("rev-parse", "HEAD"), lambda c, *_: _cp(c, 0, "f" * 40 + "\n")),
            (_has("merge-base", "--is-ancestor"), lambda c, *_: _cp(c, 1)),
        ],
        http={pr.MC2_DEV_VERSION: [{"commit": c} for c in dev_commits]},
    )


def _no_release_ref(env):
    for argv in env.argvs():
        args = [a for i, a in enumerate(argv) if i == 0 or argv[i - 1] != "-m"]  # not messages
        for a in args:
            ref = a.split(":")[-1]
            assert not (ref == "release" or ref.endswith("/release")), (
                f"mc2-pin touched a release ref: {argv}")


def test_mc2_pin_bumps_tests_commits_pushes_dev_and_verifies(tmp_path):
    mc2 = _mc2(tmp_path)
    env = _mc2_env(tmp_path)
    detail = pr.step_mc2_pin(pr.Ctx(_opts(mc2_repo=mc2), env))
    assert detail.startswith("v0.131.0 on main fffffff, gate 1027 passed, DEV live")
    assert "PROD: git -C" in detail and "origin/main:refs/heads/release" in detail  # printed, not run
    argvs = env.argvs()
    gate = [c for c in env.calls if c[0][0].endswith("/pytest")][0]
    assert gate[0][1:] == ["tests/", "-q", "--ignore=tests/test_a_router.py",
                           "--ignore=tests/test_b_router.py"]
    assert gate[1].name == "backend"
    assert gate[2]["CP_ENGINE_PATH"] == str(REPO_ROOT)
    assert any("--force-reinstall" in a for a in argvs)  # stale git deps re-fetched
    push = [a for a in argvs if "push" in a]
    assert push == [["git", "-C", push[0][2], "push", "-q", "origin", "HEAD:refs/heads/main"]]
    assert any("worktree" in a and "remove" in a for a in argvs)
    _no_release_ref(env)


def test_mc2_pin_red_gate_commits_and_pushes_nothing(tmp_path):
    env = _mc2_env(tmp_path, gate_code=1)
    with pytest.raises(pr.StepFailed, match="gate exit 1 .*nothing committed"):
        pr.step_mc2_pin(pr.Ctx(_opts(mc2_repo=_mc2(tmp_path)), env))
    argvs = env.argvs()
    assert not any("commit" in a or "push" in a for a in argvs)
    assert any("worktree" in a and "remove" in a for a in argvs)  # cleaned up anyway
    _no_release_ref(env)


def test_mc2_pin_already_pinned_only_verifies_dev(tmp_path):
    pinned = pr.bump_mc2_requirements(_REQS, "0.131.0", pr.component_pins(_ENGINE_PYPROJECT))
    env = _mc2_env(tmp_path, remote_reqs=pinned, dev_commits=("e" * 40,))
    env.handlers.insert(0, (_has("log", "-1"), lambda c, *_: _cp(c, 0, "e" * 40 + "\n")))
    detail = pr.step_mc2_pin(pr.Ctx(_opts(mc2_repo=_mc2(tmp_path)), env))
    assert detail.startswith("already pinned v0.131.0")
    assert not any("push" in a or "worktree" in a for a in env.argvs())


def test_mc2_dev_push_guard_refuses_release():
    for bad in ("origin/main:refs/heads/release", "HEAD:refs/heads/release", "HEAD:release"):
        with pytest.raises(RuntimeError, match="refusing"):
            pr._assert_dev_push(bad)
    pr._assert_dev_push(pr.MC2_DEV_REFSPEC)


# ── release.py wiring + #346 ─────────────────────────────────────────────


def _bump_targets(tmp_path, release):
    files = {
        "PYPROJECT": ("pyproject.toml", '[project]\nname = "cp-engine"\nversion = "0.130.0"\n'),
        "INIT_PY": ("__init__.py", '__version__ = "0.130.0"\n'),
        "PLUGIN_JSON": ("plugin.json", '{"version": "0.130.0"}\n'),
        "MARKETPLACE_JSON": ("marketplace.json", '{"version": "0.130.0"}\n'),
        "WEBHOOK_PYPROJECT": ("webhook.toml", 'deps = ["cp-engine==0.130.0"]\n'),
        "HOSTED_SERVER": ("server.py", 'SERVER_VERSION = "hosted-cp/0.130.0"\n'),
        "UV_LOCK": ("uv.lock", 'version = "0.130.0"\n'),
    }
    patches = []
    for const, (name, body) in files.items():
        p = tmp_path / name
        p.write_text(body)
        patches.append(patch.object(release, const, p))
    return patches, {tmp_path / n: b for n, b in files.values()}


def _run_release_main(release, argv, *, pytest_result=(0, "10 passed"), post=None):
    from packaging.version import Version

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["git", "tag", "--list"]:
            return subprocess.CompletedProcess(cmd, 0, "v0.131.0\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    ctx = [
        patch.object(release, "preflight", lambda v: (Version("0.130.0"), Version(v))),
        patch.object(release, "ci_gate", lambda: "a" * 40),
        patch.object(release, "run", fake_run),
        patch.object(release, "run_pytest_counted", lambda *a, **k: pytest_result),
        patch.object(release, "changelog_oneline", lambda v: "notes"),
        patch.object(sys, "argv", ["release.py", *argv]),
    ]
    if hasattr(release, "post_release"):  # absent before step 7; controls run on old code
        ctx.append(patch.object(release.post_release, "run_pipeline",
                                post or (lambda opts, *a, **k: [pr.Result("x", "ok", "")])))
    for c in ctx:
        c.start()
    try:
        try:
            code = release.main()
        except SystemExit as e:  # the pre-#346 script exited this way
            code = e.code
    finally:
        for c in ctx:
            c.stop()
    return code, calls


def test_346_red_tests_leave_every_version_file_as_it_was(tmp_path):
    """CONTROL (#346 part 1): on the old script this left six files bumped."""
    release = _load_release()
    patches, before = _bump_targets(tmp_path, release)
    for p in patches:
        p.start()
    try:
        code, calls = _run_release_main(release, ["0.131.0", "--skip-build"],
                                        pytest_result=(1, "1 failed, 9 passed"))
    finally:
        for p in patches:
            p.stop()
    assert code not in (0, None)
    for path, body in before.items():
        assert path.read_text() == body, f"{path.name} left bumped after red tests"
    assert not any(c[:2] == ["git", "commit"] or c[:2] == ["git", "push"] for c in calls)


def test_a_green_release_runs_the_post_pipeline_after_the_push(tmp_path):
    """CONTROL: the old script stopped at the push and printed reminders."""
    release = _load_release()
    patches, _ = _bump_targets(tmp_path, release)
    seen = {}

    def post(opts, *a, **k):
        seen["opts"] = opts
        return [pr.Result("local-install", "ok", "")]

    for p in patches:
        p.start()
    try:
        code, calls = _run_release_main(release, ["0.131.0", "--skip-build", "--skip", "mc2-pin"],
                                        post=post)
    finally:
        for p in patches:
            p.stop()
    assert code == 0
    assert seen["opts"].version == "0.131.0" and seen["opts"].previous == "0.130.0"
    assert seen["opts"].skip == {"mc2-pin"}
    assert ["git", "push", "origin", "v0.131.0"] in calls


def test_resume_post_runs_only_the_post_steps():
    """CONTROL: `--resume-post` did not exist."""
    release = _load_release()
    seen = {}

    def post(opts, *a, **k):
        seen["opts"] = opts
        return [pr.Result("mc2-pin", "failed", "x")]

    code, calls = _run_release_main(release, ["--resume-post", "0.131.0", "--only", "mc2-pin"],
                                    post=post)
    assert code == 1  # a failed post step is a non-zero exit
    assert seen["opts"].only == {"mc2-pin"} and seen["opts"].previous is None
    assert not any(c[:2] in (["git", "commit"], ["git", "push"], ["git", "tag"]) and
                   c[:3] != ["git", "tag", "--list"] for c in calls)


def test_no_post_stops_after_the_push(tmp_path):
    release = _load_release()
    patches, _ = _bump_targets(tmp_path, release)
    ran = []
    for p in patches:
        p.start()
    try:
        code, _ = _run_release_main(release, ["0.131.0", "--skip-build", "--no-post"],
                                    post=lambda *a, **k: ran.append(1) or [])
    finally:
        for p in patches:
            p.stop()
    assert code == 0 and ran == []
