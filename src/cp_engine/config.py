"""Tenant configuration — merges committed `.cp-engine.toml` with the
gitignored `.cp-engine.local.toml`.

See spec v02 §5 for the schema. The committed file is the source of truth
for *which projects exist* (project list, GitHub coordinates, sync backend);
the local file is the source of truth for *where they live on this machine*.

Fail-loud semantics: silent skipping is a primary failure mode this module
prevents. A project listed in the committed file but missing from local
config raises `ProjectsMissingFromLocal`; a configured local path that
doesn't exist on disk raises `LocalPathNotFound`.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from cp_engine import __version__ as ENGINE_VERSION

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
#  Errors
# ──────────────────────────────────────────────────────────────────────


class ConfigError(Exception):
    """Base class for all configuration errors."""


class CommittedConfigMissing(ConfigError):
    """`.cp-engine.toml` doesn't exist at the tenant root."""


class LocalConfigMissing(ConfigError):
    """`.cp-engine.local.toml` doesn't exist; user needs to run `cp init`."""


class CommittedConfigInvalid(ConfigError):
    """`.cp-engine.toml` is missing a required section or field."""


class LocalConfigInvalid(ConfigError):
    """`.cp-engine.local.toml` is malformed."""


class ProjectsMissingFromLocal(ConfigError):
    """One or more committed projects have no entry in local config."""

    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        super().__init__(
            "Missing local repo paths for: "
            + ", ".join(missing)
            + ". Run `cp init` to configure, or edit .cp-engine.local.toml directly."
        )


class LocalPathNotFound(ConfigError):
    """A configured local path doesn't exist on disk."""

    def __init__(self, code: str, path: Path) -> None:
        self.code = code
        self.path = path
        super().__init__(f"Local path for project '{code}' does not exist: {path}")


class EngineVersionMismatch(ConfigError):
    """Installed cp-engine version doesn't satisfy the tenant's pin."""

    def __init__(self, installed: str, required: str) -> None:
        self.installed = installed
        self.required = required
        super().__init__(
            f"Installed cp-engine {installed} does not satisfy the tenant's "
            f"engine pin '{required}'.\n\n"
            "Fix:\n"
            "  - System-wide install (recommended for daily use across repos):\n"
            "      uv tool install --force --from <path-to-cp-engine-repo> cp-engine\n"
            "  - Project-local install (cp-engine repo dev):\n"
            "      uv pip install -e <path-to-cp-engine-repo>\n\n"
            "Or update the engine pin in .cp-engine.toml if intentional."
        )


# ──────────────────────────────────────────────────────────────────────
#  Data shapes
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProjectConfig:
    """One project tracked by a tenant.

    `local_path` is None when the user *intentionally skipped* the project
    in their local config (`code = ""`). Brandon doesn't have access to
    `mc-2`, for instance — he should be able to skip it cleanly without
    breaking the engine.
    """

    code: str
    github: str  # "owner/repo"
    local_path: Path | None


@dataclass(frozen=True)
class SyncConfig:
    """How the tenant syncs source-of-truth state into engine-managed regions."""

    backend: str  # "mc-2" or "github-issues"
    cron: str
    mc_2_supabase_project_ref: str | None = None


@dataclass(frozen=True)
class TenantConfig:
    """Merged view of a tenant's committed + local configuration.

    `local_repos` (v0.4+) maps repo name → absolute local clone path. It's
    a per-machine extension of the local file with NO required overlap with
    `[[projects]]` — it can name `cp-engine` itself, `1p-component-library`,
    or any repo the user wants Claude to be able to traverse without
    network calls. Empty when the section is absent.
    """

    name: str
    display: str
    engine_version_constraint: str
    sync: SyncConfig
    projects: tuple[ProjectConfig, ...]
    root: Path  # absolute path to the tenant repo
    local_repos: Mapping[str, Path] = field(
        default_factory=lambda: MappingProxyType({})
    )


# ──────────────────────────────────────────────────────────────────────
#  Public API
# ──────────────────────────────────────────────────────────────────────


COMMITTED_FILENAME = ".cp-engine.toml"
LOCAL_FILENAME = ".cp-engine.local.toml"


def load(tenant_root: Path) -> TenantConfig:
    """Load and merge `.cp-engine.toml` (committed) with `.cp-engine.local.toml`.

    Raises one of the `ConfigError` subclasses on any failure. Never returns
    a partial config — silent skipping would defeat the file split's purpose.
    """
    tenant_root = tenant_root.resolve()
    committed = _load_committed(tenant_root)
    local = _load_local(tenant_root, committed_has_projects=bool(committed["projects"]))

    _enforce_engine_version(committed["engine_version_constraint"])

    projects = _merge_projects(committed["projects"], local["repos"])

    return TenantConfig(
        name=committed["name"],
        display=committed["display"],
        engine_version_constraint=committed["engine_version_constraint"],
        sync=committed["sync"],
        projects=projects,
        root=tenant_root,
        local_repos=MappingProxyType(dict(local["local_repos"])),
    )


