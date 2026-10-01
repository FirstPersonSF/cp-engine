#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["packaging>=24.0", "tomlkit>=0.13"]
# ///
"""Everything a release needs AFTER the tag is pushed (architecture plan step 7).

`scripts/release.py` calls this once main + tag are on origin. It can also be
run on its own to finish a release whose post steps partly failed:

    scripts/release.py --resume-post 0.131.0               # every step again
    scripts/release.py --resume-post 0.131.0 --only mc2-pin
    scripts/post_release.py 0.131.0 --skip hosted-deploy   # same thing

WHY (2026-10-01). Until this existed, `release.py` stopped at "pushed" and a
person did six more things by hand, every release: reinstall the CLI, update
each plugin install (user scope AND every project scope `cxp doctor` finds),
deploy the hosted server and watch /health, watch the webhook redeploy itself,
raise the tenant pin on a minor bump, and bump + test + push mc-2's pin. Each
was done faithfully until the day it was not — the tenant pin sat at `~= 0.42`
for 80 releases, mc-2's venv ran 24 releases behind its own pin. A step a
human must remember is a step that will eventually be skipped silently.

THE STEPS, in order. Each is idempotent (re-running a finished step reports
"already" and changes nothing), prints one line of outcome, and on failure
names the exact manual command that finishes it:

    local-install   uv tool install --force --reinstall --from <clone>; verify `cxp --version`
    plugins         marketplace update, then `claude plugin update` for EVERY install
                    in installed_plugins.json (user + project scopes); verify versions
    hosted-deploy   prototypes/hosted-mcp/deploy.sh; poll /health for hosted-cp/<v>
    webhook-verify  the webhook deploys itself from main; poll /health for <v>
    tenant-pin      MINOR bumps only: `[engine] version = "~= X.Y"` in each tenant,
                    commit, push with rebase (the tenant's push-with-rebase.sh)
    mc2-pin         bump mc-2's cp-engine pin (+ shared 1p-component-library pins to
                    cp-engine's), run mc-2's backend gate exactly as CI does, commit,
                    push to mc-2 main (= DEV), verify DEV /api/version

`plugins` and `tenant-pin` depend on `local-install`: a plugin newer than the
CLI is drift in the other direction, and a tenant pin this machine's CLI does
not satisfy fails every session's hook. A failed step blocks its dependents;
independent steps still run.

PROD IS NEVER TOUCHED. mc-2 promotes to production by pushing main to its
`release` branch. That stays a decision a person makes; this script prints the
command and refuses any push whose refspec names `release` (`_assert_dev_push`).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Callable

from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parent.parent

STEPS = ("local-install", "plugins", "hosted-deploy", "webhook-verify", "tenant-pin", "mc2-pin")
DEPENDS: dict[str, tuple[str, ...]] = {
    "plugins": ("local-install",),
    "tenant-pin": ("local-install",),
}

HOSTED_HEALTH = "https://cp.mc-2.1p.is/health"
WEBHOOK_HEALTH = "https://cp-engine-production.up.railway.app/health"
MC2_DEV_VERSION = "https://api-dev-9400.up.railway.app/api/version"
MC2_WORKFLOW = ".github/workflows/backend-tests.yml"
MC2_REQUIREMENTS = "backend/requirements.txt"
MC2_DEV_REFSPEC = "HEAD:refs/heads/main"
PLUGIN_ID = "cp-engine@cp-engine"

# ── secrets never reach the terminal ─────────────────────────────────────

_SECRET_RES = (
    re.compile(r"(https?://)[^/@\s:]+(?::[^/@\s]*)?@"),          # creds in a URL
    re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"(?i)\b((?:token|secret|password|api[_-]?key)\s*[=:]\s*)\S+"),
)


def redact(text: str) -> str:
    """Strip anything shaped like a credential from text we are about to print."""
    text = _SECRET_RES[0].sub(r"\1***@", text)
    text = _SECRET_RES[1].sub("***", text)
    return _SECRET_RES[2].sub(r"\1***", text)


def _tail(text: str, n: int = 15) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return redact("\n".join(lines[-n:]))


# ── the side-effect surface (tests replace it) ───────────────────────────


class Env:
    """Every subprocess, HTTP call and clock read goes through here."""

    def run(
        self, cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False
            )
        except FileNotFoundError as e:
            return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found ({e})")

    def get_json(self, url: str, timeout: float = 10) -> dict | None:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "cp-engine-release"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed https URLs
                return json.loads(resp.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 — "unreachable" is a poll result, not a crash
            return None

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def now(self) -> float:
        return time.monotonic()


@dataclass
class Options:
    version: str
    previous: str | None = None
    repo_root: Path = REPO_ROOT
    skip: set[str] = field(default_factory=set)
    only: set[str] = field(default_factory=set)
    tenants: list[Path] | None = None
    mc2_repo: Path | None = None
    mc2_python: Path | None = None
    poll_timeout: float = 900.0
    poll_interval: float = 15.0
    installed_plugins: Path = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    hosted_health: str = HOSTED_HEALTH
    webhook_health: str = WEBHOOK_HEALTH
    mc2_dev_version: str = MC2_DEV_VERSION


@dataclass
class Result:
    step: str
    status: str  # ok | skipped | n/a | failed | blocked
    detail: str
    manual: str | None = None


class StepFailed(Exception):
    def __init__(self, detail: str, manual: str):
        super().__init__(detail)
        self.detail = detail
        self.manual = manual


@dataclass
class Ctx:
    opts: Options
    env: Env

    def run(self, cmd, *, cwd=None, env=None):
        return self.env.run(cmd, cwd=cwd, env=env)

    def git(self, repo: Path, *args: str, env=None):
        return self.env.run(["git", "-C", str(repo), *args], env=env)

    def poll(self, url: str, ok: Callable[[dict], bool]) -> tuple[bool, dict | None]:
        deadline = self.env.now() + self.opts.poll_timeout
        last = None
        while True:
            body = self.env.get_json(url)
            if body is not None:
                last = body
                if ok(body):
                    return True, body
            if self.env.now() >= deadline:
                return False, last
            self.env.sleep(self.opts.poll_interval)


def _health_module(repo_root: Path) -> ModuleType:
    """`cp_engine.health` loaded by FILE, so this script reuses the engine's own
    plugin-registry reader and pin-floor rules without importing the package
    (whose `__init__` pulls the whole engine and its dependencies)."""
    path = repo_root / "src" / "cp_engine" / "health.py"
    spec = importlib.util.spec_from_file_location("_cp_release_health", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def minor_of(v: str) -> str:
    p = Version(v)
    return f"{p.major}.{p.minor}"


def is_minor_bump(new: str, previous: str | None) -> bool:
    """A new minor (or major) series. Without a known previous version (resume),
    X.Y.0 is the first release of its series and so the bump that needs it."""
    if previous:
        return minor_of(previous) != minor_of(new)
    return Version(new).micro == 0


# ── local-install ─────────────────────────────────────────────────────────


def _cxp_version(ctx: Ctx) -> str | None:
    out = ctx.run(["cxp", "--version"])
    m = re.search(r"version\s+(\S+)", out.stdout or "")
    return m.group(1) if out.returncode == 0 and m else None


def step_local_install(ctx: Ctx) -> str:
    v = ctx.opts.version
    root = ctx.opts.repo_root
    manual = f'uv tool install --force --reinstall --from "{root}" cp-engine && cxp --version'
    if _cxp_version(ctx) == v:
        return f"already cxp {v}"
    # `--reinstall`, never bare `--force`: bare --force skips the reinstall when
    # the version string looks unchanged, and reports success (health.py).
    out = ctx.run(["uv", "tool", "install", "-q", "--force", "--reinstall",
                   "--from", str(root), "cp-engine"])
    if out.returncode != 0:
        raise StepFailed(f"uv tool install exited {out.returncode}: {_tail(out.stderr, 3)}", manual)
    got = _cxp_version(ctx)
    if got != v:
        raise StepFailed(f"installed, but `cxp --version` reports {got or 'nothing'}", manual)
    return f"cxp {v}"


# ── plugins ───────────────────────────────────────────────────────────────


def _plugin_argv(p) -> tuple[list[str], Path | None]:
    if p.scope == "project" and p.project_path:
        return ["claude", "plugin", "update", "--scope", "project", PLUGIN_ID], Path(p.project_path)
    if p.scope != "user":
        return ["claude", "plugin", "update", "--scope", p.scope, PLUGIN_ID], None
    return ["claude", "plugin", "update", PLUGIN_ID], None


def _label(p) -> str:
    return f"project {Path(p.project_path).name}" if p.project_path else p.scope


def step_plugins(ctx: Ctx) -> str:
    v = ctx.opts.version
    health = _health_module(ctx.opts.repo_root)
    installs = health.read_installed_plugins(ctx.opts.installed_plugins)
    if not installs:
        return f"n/a: no {PLUGIN_ID} installs in {ctx.opts.installed_plugins}"
    stale = [p for p in installs if p.version != v]
    if not stale:
        return "already " + ", ".join(f"{_label(p)} {p.version}" for p in installs)
    # The marketplace clone must know the new tag first, or `update` resolves to
    # the old one and reports success (the marketplace-lag downgrade).
    mk = ctx.run(["claude", "plugin", "marketplace", "update", "cp-engine"])
    if mk.returncode != 0:
        raise StepFailed(
            f"marketplace update exited {mk.returncode}: {_tail(mk.stderr, 3)}",
            "claude plugin marketplace update cp-engine && "
            + " && ".join(health.plugin_update_command(p) for p in stale),
        )
    errors = []
    for p in stale:
        argv, cwd = _plugin_argv(p)
        if cwd is not None and not cwd.is_dir():
            errors.append((p, f"{cwd} no longer exists"))
            continue
        out = ctx.run(argv, cwd=cwd)
        if out.returncode != 0:
            errors.append((p, f"exit {out.returncode}"))
    after = health.read_installed_plugins(ctx.opts.installed_plugins)
    behind = [p for p in after if p.version != v]
    if errors or behind:
        names = [f"{_label(p)} ({why})" for p, why in errors]
        names += [f"{_label(p)} at {p.version}" for p in behind if p not in [e[0] for e in errors]]
        raise StepFailed(
            "not at " + v + ": " + ", ".join(names),
            " && ".join(health.plugin_update_command(p) for p in (behind or [e[0] for e in errors])),
        )
    return ", ".join(f"{_label(p)} {p.version}" for p in after)


# ── hosted-deploy ─────────────────────────────────────────────────────────


def step_hosted_deploy(ctx: Ctx) -> str:
    v = ctx.opts.version
    want = f"hosted-cp/{v}"
    root = ctx.opts.repo_root
    url = ctx.opts.hosted_health
    deploy = f'{root}/prototypes/hosted-mcp/deploy.sh -m "cp-engine v{v}"'
    watch = f"curl -s {url}   # until server_version == {want}"
    current = ctx.env.get_json(url) or {}
    if current.get("server_version") == want:
        return f"already live ({want}, {current.get('commit', '?')})"
    # deploy.sh ships HEAD. If HEAD is not a v<v> tree, the deploy would report a
    # version it is not running — refuse rather than ship the wrong commit.
    server = (root / "prototypes" / "hosted-mcp" / "server.py").read_text(encoding="utf-8")
    if f'SERVER_VERSION = "{want}"' not in server:
        raise StepFailed(
            f"checkout at {root} is not v{v} (server.py SERVER_VERSION differs)",
            f"git -C {root} checkout v{v} && {deploy} && {watch}",
        )
    out = ctx.run([str(root / "prototypes" / "hosted-mcp" / "deploy.sh"),
                   "-m", f"cp-engine v{v}"], cwd=root)
    if out.returncode != 0:
        raise StepFailed(f"deploy.sh exited {out.returncode}: {_tail(out.stderr or out.stdout, 3)}",
                         f"{deploy} && {watch}")
    ok, last = ctx.poll(url, lambda b: b.get("server_version") == want)
    if not ok:
        seen = (last or {}).get("server_version", "unreachable")
        raise StepFailed(
            f"queued, but /health still reports {seen} after {int(ctx.opts.poll_timeout)}s",
            f"railway deployment list --service hosted-mcp --json; {watch}",
        )
    return f"live {want} ({last.get('commit', '?')})"


# ── webhook-verify ────────────────────────────────────────────────────────


def step_webhook_verify(ctx: Ctx) -> str:
    v = ctx.opts.version
    url = ctx.opts.webhook_health
    ok, last = ctx.poll(url, lambda b: b.get("cp_engine_version") == v)
    if not ok:
        seen = (last or {}).get("cp_engine_version", "unreachable")
        raise StepFailed(
            f"/health reports {seen} after {int(ctx.opts.poll_timeout)}s "
            "(the webhook deploys itself from cp-engine main)",
            f"curl -s {url}   # until cp_engine_version == {v}; else check the "
            "webhook service's deploys in Railway",
        )
    return f"live {v} ({last.get('commit', '?')})"


# ── tenant-pin ────────────────────────────────────────────────────────────


def _is_tenant(d: Path) -> bool:
    import tomlkit

    try:
        doc = tomlkit.parse((d / ".cp-engine.toml").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — not a readable tenant config
        return False
    return "tenant" in doc and "engine" in doc


def _main_clone(ctx: Ctx, repo: Path) -> Path:
    """The primary clone of `repo` (a worktree's parent repository)."""
    out = ctx.git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if out.returncode == 0 and out.stdout.strip():
        return Path(out.stdout.strip()).parent
    return repo


def discover_tenants(ctx: Ctx) -> list[Path]:
    """Tenant clones on this machine, without a hard-coded list.

    1. `--tenant PATH` (repeatable), or `CP_RELEASE_TENANTS` (os.pathsep list).
    2. Otherwise: siblings of the cp-engine clone holding a `.cp-engine.toml`
       with `[tenant]` and `[engine]` — the same sibling layout mc-2's parity
       test relies on — plus the tenant above the clone's `.cp-link` target.
    """
    if ctx.opts.tenants is not None:
        return [Path(t).resolve() for t in ctx.opts.tenants]
    env = os.environ.get("CP_RELEASE_TENANTS")
    if env:
        return [Path(t).resolve() for t in env.split(os.pathsep) if t]
    clone = _main_clone(ctx, ctx.opts.repo_root)
    found: list[Path] = []
    try:
        siblings = sorted(p for p in clone.parent.iterdir() if p.is_dir())
    except OSError:
        siblings = []
    for d in siblings:
        if (d / ".cp-engine.toml").is_file() and _is_tenant(d):
            found.append(d.resolve())
    link = clone / ".cp-link"
    if link.is_file():
        target = Path(link.read_text(encoding="utf-8").strip())
        for d in (target, *target.parents):
            if (d / ".cp-engine.toml").is_file() and _is_tenant(d):
                if d.resolve() not in found:
                    found.append(d.resolve())
                break
    return found


def _autostash_env() -> dict[str, str]:
    e = os.environ.copy()
    n = int(e.get("GIT_CONFIG_COUNT", "0"))
    e.update({"GIT_CONFIG_COUNT": str(n + 1), f"GIT_CONFIG_KEY_{n}": "rebase.autoStash",
              f"GIT_CONFIG_VALUE_{n}": "true"})
    return e


def _push_tenant(ctx: Ctx, tenant: Path) -> subprocess.CompletedProcess[str]:
    """The tenant's own push-with-rebase.sh when it has one; else the same
    contract inline: push, rebase on a lost race, never resolve a conflict."""
    script = tenant / ".github" / "scripts" / "push-with-rebase.sh"
    env = _autostash_env()
    if script.is_file():
        env.update({"PUSH_LABEL": "release.py tenant-pin", "PUSH_BRANCH": "main"})
        return ctx.run(["bash", str(script)], cwd=tenant, env=env)
    for _ in range(3):
        out = ctx.git(tenant, "push", "origin", "HEAD:refs/heads/main", env=env)
        if out.returncode == 0:
            return out
        if not re.search(r"non-fast-forward|fetch first|rejected", out.stderr or "", re.I):
            return out
        rb = ctx.git(tenant, "pull", "--rebase", "origin", "main", env=env)
        if rb.returncode != 0:
            ctx.git(tenant, "rebase", "--abort")
            return rb
    return out


def _pin_one_tenant(ctx: Ctx, tenant: Path, health: ModuleType) -> str:
    v = ctx.opts.version
    minor = minor_of(v)
    name = tenant.name
    edit = (f'cd "{tenant}" && <set [engine] version = "~= {minor}" in .cp-engine.toml> && '
            f'git commit -m "[engine] pin ~= {minor}" -- .cp-engine.toml && '
            ".github/scripts/push-with-rebase.sh")
    if ctx.git(tenant, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != "main":
        raise StepFailed(f"{name}: not on main; refusing to commit the pin elsewhere", edit)
    if ctx.git(tenant, "fetch", "-q", "origin", "main").returncode != 0:
        raise StepFailed(f"{name}: git fetch failed", edit)
    remote = ctx.git(tenant, "show", "origin/main:.cp-engine.toml")
    if remote.returncode == 0:
        _, old, new = health.raise_pin_floor(remote.stdout, v)
        if new is None:
            return f"{name} already {old}" if old else f"{name} has no [engine] version"
    if ctx.git(tenant, "status", "--porcelain", "--", ".cp-engine.toml").stdout.strip():
        raise StepFailed(f"{name}: .cp-engine.toml has uncommitted edits", edit)
    pull = ctx.git(tenant, "pull", "-q", "--rebase", "--autostash", "origin", "main")
    if pull.returncode != 0:
        raise StepFailed(f"{name}: pull --rebase failed: {_tail(pull.stderr, 2)}", edit)
    path = tenant / ".cp-engine.toml"
    text, old, new = health.raise_pin_floor(path.read_text(encoding="utf-8"), v)
    if new is None:
        return f"{name} already {old}"
    path.write_text(text, encoding="utf-8")
    msg = f"[engine] pin {new} (cp-engine v{v} release)"
    commit = ctx.git(tenant, "commit", "-q", "-m", msg, "--", ".cp-engine.toml")
    if commit.returncode != 0:
        raise StepFailed(f"{name}: commit failed: {_tail(commit.stderr, 2)}", edit)
    push = _push_tenant(ctx, tenant)
    if push.returncode != 0:
        raise StepFailed(
            f"{name}: pin committed locally, push failed: {_tail(push.stderr or push.stdout, 2)}",
            f'cd "{tenant}" && .github/scripts/push-with-rebase.sh',
        )
    return f"{name} {old} → {new}"


def step_tenant_pin(ctx: Ctx) -> str:
    v = ctx.opts.version
    minor = minor_of(v)
    if not is_minor_bump(v, ctx.opts.previous):
        return f"n/a: patch release; ~= {minor} already admits {v}"
    tenants = discover_tenants(ctx)
    if not tenants:
        raise StepFailed(
            "no tenant clones found (siblings of the cp-engine clone, .cp-link)",
            f"scripts/release.py --resume-post {v} --only tenant-pin --tenant <tenant-clone>",
        )
    health = _health_module(ctx.opts.repo_root)
    done, failed = [], []
    for t in tenants:
        try:
            done.append(_pin_one_tenant(ctx, t, health))
        except StepFailed as e:
            failed.append(e)
    if failed:
        raise StepFailed("; ".join([*done, *(e.detail for e in failed)]),
                         " ; ".join(e.manual for e in failed))
    return "; ".join(done)


# ── mc2-pin ───────────────────────────────────────────────────────────────

_CP_ENGINE_PIN = re.compile(
    r"(cp-engine\s*@\s*git\+https://github\.com/FirstPersonSF/cp-engine\.git@)v[^\s#]+"
)
_COMPONENT_PIN = re.compile(
    r"(1p-component-library\.git@)(?P<rev>[0-9a-f]+)(#subdirectory=(?P<sub>[\w./-]+))"
)
_GIT_PIN_LINE = re.compile(r"^[A-Za-z0-9_.\-]+\s*@\s*git\+\S+", re.MULTILINE)


def component_pins(text: str) -> dict[str, str]:
    """`{subdirectory: rev}` for every 1p-component-library git pin in `text`."""
    return {m.group("sub"): m.group("rev") for m in _COMPONENT_PIN.finditer(text)}


def bump_mc2_requirements(text: str, version: str, engine_pins: dict[str, str]) -> str:
    """mc-2's requirements with cp-engine at v<version> and every shared
    component-library pin at cp-engine's revision (test_shared_pin_parity)."""
    if not _CP_ENGINE_PIN.search(text):
        raise ValueError("no `cp-engine @ git+…cp-engine.git@v…` pin in requirements.txt")
    text = _CP_ENGINE_PIN.sub(rf"\g<1>v{version}", text, count=1)

    def repin(m: re.Match) -> str:
        rev = engine_pins.get(m.group("sub"), m.group("rev"))
        return f"{m.group(1)}{rev}{m.group(3)}"

    return _COMPONENT_PIN.sub(repin, text)


def parse_gate(workflow_text: str) -> tuple[list[str], str, str | None]:
    """(pytest argv, working directory, python version) of backend-tests.yml's
    test step — read from the workflow so the --ignore list cannot drift."""
    lines = workflow_text.splitlines()
    py = re.search(r'python-version:\s*"?([\d.]+)"?', workflow_text)
    for i, line in enumerate(lines):
        s = line.strip()
        if not (s == "pytest" or s.startswith("pytest ")):
            continue
        parts = [s]
        j = i
        while parts[-1].endswith("\\") and j + 1 < len(lines):
            parts[-1] = parts[-1][:-1]
            j += 1
            parts.append(lines[j].strip())
        wd = "."
        for k in range(i, -1, -1):
            m = re.match(r"\s*working-directory:\s*(\S+)", lines[k])
            if m:
                wd = m.group(1)
                break
            if re.match(r"\s*- (name|uses):", lines[k]):
                break
        return shlex.split(" ".join(parts)), wd, py.group(1) if py else None
    raise ValueError(f"no `pytest` command in {MC2_WORKFLOW}")


def find_mc2_python(mc2: Path, want: str | None, override: Path | None) -> Path:
    """mc-2's own venv — never a global install, never a new venv.

    Prefers the venv whose Python matches the workflow's `python-version`.
    """
    if override:
        if not Path(override).is_file():
            raise ValueError(f"--mc2-python {override} does not exist")
        return Path(override)
    candidates = [mc2 / "backend" / "venv", mc2 / "venv", mc2 / "backend" / ".venv", mc2 / ".venv"]
    usable = []
    for c in candidates:
        cfg, py = c / "pyvenv.cfg", c / "bin" / "python"
        if not (cfg.is_file() and py.exists()):
            continue
        m = re.search(r"^version(?:_info)?\s*=\s*(\S+)", cfg.read_text(), re.MULTILINE)
        ver = m.group(1) if m else ""
        usable.append((c, py, ver))
    for c, py, ver in usable:
        if want and (ver == want or ver.startswith(want + ".")):
            return py
    if usable:
        if want:
            raise ValueError(
                f"no mc-2 venv on Python {want} (CI's version); found "
                + ", ".join(f"{c.relative_to(mc2)} ({ver})" for c, _, ver in usable)
                + " — pass --mc2-python to choose one")
        return usable[0][1]
    raise ValueError(f"no venv under {mc2} (looked in backend/venv, venv, backend/.venv, .venv)")


def _assert_dev_push(refspec: str) -> None:
    """mc-2 PROD deploys off `release`. This pipeline never writes it."""
    if "release" in refspec or refspec != MC2_DEV_REFSPEC:
        raise RuntimeError(f"refusing mc-2 push {refspec!r}: only {MC2_DEV_REFSPEC} is automated")


def _dev_has(ctx: Ctx, mc2: Path, sha: str) -> Callable[[dict], bool]:
    def ok(body: dict) -> bool:
        dev = str(body.get("commit") or "")
        if not dev:
            return False
        if dev.startswith(sha) or sha.startswith(dev):
            return True
        ctx.git(mc2, "fetch", "-q", "origin", "main")
        return ctx.git(mc2, "merge-base", "--is-ancestor", sha, dev).returncode == 0
    return ok


def step_mc2_pin(ctx: Ctx) -> str:
    v = ctx.opts.version
    root = ctx.opts.repo_root
    mc2 = Path(ctx.opts.mc2_repo or os.environ.get("MC2_REPO")
               or _main_clone(ctx, root).parent / "mc-2")
    prod = f"git -C {mc2} push origin origin/main:refs/heads/release"
    manual_head = (f"cd {mc2} && edit {MC2_REQUIREMENTS} (cp-engine @v{v}, spine-authoring to "
                   f"cp-engine's pin) && run backend-tests.yml's pytest && git push origin main")
    if not (mc2 / MC2_REQUIREMENTS).is_file():
        raise StepFailed(f"no mc-2 clone at {mc2}", "pass --mc2-repo <mc-2 clone>")
    tagged = ctx.git(root, "show", f"v{v}:pyproject.toml")
    if tagged.returncode != 0:
        raise StepFailed(f"tag v{v} not found in {root}", f"git -C {root} fetch --tags")
    engine_pins = component_pins(tagged.stdout)
    if ctx.git(mc2, "fetch", "-q", "origin", "main").returncode != 0:
        raise StepFailed("git fetch in mc-2 failed", manual_head)
    current = ctx.git(mc2, "show", f"origin/main:{MC2_REQUIREMENTS}")
    if current.returncode != 0:
        raise StepFailed(f"cannot read origin/main:{MC2_REQUIREMENTS}", manual_head)
    try:
        wanted = bump_mc2_requirements(current.stdout, v, engine_pins)
    except ValueError as e:
        raise StepFailed(str(e), manual_head) from e

    if wanted == current.stdout:
        # Already pinned on main (an earlier run, or by hand). Still prove DEV runs it.
        log = ctx.git(mc2, "log", "-1", "--format=%H", f"-Scp-engine.git@v{v}",
                      "origin/main", "--", MC2_REQUIREMENTS)
        sha = log.stdout.strip() or ctx.git(mc2, "rev-parse", "origin/main").stdout.strip()
        ok, last = ctx.poll(ctx.opts.mc2_dev_version, _dev_has(ctx, mc2, sha))
        if not ok:
            raise StepFailed(
                f"main already pins v{v} ({sha[:7]}) but DEV runs {(last or {}).get('commit', '?')[:7]}",
                f"curl -s {ctx.opts.mc2_dev_version}")
        return f"already pinned v{v} on main; DEV at {(last or {}).get('commit', '')[:7]}. PROD: {prod}"

    wt = Path(tempfile.mkdtemp(prefix="mc2-pin-"))
    added = ctx.git(mc2, "worktree", "add", "-q", "--detach", str(wt), "origin/main")
    if added.returncode != 0:
        raise StepFailed(f"git worktree add failed: {_tail(added.stderr, 2)}", manual_head)
    try:
        return _mc2_in_worktree(ctx, mc2, wt, wanted, engine_pins, prod, manual_head)
    finally:
        ctx.git(mc2, "worktree", "remove", "--force", str(wt))


def _mc2_in_worktree(ctx, mc2, wt, wanted, engine_pins, prod, manual_head) -> str:
    v = ctx.opts.version
    (wt / MC2_REQUIREMENTS).write_text(wanted, encoding="utf-8")
    try:
        gate, wd, py_ver = parse_gate((wt / MC2_WORKFLOW).read_text(encoding="utf-8"))
        python = find_mc2_python(mc2, py_ver, ctx.opts.mc2_python)
    except (OSError, ValueError) as e:
        raise StepFailed(str(e), manual_head) from e
    backend = wt / "backend"
    # Same install CI does — then force the git pins: pip skips a git dependency
    # whose version string did not change (test_installed_pins_match's reason).
    inst = ctx.run([str(python), "-m", "pip", "install", "-q", "-r", "requirements.txt"], cwd=backend)
    pins = _GIT_PIN_LINE.findall(wanted)
    if inst.returncode == 0 and pins:
        inst = ctx.run([str(python), "-m", "pip", "install", "-q", "--no-deps",
                        "--force-reinstall", *pins], cwd=backend)
    if inst.returncode != 0:
        raise StepFailed(f"pip install into {python.parent.parent.name} failed: "
                         f"{_tail(inst.stderr, 3)}", manual_head)
    exe = python.parent / gate[0]
    argv = ([str(exe)] if exe.exists() else [str(python), "-m", gate[0]]) + gate[1:]
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["CP_ENGINE_PATH"] = str(ctx.opts.repo_root)  # the parity guard reads the released pins
    env["VIRTUAL_ENV"] = str(python.parent.parent)
    out = ctx.run(argv, cwd=wt / wd, env=env)
    summary = _pytest_summary(out.stdout or "")
    if out.returncode != 0 or summary is None:
        raise StepFailed(
            f"mc-2 backend gate exit {out.returncode} ({summary or 'no summary line'}); "
            f"nothing committed:\n{_tail(out.stdout, 12)}",
            manual_head)
    ctx.git(wt, "add", MC2_REQUIREMENTS)
    sa = engine_pins.get("services/spine-authoring")
    msg = f"Pin cp-engine v{v}" + (f" (spine-authoring @{sa})" if sa else "") + \
        f"\n\nBumped by cp-engine scripts/release.py after the v{v} release; " \
        f"backend gate: {summary}."
    commit = ctx.git(wt, "commit", "-q", "-m", msg, "--", MC2_REQUIREMENTS)
    if commit.returncode != 0:
        raise StepFailed(f"commit failed: {_tail(commit.stderr, 2)}", manual_head)
    push = _push_mc2_dev(ctx, wt)
    if push.returncode != 0:
        raise StepFailed(f"gate {summary}; push to main failed: {_tail(push.stderr, 2)}",
                         f"git -C {mc2} push origin <the pin commit>:refs/heads/main")
    sha = ctx.git(wt, "rev-parse", "HEAD").stdout.strip()
    ok, last = ctx.poll(ctx.opts.mc2_dev_version, _dev_has(ctx, mc2, sha))
    if not ok:
        raise StepFailed(
            f"pushed {sha[:7]} (gate {summary}) but DEV reports "
            f"{(last or {}).get('commit', 'unreachable')[:7]}",
            f"curl -s {ctx.opts.mc2_dev_version}   # until commit == {sha}")
    return f"v{v} on main {sha[:7]}, gate {summary}, DEV live. PROD: {prod}"


def _push_mc2_dev(ctx: Ctx, wt: Path) -> subprocess.CompletedProcess[str]:
    out = None
    for _ in range(3):
        _assert_dev_push(MC2_DEV_REFSPEC)
        out = ctx.git(wt, "push", "-q", "origin", MC2_DEV_REFSPEC)
        if out.returncode == 0:
            return out
        if not re.search(r"non-fast-forward|fetch first|rejected", out.stderr or "", re.I):
            return out
        # A commit landed on main meanwhile. Rebase the one-file pin onto it;
        # a conflict is not ours to settle.
        ctx.git(wt, "fetch", "-q", "origin", "main")
        rb = ctx.git(wt, "rebase", "origin/main")
        if rb.returncode != 0:
            ctx.git(wt, "rebase", "--abort")
            return rb
    return out


_PYTEST_SUMMARY_RE = re.compile(
    r"^=*\s*(?P<body>(?:\d+ \w+(?:, )?)+) in [\d.]+s(?: \([^)]*\))?\s*=*\s*$"
)


def _pytest_summary(output: str) -> str | None:
    for line in reversed(output.splitlines()):
        m = _PYTEST_SUMMARY_RE.match(line.strip())
        if m:
            return m.group("body").rstrip(", ")
    return None


# ── the pipeline ──────────────────────────────────────────────────────────

STEP_FUNCS: dict[str, Callable[[Ctx], str]] = {
    "local-install": step_local_install,
    "plugins": step_plugins,
    "hosted-deploy": step_hosted_deploy,
    "webhook-verify": step_webhook_verify,
    "tenant-pin": step_tenant_pin,
    "mc2-pin": step_mc2_pin,
}


def selected(opts: Options) -> list[str]:
    unknown = (opts.skip | opts.only) - set(STEPS)
    if unknown:
        raise ValueError(f"unknown step(s): {', '.join(sorted(unknown))}; steps: {', '.join(STEPS)}")
    if opts.only:
        return [s for s in STEPS if s in opts.only]
    return [s for s in STEPS if s not in opts.skip]


def run_pipeline(opts: Options, env: Env | None = None, out=print) -> list[Result]:
    ctx = Ctx(opts, env or Env())
    chosen = selected(opts)
    results: dict[str, Result] = {}
    for step in STEPS:
        if step not in chosen:
            results[step] = Result(step, "skipped", "--skip" if step in opts.skip else "not in --only")
            continue
        bad = [d for d in DEPENDS.get(step, ()) if results.get(d) and results[d].status in ("failed", "blocked")]
        if bad:
            r = Result(step, "blocked", f"{', '.join(bad)} did not finish")
        else:
            try:
                detail = STEP_FUNCS[step](ctx)
                r = Result(step, "n/a" if detail.startswith("n/a") else "ok", detail)
            except StepFailed as e:
                r = Result(step, "failed", e.detail, e.manual)
            except Exception as e:  # noqa: BLE001 — a crash is a failed step, not a lost summary
                r = Result(step, "failed", f"{type(e).__name__}: {redact(str(e))}",
                           f"scripts/release.py --resume-post {opts.version} --only {step}")
        results[step] = r
        out(f"[post] {step}: {r.status} — {redact(r.detail.splitlines()[0] if r.detail else '')}")
    ordered = [results[s] for s in STEPS]
    print_summary(opts, ordered, out)
    return ordered


def resume_command(version: str, results: list[Result]) -> str | None:
    redo = [r.step for r in results if r.status in ("failed", "blocked")]
    if not redo:
        return None
    return f"scripts/release.py --resume-post {version} " + " ".join(f"--only {s}" for s in redo)


def print_summary(opts: Options, results: list[Result], out=print) -> None:
    out("")
    out(f"[post] v{opts.version} post-release summary")
    w = max(len(s) for s in STEPS)
    for r in results:
        first = redact(r.detail.splitlines()[0]) if r.detail else ""
        out(f"[post]   {r.step:<{w}}  {r.status:<7}  {first}")
    failed = [r for r in results if r.status == "failed"]
    for r in failed:
        out(f"[post] ✗ {r.step}: {redact(r.detail)}")
        if r.manual:
            out(f"[post]     finish by hand: {redact(r.manual)}")
    cmd = resume_command(opts.version, results)
    if cmd:
        out(f"[post] resume: {cmd}")
    if any(r.step == "local-install" and r.status == "ok" and "already" not in r.detail for r in results):
        out("[post] note: running `cxp mcp` servers keep the old code until /mcp restarts them.")


def ok(results: list[Result]) -> bool:
    return not any(r.status in ("failed", "blocked") for r in results)


def add_post_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--skip", action="append", default=[], choices=STEPS, metavar="STEP",
                   help=f"Skip a post-release step (repeatable). Steps: {', '.join(STEPS)}.")
    p.add_argument("--only", action="append", default=[], choices=STEPS, metavar="STEP",
                   help="Run only these post-release steps (repeatable).")
    p.add_argument("--tenant", action="append", type=Path, metavar="PATH",
                   help="Tenant clone for tenant-pin (repeatable; default: discovered).")
    p.add_argument("--mc2-repo", type=Path, metavar="PATH",
                   help="mc-2 clone (default: $MC2_REPO, else the cp-engine clone's sibling mc-2).")
    p.add_argument("--mc2-python", type=Path, metavar="PATH",
                   help="Python of the mc-2 venv for its backend gate (default: discovered).")
    p.add_argument("--poll-timeout", type=float, default=900.0, metavar="SECS",
                   help="How long to wait for each deploy to report the new version.")


def options_from_args(args: argparse.Namespace, version: str, previous: str | None) -> Options:
    if args.skip and args.only:
        raise ValueError("--skip and --only together are ambiguous; use one")
    return Options(
        version=version, previous=previous, skip=set(args.skip), only=set(args.only),
        tenants=args.tenant, mc2_repo=args.mc2_repo, mc2_python=args.mc2_python,
        poll_timeout=args.poll_timeout,
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run cp-engine's post-release steps for a pushed tag.")
    p.add_argument("version")
    p.add_argument("--previous", help="The version released before this one (minor-bump detection).")
    add_post_args(p)
    args = p.parse_args(argv)
    try:
        opts = options_from_args(args, args.version, args.previous)
        selected(opts)
    except ValueError as e:
        print(f"[post] {e}", file=sys.stderr)
        return 2
    return 0 if ok(run_pipeline(opts)) else 1


if __name__ == "__main__":
    sys.exit(main())
