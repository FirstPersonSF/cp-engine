"""Resolve a tenant's `[engine].version` pin to a concrete git tag.

Used by the GitHub Actions runner so that bumping a tenant's engine pin
in `.cp-engine.toml` is sufficient to pick up the new version on the
next sync — no workflow file edit per release.

The resolver is intentionally narrow: read the constraint, list the
engine repo's `v*` tags, return the highest tag whose version satisfies
the constraint. No PyPI, no caching, no auth (relies on
`git ls-remote` being able to read the public repo).
"""

from __future__ import annotations

import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

ENGINE_REPO_URL = "https://github.com/FirstPersonSF/cp-engine.git"
COMMITTED_FILENAME = ".cp-engine.toml"


class PinResolutionError(RuntimeError):
    """Raised when the engine pin cannot be resolved to a tag."""


@dataclass(frozen=True)
class PinResolution:
    constraint: str
    tag: str
    version: Version


def read_constraint(tenant_root: Path) -> str:
    """Return the `[engine].version` string from the tenant's committed config."""
    path = tenant_root / COMMITTED_FILENAME
    if not path.exists():
        raise PinResolutionError(f"No {COMMITTED_FILENAME} at {tenant_root}.")
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    engine = data.get("engine") or {}
    constraint = engine.get("version")
    if not isinstance(constraint, str) or not constraint:
        raise PinResolutionError(f"{path}: [engine].version is required.")
    return constraint


def list_remote_tags(repo_url: str = ENGINE_REPO_URL) -> tuple[str, ...]:
    """Return all `v*` tags on the engine repo, newest-first by version sort.

    Uses `git ls-remote --tags` so no clone is needed. Filters to tags
    matching `v<X>.<Y>.<Z>` to skip annotated-tag refs and any non-version
    tags that might exist.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", repo_url],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise PinResolutionError(f"Failed to list remote tags from {repo_url}: {exc}") from exc

    tags: list[tuple[Version, str]] = []
    for line in result.stdout.splitlines():
        ref = line.split("\t", 1)[-1]
        if not ref.startswith("refs/tags/v"):
            continue
        tag = ref[len("refs/tags/") :]
        try:
            v = Version(tag.lstrip("v"))
        except InvalidVersion:
            continue
        tags.append((v, tag))

    tags.sort(key=lambda x: x[0], reverse=True)
    return tuple(tag for _, tag in tags)


def resolve(constraint: str, tags: tuple[str, ...]) -> PinResolution:
    """Pick the highest `tags` entry whose version satisfies `constraint`."""
    try:
        spec = SpecifierSet(constraint)
    except InvalidSpecifier as exc:
        raise PinResolutionError(f"Invalid constraint {constraint!r}: {exc}") from exc

    for tag in tags:
        try:
            v = Version(tag.lstrip("v"))
        except InvalidVersion:
            continue
        if v in spec:
            return PinResolution(constraint=constraint, tag=tag, version=v)

    raise PinResolutionError(
        f"No tag matches constraint {constraint!r}. Available: {', '.join(tags[:5])}..."
    )


def resolve_for_tenant(tenant_root: Path, repo_url: str = ENGINE_REPO_URL) -> PinResolution:
    """End-to-end: read constraint from tenant, list tags, return resolution."""
    constraint = read_constraint(tenant_root)
    tags = list_remote_tags(repo_url)
    if not tags:
        raise PinResolutionError(f"No version tags found at {repo_url}.")
    return resolve(constraint, tags)
