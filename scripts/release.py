#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["packaging>=24.0", "tomlkit>=0.13", "build>=1.0"]
# ///
"""Release a new cp-engine version.

Bumps the version in `pyproject.toml`, `plugin/plugin.json`,
`.claude-plugin/marketplace.json`, runs the test suite, builds the
package, commits, tags, and pushes.

`pyproject.toml`'s `[project].version` is the canonical source of truth;
the other files mirror it. `src/cp_engine/__init__.py`'s `__version__`
string is bumped here too — reading it from `importlib.metadata` was
considered but rejected because Python 3.14 silently returns `None` for
broken or missing metadata, which collides with the version-check logic
in `config.py`.

Usage
-----
    scripts/release.py 0.6.0
    scripts/release.py 0.6.0 --dry-run
    scripts/release.py 0.6.0 --skip-tests       # not recommended

Pre-flight checks (all must pass before any file is touched):
- Working tree is clean.
- Current branch is `main`.
- New version is a valid PEP 440 release version.
- New version is strictly greater than current.
- `CHANGELOG.md` has a `## v<new-version>` section already drafted.
- The git tag `v<new-version>` does not already exist.

On any failure the script exits non-zero and leaves the tree untouched.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import tomlkit
from packaging.version import InvalidVersion, Version

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
PLUGIN_JSON = REPO_ROOT / "plugin" / "plugin.json"
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"
INIT_PY = REPO_ROOT / "src" / "cp_engine" / "__init__.py"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

VERSION_LINE_RE = re.compile(r'^__version__ = "[^"]+"\s*$', re.MULTILINE)


class ReleaseError(RuntimeError):
    """Raised on any pre-flight or step failure."""


def run(cmd: list[str], *, capture: bool = False, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        check=check,
        capture_output=capture,
        text=True,
    )


def read_current_version() -> str:
    doc = tomlkit.parse(PYPROJECT.read_text())
    return str(doc["project"]["version"])


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Release a new cp-engine version.")
    p.add_argument("version", help="New version, e.g. 0.6.0")
    p.add_argument("--dry-run", action="store_true", help="Run pre-flight checks only.")
    p.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip pytest. Use only when tests have just passed in another way.",
    )
    p.add_argument(
        "--skip-build",
        action="store_true",
        help="Skip `python -m build`. Use only if `build` is unavailable.",
    )
    return p.parse_args()


def preflight(new: str) -> tuple[Version, Version]:
    try:
        new_v = Version(new)
    except InvalidVersion as e:
        raise ReleaseError(f"Not a valid version: {new!r} ({e})") from e

    current = read_current_version()
    cur_v = Version(current)

    if new_v <= cur_v:
        raise ReleaseError(f"New version {new_v} is not greater than current {cur_v}.")

    status = run(["git", "status", "--porcelain"], capture=True).stdout.strip()
    if status:
        raise ReleaseError(
            "Working tree is not clean. Commit or stash before releasing.\n" + status
        )

    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture=True).stdout.strip()
    if branch != "main":
        raise ReleaseError(f"Releases must be cut from `main`. Currently on `{branch}`.")

    changelog = CHANGELOG.read_text()
    if not re.search(rf"^## v{re.escape(new)} ", changelog, flags=re.MULTILINE):
        raise ReleaseError(
            f"CHANGELOG.md has no `## v{new} — <date>` section. "
            "Add release notes before releasing."
        )

    tag = f"v{new}"
    existing = run(["git", "tag", "--list", tag], capture=True).stdout.strip()
    if existing:
        raise ReleaseError(f"Tag {tag} already exists locally.")

    return cur_v, new_v


def bump_pyproject(new: str) -> None:
    doc = tomlkit.parse(PYPROJECT.read_text())
    doc["project"]["version"] = new
    PYPROJECT.write_text(tomlkit.dumps(doc))


def bump_init_py(new: str) -> None:
    """Update the `__version__ = "X"` line in src/cp_engine/__init__.py."""
    text = INIT_PY.read_text()
    if not VERSION_LINE_RE.search(text):
        raise ReleaseError(
            f"{INIT_PY} has no `__version__ = \"...\"` line to update."
        )
    new_text = VERSION_LINE_RE.sub(f'__version__ = "{new}"', text, count=1)
    INIT_PY.write_text(new_text)


def bump_json(path: Path, new: str, *, nested_keys: list[list[str]] | None = None) -> None:
    """Bump top-level "version" plus any nested version fields.

    `nested_keys` is a list of key-paths into the JSON, e.g.
    `[["plugins", 0, "version"]]`. Marketplace.json doesn't actually
    have nested version keys today, but the parameter is here so we
    can extend without rewriting.
    """
    doc = json.loads(path.read_text())
    doc["version"] = new
    for path_keys in nested_keys or []:
        cur = doc
        for k in path_keys[:-1]:
            cur = cur[k]
        cur[path_keys[-1]] = new
    path.write_text(json.dumps(doc, indent=2) + "\n")


def _first_sentence(text: str, max_len: int = 80) -> str:
    """Pull the first sentence (period + space) or trim to `max_len` chars."""
    match = re.search(r"\. ", text)
    if match and match.start() < max_len:
        return text[: match.start()].strip()
    if len(text) > max_len:
        return text[:max_len].rsplit(" ", 1)[0] + "..."
    return text.strip()


def changelog_oneline(new: str) -> str:
    """Pull the first non-empty body line under `## v<new>` for the commit summary."""
    lines = CHANGELOG.read_text().splitlines()
    pattern = re.compile(rf"^## v{re.escape(new)} ")
    in_section = False
    for line in lines:
        if pattern.match(line):
            in_section = True
            continue
        if in_section:
            stripped = line.strip()
            if not stripped:
                continue
            # `###` (subsection header) must be checked before `##`, since
            # `## v0.5.2` is also a `##` line that ends the current section.
            if stripped.startswith("###"):
                continue
            if stripped.startswith("##"):
                break
            if stripped.startswith("- "):
                stripped = stripped[2:]
            return _first_sentence(stripped)
    return f"v{new} release"


def main() -> int:
    args = parse_args()

    try:
        cur_v, new_v = preflight(args.version)
    except ReleaseError as e:
        print(f"[release] preflight failed: {e}", file=sys.stderr)
        return 1

    print(f"[release] {cur_v} -> {new_v}")

    if args.dry_run:
        print("[release] dry run; no changes made.")
        return 0

    print("[release] bumping version files...")
    bump_pyproject(args.version)
    bump_init_py(args.version)
    bump_json(PLUGIN_JSON, args.version)
    bump_json(MARKETPLACE_JSON, args.version)

    if not args.skip_tests:
        print("[release] running pytest...")
        try:
            run(["pytest"])
        except subprocess.CalledProcessError:
            raise SystemExit("[release] tests failed; aborting before commit.")

    if not args.skip_build:
        print("[release] building distribution...")
        try:
            run(["python", "-m", "build"])
        except subprocess.CalledProcessError:
            raise SystemExit("[release] build failed; aborting before commit.")

    summary = changelog_oneline(args.version)
    commit_msg = f"v{args.version}: {summary}"
    print(f"[release] committing: {commit_msg}")
    run(["git", "add", str(PYPROJECT), str(INIT_PY), str(PLUGIN_JSON), str(MARKETPLACE_JSON)])
    run(["git", "commit", "-m", commit_msg])

    tag = f"v{args.version}"
    print(f"[release] tagging {tag}...")
    run(["git", "tag", tag])

    print("[release] pushing main + tag...")
    run(["git", "push", "origin", "main"])
    run(["git", "push", "origin", tag])

    print(f"[release] done. v{args.version} released.")
    print(
        f"[release] update local CLI: "
        f"uv tool install --force --from {REPO_ROOT} cp-engine"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