# ──────────────────────────────────────────────────────────────────────
#  Internals
# ──────────────────────────────────────────────────────────────────────


def _load_committed(tenant_root: Path) -> dict:
    path = tenant_root / COMMITTED_FILENAME
    if not path.exists():
        raise CommittedConfigMissing(
            f"No {COMMITTED_FILENAME} at {tenant_root}. Not a tenant repo?"
        )

    with path.open("rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise CommittedConfigInvalid(f"Failed to parse {path}: {exc}") from exc

    return _normalize_committed(data, path)


def _normalize_committed(data: dict, source: Path) -> dict:
    tenant = data.get("tenant")
    if tenant is None or not isinstance(tenant, dict):
        raise CommittedConfigInvalid(f"{source}: missing [tenant] section")

    name = tenant.get("name")
    if not isinstance(name, str) or not name:
        raise CommittedConfigInvalid(f"{source}: [tenant].name is required (non-empty string)")

    # display is optional — fall back to a title-cased name. Worst case is a
    # slightly ugly heading in master-cp.md, trivially overridable.
    display = tenant.get("display") or name.replace("-", " ").title()

    engine = data.get("engine")
    if engine is None or not isinstance(engine, dict):
        raise CommittedConfigInvalid(f"{source}: missing [engine] section")
    engine_version = engine.get("version")
    if not isinstance(engine_version, str) or not engine_version:
        raise CommittedConfigInvalid(
            f"{source}: [engine].version is required (e.g. \"~= 0.1\")"
        )

    sync_raw = data.get("sync")
    if sync_raw is None or not isinstance(sync_raw, dict):
        raise CommittedConfigInvalid(f"{source}: missing [sync] section")
    backend = sync_raw.get("backend")
    if backend not in ("mc-2", "github-issues"):
        raise CommittedConfigInvalid(
            f"{source}: [sync].backend must be one of: 'mc-2', 'github-issues' "
            f"(got: {backend!r})"
        )
    cron = sync_raw.get("cron", "0 * * * *")
    mc_2_ref: str | None = None
    if backend == "mc-2":
        mc_2_block = sync_raw.get("mc_2") or {}
        mc_2_ref = mc_2_block.get("supabase_project_ref")
        if not mc_2_ref:
            raise CommittedConfigInvalid(
                f"{source}: [sync.mc_2].supabase_project_ref is required when backend = 'mc-2'"
            )

    sync = SyncConfig(
        backend=backend,
        cron=cron,
        mc_2_supabase_project_ref=mc_2_ref,
    )

    projects_raw = data.get("projects") or []
    if not isinstance(projects_raw, list):
        raise CommittedConfigInvalid(f"{source}: [[projects]] must be a list of tables")

    projects: list[dict] = []
    seen_codes: set[str] = set()
    for i, p in enumerate(projects_raw):
        if not isinstance(p, dict):
            raise CommittedConfigInvalid(f"{source}: [[projects]][{i}] must be a table")
        code = p.get("code")
        github = p.get("github")
        if not isinstance(code, str) or not code:
            raise CommittedConfigInvalid(
                f"{source}: [[projects]][{i}].code is required (non-empty string)"
            )
        if not isinstance(github, str) or "/" not in github:
            raise CommittedConfigInvalid(
                f"{source}: [[projects]][{i}].github must be 'owner/repo' (got: {github!r})"
            )
        if code in seen_codes:
            raise CommittedConfigInvalid(f"{source}: duplicate project code '{code}'")
        seen_codes.add(code)
        projects.append({"code": code, "github": github})

    return {
        "name": name,
        "display": display,
        "engine_version_constraint": engine_version,
        "sync": sync,
        "projects": projects,
    }


def _load_local(tenant_root: Path, *, committed_has_projects: bool) -> dict:
    """Load `.cp-engine.local.toml`.

    If the committed config lists no projects, the local file has nothing
    to map and is treated as optional — missing local file → empty repos.
    This is the common case in CI runners (gitignored local file plus a
    tenant whose mc-2 backend reads its project list from MC-2 directly).

    If the committed config DOES list projects, the local file is required
    so we can fail loudly when a path is missing — silent skipping is the
    failure mode the file split exists to prevent.
    """
    path = tenant_root / LOCAL_FILENAME
    if not path.exists():
        if not committed_has_projects:
            return {"repos": {}, "local_repos": {}}
        raise LocalConfigMissing(
            f"No {LOCAL_FILENAME} at {tenant_root}. Run `cp init` to configure."
        )

    with path.open("rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise LocalConfigInvalid(f"Failed to parse {path}: {exc}") from exc

    repos = data.get("repos") or {}
    if not isinstance(repos, dict):
        raise LocalConfigInvalid(f"{path}: [repos] must be a table")
    for code, raw_path in repos.items():
        if not isinstance(raw_path, str):
            raise LocalConfigInvalid(
                f"{path}: [repos].{code} must be a string path or empty string for skip"
            )

    local_repos = _parse_local_repos(data, path)

    return {"repos": repos, "local_repos": local_repos}


def _parse_local_repos(data: dict, source: Path) -> dict[str, Path]:
    """Parse the optional `[local-repos]` table into repo-name → resolved Path.

    The section is keyed by GitHub repo name (e.g. "mc-2", "cp-engine") rather
    than project code, so it can name repos that aren't tracked as committed
    projects. Empty string is not a valid value here — if you don't have the
    repo locally, omit the entry. Bad paths fail loudly because the only
    callers (cp link-local, /cp-summarize self-heal) need them to resolve.
    """
    raw = data.get("local-repos")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise LocalConfigInvalid(f"{source}: [local-repos] must be a table")

    resolved: dict[str, Path] = {}
    for repo_name, raw_path in raw.items():
        if not isinstance(raw_path, str) or not raw_path:
            raise LocalConfigInvalid(
                f"{source}: [local-repos].{repo_name!r} must be a non-empty string path"
            )
        expanded = Path(raw_path).expanduser()
        try:
            resolved_path = expanded.resolve(strict=True)
        except FileNotFoundError as exc:
            raise LocalPathNotFound(code=repo_name, path=expanded) from exc
        resolved[repo_name] = resolved_path
    return resolved


def _merge_projects(
    committed: list[dict],
    local_repos: dict[str, str],
) -> tuple[ProjectConfig, ...]:
    committed_codes = {p["code"] for p in committed}
    local_codes = set(local_repos.keys())

    # Drift: paths in local for projects not in committed. Warn (project may
    # have been removed from the tenant); don't fail.
    for orphan in sorted(local_codes - committed_codes):
        logger.warning(
            "Local config has path for unknown project %r (not in %s); ignoring.",
            orphan,
            COMMITTED_FILENAME,
        )

    # Hard fail: projects in committed missing from local entirely.
    missing = tuple(sorted(committed_codes - local_codes))
    if missing:
        raise ProjectsMissingFromLocal(missing)

    merged: list[ProjectConfig] = []
    for p in committed:
        code = p["code"]
        raw_path = local_repos[code]  # guaranteed present after the check above

        if raw_path == "":
            # Intentionally skipped (e.g. Brandon doesn't have access to mc-2).
            local_path: Path | None = None
        else:
            expanded = Path(raw_path).expanduser()
            try:
                resolved = expanded.resolve(strict=True)
            except FileNotFoundError as exc:
                raise LocalPathNotFound(code=code, path=expanded) from exc
            local_path = resolved

        merged.append(
            ProjectConfig(code=code, github=p["github"], local_path=local_path)
        )

    return tuple(merged)


def enforce_engine_version_for_tenant(tenant_root: Path) -> None:
    """Read `<tenant_root>/.cp-engine.toml`'s engine pin and verify the
    installed cp-engine version satisfies it. Raises `EngineVersionMismatch`
    on stale installs.

    Lighter-weight than `load()` — used by commands (capture-session) that
    don't need the full merged config but still must fail loudly on a
    stale cp-engine binary, which would otherwise produce wrong output
    against a newer-pinned tenant.
    """
    path = tenant_root / COMMITTED_FILENAME
    if not path.exists():
        raise CommittedConfigMissing(
            f"No {COMMITTED_FILENAME} at {tenant_root}. Not a tenant repo?"
        )
    with path.open("rb") as fh:
        try:
            data = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise CommittedConfigInvalid(f"Failed to parse {path}: {exc}") from exc
    engine = data.get("engine") or {}
    constraint = engine.get("version")
    if not isinstance(constraint, str) or not constraint:
        raise CommittedConfigInvalid(
            f"{path}: [engine].version is required (e.g. \"~= 0.1\")"
        )
    _enforce_engine_version(constraint)


def _enforce_engine_version(constraint: str) -> None:
    try:
        spec = SpecifierSet(constraint)
    except InvalidSpecifier as exc:
        raise CommittedConfigInvalid(
            f"Invalid [engine].version constraint {constraint!r}: {exc}"
        ) from exc

    try:
        installed = Version(ENGINE_VERSION)
    except InvalidVersion as exc:
        # Defensive: should never happen because we control __version__.
        raise ConfigError(f"cp-engine reports invalid version {ENGINE_VERSION!r}") from exc

    if installed not in spec:
        raise EngineVersionMismatch(installed=ENGINE_VERSION, required=constraint)
