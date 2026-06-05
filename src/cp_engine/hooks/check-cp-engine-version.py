#!/usr/bin/env python3
"""SessionStart self-heal for the `cp` CLI version.

The `cp` CLI is a `uv tool`-installed binary, separate from the cp-engine
Claude Code plugin. It can silently drift behind the tenant's engine pin
(`.cp-engine.toml [engine].version`) — e.g. installed from a frozen git tag
while the local repo advances through several releases. `cp sync` then
refuses to run with a pin-mismatch error.

This hook runs on SessionStart, compares the installed `cp` version against
the tenant pin, and if they don't satisfy each other, reinstalls `cp` from
the local cp-engine clone (path from `.cp-engine.local.toml [local-repos]`).

Design notes:
- Reads from the local repo, NOT remote tags — so dev work is reflected
  immediately and there's no network call at session start. (The CI runner's
  pin_resolver.py resolves against remote tags; that's a different context.)
- Silent + non-blocking on every failure path. A version-check hook must
  NEVER stop a session from starting. It prints a one-line note to stderr
  (surfaced as hook context) and exits 0 regardless.
- No third-party imports at all: version-constraint checking is implemented
  inline so the hook runs under bare system python3 (homebrew's python3 has
  no `packaging`). Only `~=`, `>=`, `==` and exact pins are supported — the
  forms a tenant pin actually uses.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import tomllib
from pathlib import Path


def _note(msg: str) -> None:
    """Emit a single advisory line; never raises."""
    print(f"[cp-engine version check] {msg}", file=sys.stderr)


def _tenant_root() -> Path | None:
    """Walk up from CWD to find the dir holding .cp-engine.toml."""
    here = Path.cwd()
    for d in (here, *here.parents):
        if (d / ".cp-engine.toml").exists():
            return d
    return None


def _read_pin(root: Path) -> str | None:
    try:
        with (root / ".cp-engine.toml").open("rb") as fh:
            data = tomllib.load(fh)
        pin = (data.get("engine") or {}).get("version")
        return pin if isinstance(pin, str) and pin else None
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _read_repo_path(root: Path) -> Path | None:
    """cp-engine clone path from .cp-engine.local.toml [local-repos]."""
    local = root / ".cp-engine.local.toml"
    try:
        with local.open("rb") as fh:
            data = tomllib.load(fh)
        raw = (data.get("local-repos") or {}).get("cp-engine")
        if not raw:
            return None
        path = Path(raw).expanduser()
        return path if (path / "pyproject.toml").exists() else None
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _installed_version() -> str | None:
    try:
        out = subprocess.run(
            ["cp", "--version"], capture_output=True, text=True, timeout=10
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    # "cp, version 0.16.0" -> "0.16.0"
    parts = out.stdout.strip().split()
    return parts[-1] if parts else None


def _parse(v: str) -> tuple[int, ...] | None:
    """'0.16.0' -> (0, 16, 0). None if not a plain numeric version."""
    try:
        return tuple(int(p) for p in v.strip().lstrip("v").split("."))
    except (ValueError, AttributeError):
        return None


def _satisfies(installed: str, pin: str) -> bool | None:
    """Evaluate a PEP 440-style constraint without `packaging`.

    Supports the operators a tenant pin actually uses: `~=`, `>=`, `==`,
    and a bare version (treated as `==`). Returns None for anything it
    can't confidently evaluate, so callers stay silent rather than guess.
    """
    iv = _parse(installed)
    if iv is None:
        return None

    pin = pin.strip()
    for op in ("~=", ">=", "==", "<=", ">", "<"):
        if pin.startswith(op):
            spec = pin[len(op):].strip()
            sv = _parse(spec)
            if sv is None:
                return None
            if op == "~=":
                # ~= X.Y  ->  >= X.Y  AND  same X (== X.Y.* compatible release)
                # ~= X.Y.Z -> >= X.Y.Z AND same X.Y
                if len(sv) < 2:
                    return None  # ~= needs at least major.minor
                floor_ok = iv >= sv
                prefix_len = len(sv) - 1
                same_prefix = iv[:prefix_len] == sv[:prefix_len]
                return floor_ok and same_prefix
            if op == ">=":
                return iv >= sv
            if op == "<=":
                return iv <= sv
            if op == ">":
                return iv > sv
            if op == "<":
                return iv < sv
            if op == "==":
                # pad shorter side with zeros for compare
                n = max(len(iv), len(sv))
                return iv + (0,) * (n - len(iv)) == sv + (0,) * (n - len(sv))
    # bare version → exact match
    sv = _parse(pin)
    if sv is None:
        return None
    n = max(len(iv), len(sv))
    return iv + (0,) * (n - len(iv)) == sv + (0,) * (n - len(sv))


def _reinstall(repo: Path) -> bool:
    _note(f"reinstalling cp from {repo} ...")
    try:
        out = subprocess.run(
            ["uv", "tool", "install", "--force", "--from", str(repo), "cp-engine"],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        _note(f"reinstall failed to launch: {exc}")
        return False
    if out.returncode != 0:
        _note(f"reinstall errored: {out.stderr.strip()[:200]}")
        return False
    return True


def main() -> int:
    root = _tenant_root()
    if root is None:
        return 0  # not in a cp tenant; nothing to do

    pin = _read_pin(root)
    if pin is None:
        return 0  # no pin to check against

    installed = _installed_version()
    if installed is None:
        _note(
            "cp CLI not found or not runnable; skipping "
            "(run `cp sync` to see the install error)."
        )
        return 0

    ok = _satisfies(installed, pin)
    if ok is None:
        return 0  # can't evaluate (no packaging); stay silent, don't guess
    if ok:
        return 0  # healthy — the common path, fully silent

    repo = _read_repo_path(root)
    if repo is None:
        _note(
            f"cp {installed} does not satisfy pin '{pin}', and no local cp-engine "
            f"clone is configured in .cp-engine.local.toml. Reinstall manually."
        )
        return 0

    _note(f"cp {installed} does not satisfy pin '{pin}'; self-healing.")
    if _reinstall(repo):
        new = _installed_version()
        _note(f"cp now at {new}.")
    return 0


if __name__ == "__main__":
    # SessionStart hooks receive JSON on stdin; we don't need it, but drain it.
    with contextlib.suppress(Exception):
        sys.stdin.read()
    sys.exit(main())
