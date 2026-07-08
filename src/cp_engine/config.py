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

    `contacts` is an optional per-project list of free-form contact records
    (typically `{"name": "...", "role": "..."}`) declared in `.cp-engine.toml`.
    Surfaces on sprint scaffolds so the tenant doesn't have to retype them
    each week. Empty tuple when the block is absent.
    """

    code: str
    github: str  # "owner/repo"
    local_path: Path | None
    contacts: tuple[dict, ...] = ()


@dataclass(frozen=True)
class SyncConfig:
    """How the tenant syncs source-of-truth state into engine-managed regions."""

    backend: str  # "mc-2" or "github-issues"
    cron: str
    mc_2_supabase_project_ref: str | None = None


@dataclass(frozen=True)
class AttentionDigestConfig:
    """Lever 2 — daily attention digest configuration.

    All fields optional with sensible defaults. Empty `recipients` means
    `cp attention-digest --post-to-slack` will no-op (or fail with a
    clear message) — opt-in by adding Slack user IDs.
    """
    recipients: tuple[str, ...] = ()
    past_due_threshold_days: int = 7
    escalated_window_days: int = 7
    allocation_cap_hours: int = 50
    post_when_clear: bool = True


@dataclass(frozen=True)
class DatesLoopConfig:
    """Weekly Slack dates loop configuration (commitments consolidation).

    `partners_channel` is the tenant-wide rollup channel id; None means
    the rollup post is skipped (per-project posts still go out).
    """
    partners_channel: str | None = None
    window_days: int = 14


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
    # Per-machine: gitignored, drives `cp link-local` and capture-session
    # self-healing. Empty by default.
    local_repos: Mapping[str, Path] = field(
        default_factory=lambda: MappingProxyType({})
    )
    # Multi-user committed: surfaces in rendered `_repo.md`. Outer key is
    # user (free-form, e.g. "drew"); inner is repo-name → path string.
    # Paths are NOT resolved (the runner doesn't have these on disk; they're
    # purely for display). Empty by default.
    local_repos_by_user: Mapping[str, Mapping[str, str]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    # Categories used to classify Risks parsed out of sprint files. Tenants
    # can override via `[risk_categories]\nvalues = [...]` in `.cp-engine.toml`.
    # The default covers the most common axes; bespoke tenants (e.g. ops-heavy
    # engagements) may want a narrower or wider set.
    risk_categories: tuple[str, ...] = (
        "contract",
        "pricing",
        "people",
        "technical",
        "scope",
        "timeline",
    )
    # Internal team members. Surfaces in plan_from_transcript so Claude
    # doesn't auto-add Drew/Tony/etc as "new" stakeholders. Empty by
    # default; tenants opt in via `[team]\nmembers = [...]` in
    # `.cp-engine.toml`. Free-form first-name strings; matching is
    # case-insensitive substring on stakeholder name.
    team: tuple[str, ...] = ()
    # Lever 2 — daily attention digest configuration. Tenants opt in via
    # `[attention_digest]` in `.cp-engine.toml`; absent block yields the
    # default-constructed dataclass (no recipients = no Slack post).
    attention_digest: AttentionDigestConfig = field(
        default_factory=AttentionDigestConfig
    )
    # Weekly Slack dates loop. Tenants opt in via `[dates_loop]` in
    # `.cp-engine.toml`; absent block yields defaults (no partners rollup).
    dates_loop: DatesLoopConfig = field(default_factory=DatesLoopConfig)


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

    kwargs: dict = {
        "name": committed["name"],
        "display": committed["display"],
        "engine_version_constraint": committed["engine_version_constraint"],
        "sync": committed["sync"],
        "projects": projects,
        "root": tenant_root,
        "local_repos": MappingProxyType(dict(local["local_repos"])),
        "local_repos_by_user": MappingProxyType(
            {
                user: MappingProxyType(dict(paths))
                for user, paths in committed["local_repos_by_user"].items()
            }
        ),
    }
    # Only override the dataclass default when the tenant explicitly declared
    # [risk_categories]; otherwise rely on the canonical six-axis default.
    if committed["risk_categories"] is not None:
        kwargs["risk_categories"] = committed["risk_categories"]
    if committed["team"]:
        kwargs["team"] = committed["team"]
    kwargs["attention_digest"] = committed["attention_digest"]
    kwargs["dates_loop"] = committed["dates_loop"]
    return TenantConfig(**kwargs)


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

        contacts_raw = p.get("contacts") or []
        if not isinstance(contacts_raw, list):
            raise CommittedConfigInvalid(
                f"{source}: [[projects]][{i}].contacts must be a list of tables"
            )
        contacts: list[dict] = []
        for j, c in enumerate(contacts_raw):
            if not isinstance(c, dict):
                raise CommittedConfigInvalid(
                    f"{source}: [[projects]][{i}].contacts[{j}] must be a table"
                )
            contacts.append(dict(c))

        projects.append({"code": code, "github": github, "contacts": tuple(contacts)})

    # [local-repos.<user>] — committed, multi-user map of repo name → local
    # clone path. Read by render_repo_md to surface "**Local clone (User):**"
    # lines on each repo's _repo.md. The runner sees these (the per-machine
    # [local-repos] in .cp-engine.local.toml does not survive the round-trip).
    # Free-form user keys; no validation against a known users list.
    local_repos_raw = data.get("local-repos") or {}
    local_repos_by_user: dict[str, dict[str, str]] = {}
    if not isinstance(local_repos_raw, dict):
        raise CommittedConfigInvalid(
            f"{source}: [local-repos] must be a table of [local-repos.<user>] sections"
        )
    for user, user_block in local_repos_raw.items():
        if not isinstance(user_block, dict):
            raise CommittedConfigInvalid(
                f"{source}: [local-repos.{user}] must be a table of repo-name → path"
            )
        user_paths: dict[str, str] = {}
        for repo_name, raw_path in user_block.items():
            if not isinstance(raw_path, str) or not raw_path:
                raise CommittedConfigInvalid(
                    f"{source}: [local-repos.{user}].{repo_name!r} must be a "
                    "non-empty string path"
                )
            user_paths[repo_name] = raw_path
        local_repos_by_user[user] = user_paths

    # Optional [risk_categories] table: a `values` array overrides the default
    # tuple on TenantConfig. Absent block → fall back to the dataclass default.
    risk_categories: tuple[str, ...] | None = None
    risk_raw = data.get("risk_categories")
    if risk_raw is not None:
        if not isinstance(risk_raw, dict):
            raise CommittedConfigInvalid(
                f"{source}: [risk_categories] must be a table with a `values` array"
            )
        values = risk_raw.get("values")
        if not isinstance(values, list) or not all(
            isinstance(v, str) and v for v in values
        ):
            raise CommittedConfigInvalid(
                f"{source}: [risk_categories].values must be a non-empty list of strings"
            )
        risk_categories = tuple(values)

    # Optional [team] table: members = ["drew", "tony", ...].
    # Used by plan_from_transcript so Claude doesn't auto-add internal
    # team members as stakeholders. Free-form first-name strings; matching
    # is case-insensitive substring at use-site.
    team: tuple[str, ...] = ()
    team_raw = data.get("team")
    if team_raw is not None:
        if not isinstance(team_raw, dict):
            raise CommittedConfigInvalid(
                f"{source}: [team] must be a table with a `members` array"
            )
        members = team_raw.get("members")
        if not isinstance(members, list) or not all(
            isinstance(v, str) and v for v in members
        ):
            raise CommittedConfigInvalid(
                f"{source}: [team].members must be a list of non-empty strings"
            )
        team = tuple(members)

    # Optional [attention_digest] table (Lever 2). Absent block → defaults.
    attention_digest = _parse_attention_digest(
        data.get("attention_digest") or {}, source
    )

    # Optional [dates_loop] table. Absent block → defaults.
    dates_loop = _parse_dates_loop(data.get("dates_loop") or {}, source)

    return {
        "name": name,
        "display": display,
        "engine_version_constraint": engine_version,
        "sync": sync,
        "projects": projects,
        "local_repos_by_user": local_repos_by_user,
        "risk_categories": risk_categories,
        "team": team,
        "attention_digest": attention_digest,
        "dates_loop": dates_loop,
    }


def _parse_dates_loop(raw: dict, source: Path) -> DatesLoopConfig:
    """Parse the optional [dates_loop] block from .cp-engine.toml."""
    if not raw:
        return DatesLoopConfig()

    partners_channel = raw.get("partners_channel")
    if partners_channel is not None and (
        not isinstance(partners_channel, str) or not partners_channel
    ):
        raise CommittedConfigInvalid(
            f"{source}: [dates_loop].partners_channel must be a non-empty "
            f"Slack channel-ID string"
        )

    window_days = raw.get("window_days", 14)
    if isinstance(window_days, bool) or not isinstance(window_days, int) or window_days < 1:
        raise CommittedConfigInvalid(
            f"{source}: [dates_loop].window_days must be a positive integer"
        )

    return DatesLoopConfig(
        partners_channel=partners_channel, window_days=window_days
    )


def _parse_attention_digest(raw: dict, source: Path) -> AttentionDigestConfig:
    """Parse the optional [attention_digest] block from .cp-engine.toml.

    Empty/absent block returns the default-constructed dataclass. Partial
    blocks fill in unset fields from the dataclass defaults.

    Validates types up front and raises `CommittedConfigInvalid` on bad
    input (mirrors how `team` and `risk_categories` are parsed). The
    previous implementation coerced via `int(...)`/`bool(...)`/`tuple(...)`,
    which silently mangled common misconfigurations — e.g. `recipients =
    "U12"` became `("U", "1", "2")`, and `post_when_clear = "false"`
    became `True`. Failing here, near the .cp-engine.toml source, is far
    easier to debug than a downstream Slack DM call exploding.
    """
    if not raw:
        return AttentionDigestConfig()

    recipients_raw = raw.get("recipients", [])
    if not isinstance(recipients_raw, list) or not all(
        isinstance(r, str) and r for r in recipients_raw
    ):
        raise CommittedConfigInvalid(
            f"{source}: [attention_digest].recipients must be a list of "
            f"non-empty Slack user-ID strings"
        )

    # Note: in Python, `isinstance(True, int)` is True (bool subclasses int).
    # Explicitly reject bools so `past_due_threshold_days = true` doesn't
    # silently parse as 1.
    for key in (
        "past_due_threshold_days",
        "escalated_window_days",
        "allocation_cap_hours",
    ):
        val = raw.get(key)
        if val is not None and (isinstance(val, bool) or not isinstance(val, int)):
            raise CommittedConfigInvalid(
                f"{source}: [attention_digest].{key} must be an integer"
            )

    if "post_when_clear" in raw and not isinstance(raw["post_when_clear"], bool):
        raise CommittedConfigInvalid(
            f"{source}: [attention_digest].post_when_clear must be a boolean"
        )

    return AttentionDigestConfig(
        recipients=tuple(recipients_raw),
        past_due_threshold_days=raw.get("past_due_threshold_days", 7),
        escalated_window_days=raw.get("escalated_window_days", 7),
        allocation_cap_hours=raw.get("allocation_cap_hours", 50),
        post_when_clear=raw.get("post_when_clear", True),
    )


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
            ProjectConfig(
                code=code,
                github=p["github"],
                local_path=local_path,
                contacts=p.get("contacts", ()),
            )
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
