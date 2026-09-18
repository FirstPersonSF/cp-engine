"""Install-integrity findings — the one module behind `cxp doctor` (#296).

WHY ONE MODULE. On 2026-09-17 a `cxp` twelve releases ahead of its plugin ran a
routine render and rewrote 54 provenance stamps backwards. Five mechanisms
existed to prevent that and every one was silent: the plugin hook defers inside
a tenant, its no-downgrade guard treats "installed is ahead" as healthy, the
tenant hook compared against a pin 78 releases wide and never read the plugin
version at all, its self-heal needed a clone the user did not have, and the MCP
staleness warning only speaks inside MCP results. Five checks, two languages,
nothing keeping them in agreement — and the fix proposed first was a sixth.

So: every check lives HERE, as a pure function over data. Everything else is a
caller — `cxp doctor --brief` (the one SessionStart line), `cxp doctor` (the
report), the tenant hook, and `cxp sync`. The plugin hook keeps one bash
comparison for the single direction it can observe (see below), and
`tests/test_health.py` asserts that comparison agrees with this module's.

THE CONSTRAINT THAT SHAPES IT. A component that has drifted cannot carry the
check that detects its own drift: a stale plugin runs its stale hook. Each
direction of plugin-vs-CLI drift is therefore observable only from the side that
is current — CLI-ahead (the incident) only from the CLI side, which is this
module; plugin-ahead only from the plugin hook. `plugin_vs_cli` here reports
BOTH directions regardless, because `cxp doctor` may be run by hand on either.

Checks take their inputs as arguments so the tests can pin the exact incident
state (plugin 0.108.1, CLI 0.119.0) without a drifted machine. The `gather_*`
helpers read the real system and are the only functions here that touch it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from packaging.version import InvalidVersion, Version
from packaging.specifiers import InvalidSpecifier, SpecifierSet

# ── findings ──────────────────────────────────────────────────────────────

# Lower sorts first. The incident's own check is the most severe; bookkeeping
# is the least. `brief()` prints the first and counts the rest.
SEV_PLUGIN_VS_CLI = 10
SEV_PIN_FLOOR = 20
SEV_STALE_MCP = 30
SEV_HOSTED_VS_LOCAL = 40
SEV_INSTALL_RECORD = 50


@dataclass(frozen=True)
class Finding:
    """One thing wrong with the install, with the action that fixes it.

    `summary` and `remedy` are written for the SessionStart line, which lands
    in the SESSION'S context (Claude Code adds SessionStart stdout as context
    the agent can act on) as well as in front of the user. So they name the
    condition, the consequence, the action, and — where a restart is needed
    for the action to take effect — say so. `detail` is the evidence for
    whoever debugs it; it goes last, parenthesised.
    """

    severity: int
    code: str
    summary: str
    remedy: str
    detail: str = ""
    extra: dict = field(default_factory=dict)


# ── version helpers ───────────────────────────────────────────────────────


def parse_version(v: str | None) -> Version | None:
    """A comparable version, or None for anything we cannot read.

    None means "no opinion", never "mismatch". A check that raised on a
    version string it did not recognise would turn every unusual install into
    a false alarm, which is how a warning trains people to skip it.
    """
    if not v:
        return None
    try:
        return Version(str(v).strip().lstrip("v"))
    except InvalidVersion:
        return None


def installed_cli_version() -> str | None:
    """The cp-engine version this interpreter is running.

    `importlib.metadata` first — the same read `mcp_server.py` uses for its
    on-disk check (#150), so the two cannot disagree about what is installed —
    then `__version__` as the fallback for a source checkout with no dist.
    """
    try:
        import importlib.metadata as md

        return md.version("cp-engine")
    except Exception:  # noqa: BLE001 — a broken dist is a finding for elsewhere
        try:
            from cp_engine import __version__

            return __version__
        except Exception:  # noqa: BLE001
            return None


# ── plugin_vs_cli ─────────────────────────────────────────────────────────

PLUGIN_KEY = "cp-engine@cp-engine"
REPO_URL = "https://github.com/FirstPersonSF/cp-engine.git"


@dataclass(frozen=True)
class PluginInstall:
    version: str
    scope: str
    project_path: str | None = None


def read_installed_plugins(path: Path) -> list[PluginInstall]:
    """Every cp-engine entry in `installed_plugins.json`, every scope.

    EVERY entry, deliberately. Drew's machine carried a user-scope 0.120.2 and
    a project-scope 0.40.0 that nobody knew about; a reader that stopped at
    the first entry would have called that machine healthy. The registry is a
    claim about what is registered, and each registration can drift on its
    own.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    entries = (data.get("plugins") or {}).get(PLUGIN_KEY) or []
    out: list[PluginInstall] = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("version"):
            continue
        out.append(
            PluginInstall(
                version=str(e["version"]),
                scope=str(e.get("scope") or "user"),
                project_path=e.get("projectPath"),
            )
        )
    return out


def default_installed_plugins_path() -> Path:
    return Path.home() / ".claude" / "plugins" / "installed_plugins.json"


def default_receipt_path() -> Path:
    base = os.environ.get("UV_TOOL_DIR")
    root = Path(base) if base else Path.home() / ".local" / "share" / "uv" / "tools"
    return root / "cp-engine" / "uv-receipt.toml"


def read_receipt_source(path: Path) -> tuple[str, str] | None:
    """`("git", url)` or `("directory", path)` from `uv-receipt.toml`, else None.

    Tony's receipt pins an exact git rev (`?rev=v0.119.0`); Drew's points at a
    local clone. They have different upgrade commands, and `uv tool upgrade`
    is a silent no-op on the first — so the remedy must be built from what the
    receipt actually says, not from a guess.
    """
    try:
        import tomllib

        data = tomllib.loads(path.read_text())
    except (OSError, ValueError):
        return None
    for req in (data.get("tool") or {}).get("requirements") or []:
        if not isinstance(req, dict) or req.get("name") != "cp-engine":
            continue
        if req.get("git"):
            url = str(req["git"]).split("?", 1)[0]
            return ("git", url)
        if req.get("directory"):
            return ("directory", str(req["directory"]))
    return None


def cli_reinstall_command(target_version: str, source: tuple[str, str] | None) -> str:
    """The `uv tool install` line that actually moves the CLI.

    `--force --reinstall`, never bare `--force`: bare `--force` skips the
    reinstall when the version string looks unchanged, and `uv tool upgrade`
    does nothing when the receipt pins an exact rev. Both report success.
    """
    if source and source[0] == "directory":
        return f'uv tool install --force --reinstall --from "{source[1]}" cp-engine'
    url = source[1] if source and source[0] == "git" else REPO_URL
    return f'uv tool install --force --reinstall --from "git+{url}@v{target_version}" cp-engine'


PLUGIN_UPDATE_COMMAND = "claude plugin update cp-engine@cp-engine"


def plugin_vs_cli(
    plugins: Iterable[PluginInstall],
    cli_version: str | None,
    receipt_source: tuple[str, str] | None = None,
) -> Finding | None:
    """The incident's check: do the plugin and the CLI agree?

    DIRECTION-AGNOSTIC, and this is the whole point. The plugin hook's
    no-downgrade guard treats "installed is ahead" as healthy — correct for its
    job of not downgrading off a stale marketplace clone, wrong as a detector,
    because CLI-ahead is exactly the direction the incident ran. Any mismatch
    is a finding. The remedy follows the direction: the side that is behind is
    the side to move.

    The one-line wording is written for the agent as much as the human: the
    session can run either remedy, but it has already loaded whichever half is
    stale, so the line says to restart. Without that the user does the thing
    and continues on stale code — the shape being fixed.
    """
    cli = parse_version(cli_version)
    if cli is None:
        return None

    behind_plugin: list[PluginInstall] = []
    behind_cli: list[PluginInstall] = []
    for p in plugins:
        pv = parse_version(p.version)
        if pv is None or pv == cli:
            continue
        (behind_plugin if pv < cli else behind_cli).append(p)

    if not behind_plugin and not behind_cli:
        return None

    def _label(p: PluginInstall) -> str:
        where = f" for {p.project_path}" if p.project_path else ""
        return f"plugin v{p.version} ({p.scope}{where})"

    if behind_plugin:
        # The incident direction. The plugin (skills, slash commands) is
        # behind the engine it calls.
        worst = min(behind_plugin, key=lambda p: parse_version(p.version) or cli)
        return Finding(
            severity=SEV_PLUGIN_VS_CLI,
            code="plugin_vs_cli",
            summary=(
                "Your cp tools are out of sync with each other and may save "
                "bad data."
            ),
            remedy=(
                'Say "update cp-engine" and this session will update them — '
                "then restart Claude Code to pick it up."
            ),
            detail=f"{_label(worst)}, engine v{cli_version}",
            extra={
                "direction": "plugin_behind",
                "command": PLUGIN_UPDATE_COMMAND,
                "plugins": [_label(p) for p in behind_plugin],
            },
        )

    # Plugin ahead of the CLI: the engine is the stale half.
    worst = max(behind_cli, key=lambda p: parse_version(p.version) or cli)
    return Finding(
        severity=SEV_PLUGIN_VS_CLI,
        code="plugin_vs_cli",
        summary=(
            "Your cp tools are out of sync with each other and may save bad "
            "data."
        ),
        remedy=(
            'Say "update cp-engine" and this session will update them — then '
            "restart Claude Code (and /mcp) to pick it up."
        ),
        detail=f"{_label(worst)}, engine v{cli_version}",
        extra={
            "direction": "cli_behind",
            "command": cli_reinstall_command(worst.version, receipt_source),
            "plugins": [_label(p) for p in behind_cli],
        },
    )


# ── pin_floor ─────────────────────────────────────────────────────────────


def find_tenant_root(start: Path | None = None) -> Path | None:
    """Walk up from `start` (default cwd) for `.cp-engine.toml`."""
    d = (start or Path.cwd()).resolve()
    for cand in (d, *d.parents):
        if (cand / ".cp-engine.toml").is_file():
            return cand
    return None


def read_pin(root: Path) -> str | None:
    """`[engine].version` from the tenant's committed config, or None."""
    try:
        import tomllib

        data = tomllib.loads((root / ".cp-engine.toml").read_text())
    except (OSError, ValueError):
        return None
    v = (data.get("engine") or {}).get("version")
    return v if isinstance(v, str) and v else None


def pin_floor_version(constraint: str | None) -> Version | None:
    """The lowest version a constraint admits, or None if unreadable.

    `~= 0.42` admits 0.42.0 upward, so its floor is 0.42. For a compound
    constraint the floor is the highest lower bound among its clauses.
    """
    if not constraint:
        return None
    try:
        spec = SpecifierSet(constraint)
    except InvalidSpecifier:
        return None
    floors = []
    for s in spec:
        if s.operator in ("~=", ">=", "==", "==="):
            v = parse_version(s.version.rstrip(".*"))
            if v is not None:
                floors.append(v)
    return max(floors) if floors else None


def _minor(v: Version) -> tuple[int, int]:
    return (v.major, v.minor)


def pin_floor(constraint: str | None, cli_version: str | None) -> Finding | None:
    """Is the tenant pin capable of failing?

    THE INCIDENT'S CHECK (plan §1, corrected timeline). For thirteen days both
    tracks sat at 0.108.1 while the tenant pin read `~= 0.42` — a floor
    seventy-eight releases down, satisfied by everything since June. The
    tenant hook asked "is this allowed?" and the pin said yes every session.
    Nothing asked "is this current?" This does.

    A pin whose floor is below the running engine's minor cannot fail for an
    install as old as the floor. `cxp sync` raises the floor automatically
    (`raise_pin_floor`), so in steady state this fires only when sync has not
    run since a release — which is itself worth knowing. An engine BEHIND the
    pin is not this check's business; that is EngineVersionMismatch's, and it
    already raises.
    """
    cli = parse_version(cli_version)
    floor = pin_floor_version(constraint)
    if cli is None or floor is None:
        return None
    if _minor(floor) >= _minor(cli):
        return None
    return Finding(
        severity=SEV_PIN_FLOOR,
        code="pin_floor",
        summary=(
            "This project's cp pin is behind the engine you're running, so an "
            "out-of-date install would pass its check."
        ),
        remedy="Run cxp sync to raise it, or say \"update the cp pin\".",
        detail=f"pin {constraint}, engine v{cli_version}",
        extra={"pin": constraint, "floor": str(floor), "target": f"~= {cli.major}.{cli.minor}"},
    )


def raise_pin_floor(toml_text: str, cli_version: str | None) -> tuple[str, str | None, str | None]:
    """Rewrite `[engine].version` to `~= <engine minor>` if that is HIGHER.

    Returns `(new_text, old_pin, new_pin)`; `new_pin` is None when nothing
    changed. Formatting and comments are preserved (tomlkit). Rules:

      * Never lowers. A pin ahead of the running engine is left alone — that
        engine is the one that needs to move.
      * Only a single `~=` clause is rewritten. A hand-authored compound
        constraint (`>= 0.5, < 1`) is someone's deliberate shape; not ours to
        collapse.
      * `[engine] version_lock = true` opts a tenant out entirely — the escape
        hatch for a tenant that genuinely must hold, stated where the pin is.

    WHY SYNC AND NOT THE RELEASE SCRIPT. `release.py` cannot reach the
    tenant: it is another repository, on a clone it cannot assume exists. The
    pin was bumped by hand for ~20 releases, then not for 80. Sync runs with a
    known engine version, in the tenant, and its output is already committed —
    so the floor moves for free, in the same commit as everything else.
    """
    import tomlkit

    cli = parse_version(cli_version)
    if cli is None:
        return toml_text, None, None
    try:
        doc = tomlkit.parse(toml_text)
    except Exception:  # noqa: BLE001 — unreadable config is not ours to rewrite
        return toml_text, None, None
    engine = doc.get("engine")
    if engine is None:
        return toml_text, None, None
    old = engine.get("version")
    if not isinstance(old, str) or not old:
        return toml_text, None, None
    if engine.get("version_lock"):
        return toml_text, old, None
    if not re.fullmatch(r"\s*~=\s*\d+\.\d+(\.\d+)?\s*", old):
        return toml_text, old, None
    floor = pin_floor_version(old)
    if floor is None or _minor(floor) >= _minor(cli):
        return toml_text, old, None
    new = f"~= {cli.major}.{cli.minor}"
    engine["version"] = new
    return tomlkit.dumps(doc), old, new


# ── stale_mcp ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Proc:
    pid: int
    started: float  # epoch seconds
    command: str


_LSTART_FMT = "%a %b %d %H:%M:%S %Y"


def parse_ps(output: str) -> list[Proc]:
    """Rows of `ps -eo pid,lstart,command` into Procs. Unparseable rows skip."""
    procs: list[Proc] = []
    for line in output.splitlines():
        m = re.match(r"^\s*(\d+)\s+(\w{3}\s+\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+(.*)$", line)
        if not m:
            continue
        try:
            started = time.mktime(time.strptime(m.group(2), _LSTART_FMT))
        except ValueError:
            continue
        procs.append(Proc(pid=int(m.group(1)), started=started, command=m.group(3)))
    return procs


def gather_procs() -> list[Proc]:
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,lstart,command"], capture_output=True, text=True, timeout=10
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return []
    return parse_ps(out.stdout) if out.returncode == 0 else []


def is_cxp_mcp(command: str) -> bool:
    # `.../bin/cxp mcp` — the stdio server the tenant's .mcp.json launches.
    return bool(re.search(r"(^|/|\s)cxp\s+mcp(\s|$)", command))


def stale_mcp(procs: Iterable[Proc], install_mtime: float | None) -> Finding | None:
    """`cxp mcp` processes older than the install they should be serving.

    Reproduced on both audited machines. A `cxp mcp` server keeps serving the
    bytecode it started with; after a reinstall it answers tool calls from old
    code, normally, with nothing to say so. `mcp_server.py` warns about the
    same condition from INSIDE tool results (#150) — reactive, so it is seen
    only once something is already being asked. This is the proactive read:
    start time before install mtime means stale, no round-trip needed.

    Never kills. A running server belongs to a live session, possibly someone
    else's. It names the PIDs and the restart.
    """
    if install_mtime is None:
        return None
    stale = [p for p in procs if is_cxp_mcp(p.command) and p.started < install_mtime]
    if not stale:
        return None
    pids = ", ".join(str(p.pid) for p in sorted(stale, key=lambda p: p.started))
    n = len(stale)
    return Finding(
        severity=SEV_STALE_MCP,
        code="stale_mcp",
        summary=(
            f"{n} cp tool server{'s' if n != 1 else ''} started before the last "
            "update and may answer with old code."
        ),
        remedy="Run /mcp to restart them.",
        detail=f"pid {pids}",
        extra={"pids": [p.pid for p in stale]},
    )


def receipt_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


# ── collect + render ──────────────────────────────────────────────────────


def collect(
    *,
    installed_plugins_path: Path | None = None,
    receipt_path: Path | None = None,
    cli_version: str | None = None,
    procs: Iterable[Proc] | None = None,
    tenant_root: Path | None = None,
) -> list[Finding]:
    """Run every check against the real system (or injected substitutes).

    Sorted most-severe first. Each argument defaults to the live read; tests
    pass substitutes so a finding can be pinned to an exact state.
    """
    ip = installed_plugins_path or default_installed_plugins_path()
    rp = receipt_path or default_receipt_path()
    cli = cli_version if cli_version is not None else installed_cli_version()
    ps = list(procs) if procs is not None else gather_procs()

    root = tenant_root if tenant_root is not None else find_tenant_root()
    findings = [
        plugin_vs_cli(read_installed_plugins(ip), cli, read_receipt_source(rp)),
        pin_floor(read_pin(root), cli) if root else None,
        stale_mcp(ps, receipt_mtime(rp)),
    ]
    return sorted((f for f in findings if f is not None), key=lambda f: f.severity)


def brief(findings: list[Finding]) -> str:
    """The one SessionStart line. Empty when healthy — silence IS the signal.

    A line that appears only when something is wrong is alarming by
    construction; one that always prints stops being read. Highest-severity
    finding, then a count of the rest that points at the full report.
    """
    if not findings:
        return ""
    top = findings[0]
    more = len(findings) - 1
    tail = f"  (+{more} more: cxp doctor)" if more else ""
    lines = [f"[cp] {top.summary} {top.remedy}{tail}"]
    if top.detail:
        lines.append(f"     ({top.detail})")
    return "\n".join(lines)


def report(findings: list[Finding]) -> str:
    """The full `cxp doctor` text: every finding, with its command."""
    if not findings:
        return "cp install: healthy."
    out = [f"cp install: {len(findings)} finding(s)", ""]
    for f in findings:
        out.append(f"⚠ {f.code} — {f.summary}")
        out.append(f"    {f.remedy}")
        if f.detail:
            out.append(f"    ({f.detail})")
        cmd = f.extra.get("command")
        if cmd:
            out.append(f"    command: {cmd}")
        for p in f.extra.get("plugins", [])[1:]:
            out.append(f"    also: {p}")
        out.append("")
    return "\n".join(out).rstrip()
