"""Engine-managed Claude Code config under a tenant's `.claude/`.

The engine ships a SessionStart hook that self-heals a stale `cp` CLI
(installed via `uv tool` from a frozen git rev, so it silently lags the
tenant's `[engine].version` pin). This module installs that hook into a
tenant on every sync:

1. Copies the packaged hook script to `.claude/hooks/check-cp-engine-version.py`.
2. Idempotently merges a single SessionStart entry into
   `.claude/settings.json`, preserving any hooks/settings the tenant added
   themselves.

`.claude/settings.json` is JSON, so it can't carry the
`cp-engine:start/end` splice markers the markdown files use. Instead we
recognize *our* hook by a sentinel substring in its command
(`_HOOK_SENTINEL`) and ensure exactly one such entry exists — adding,
updating, or leaving it as-is. Everything else in the file is untouched.

Design constraints:
- Never raise into sync over a settings-merge problem: malformed existing
  settings.json is logged and skipped, not fatal.
- The hook command invokes the *script*, which contains all the real logic
  (and is itself non-blocking). This module only wires it up.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

# Substring that identifies the engine's own SessionStart hook entry in a
# tenant's settings.json. Stable across versions so re-syncs recognize and
# update the existing entry rather than appending duplicates.
_HOOK_SENTINEL = "check-cp-engine-version.py"

_HOOK_SCRIPT_NAME = "check-cp-engine-version.py"

# PreToolUse guard for engine-managed regions (#205). Same distribution
# discipline as the version hook: packaged under src/cp_engine/hooks/,
# copied into the tenant on sync, recognized by its own sentinel.
_GUARD_SENTINEL = "guard-engine-regions.py"
_GUARD_SCRIPT_NAME = "guard-engine-regions.py"

# The command Claude Code runs for the SessionStart hook. $CLAUDE_PROJECT_DIR
# is expanded by Claude Code to the tenant root, so the path is portable
# across machines and users.
_HOOK_COMMAND = (
    'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/' + _HOOK_SCRIPT_NAME + '"'
)


def _packaged_hook_source(name: str = _HOOK_SCRIPT_NAME) -> str:
    """Return the text of a packaged hook script."""
    return (resources.files("cp_engine") / "hooks" / name).read_text()


_GUARD_COMMAND = (
    'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/' + _GUARD_SCRIPT_NAME + '"'
)


def _guard_entry() -> dict:
    """The engine's PreToolUse matcher entry, in Claude Code's schema."""
    return {
        "matcher": "Edit|MultiEdit|Write|NotebookEdit",
        "hooks": [{"type": "command", "command": _GUARD_COMMAND}],
    }


def _is_guard_entry(entry: object) -> bool:
    """True if `entry` is (a prior version of) the engine's guard entry."""
    if not isinstance(entry, dict):
        return False
    for h in entry.get("hooks", []):
        if isinstance(h, dict) and _GUARD_SENTINEL in str(h.get("command", "")):
            return True
    return False


def _hook_entry() -> dict:
    """The engine's SessionStart matcher entry, in Claude Code's schema."""
    return {
        "hooks": [
            {"type": "command", "command": _HOOK_COMMAND},
        ]
    }


def _is_engine_entry(entry: object) -> bool:
    """True if `entry` is (a prior version of) the engine's hook entry."""
    if not isinstance(entry, dict):
        return False
    for h in entry.get("hooks", []):
        if isinstance(h, dict) and _HOOK_SENTINEL in str(h.get("command", "")):
            return True
    return False


def merge_settings(existing: dict | None) -> tuple[dict, bool]:
    """Return (merged settings, changed) ensuring exactly one engine hook.

    Preserves every other key and every non-engine SessionStart entry.
    `changed` is False iff the engine entry was already present and current.
    """
    settings: dict = dict(existing) if isinstance(existing, dict) else {}

    hooks = dict(settings.get("hooks") or {})
    session_start = list(hooks.get("SessionStart") or [])

    desired = _hook_entry()
    # Drop any existing engine entries; keep the tenant's own untouched and
    # in order, then append exactly one current engine entry at the end.
    others = [e for e in session_start if not _is_engine_entry(e)]
    new_session_start = [*others, desired]

    pre_tool = list(hooks.get("PreToolUse") or [])
    guard_others = [e for e in pre_tool if not _is_guard_entry(e)]
    new_pre_tool = [*guard_others, _guard_entry()]

    changed = session_start != new_session_start or pre_tool != new_pre_tool
    hooks["SessionStart"] = new_session_start
    hooks["PreToolUse"] = new_pre_tool
    settings["hooks"] = hooks
    return settings, changed


# ──────────────────────────────────────────────────────────────────────
#  permissions.deny — credential paths the model must never read
# ──────────────────────────────────────────────────────────────────────

# Rules the engine owns in `permissions.deny`. Unlike the hook entry — which
# carries `_HOOK_SENTINEL` inside its command string — a deny rule is a bare
# string with nowhere to hang a marker. The rules themselves ARE the
# sentinel: this frozen set is the engine's claim, so a re-sync can add
# what's missing without touching a single rule the tenant wrote. Removing a
# pattern from this set intentionally orphans it (it stays in tenant files
# until hand-removed) — that is safer than the engine deleting deny rules.
#
# Scope is credential safety, NOT locking down ordinary work: nothing here
# touches the tenant's own `.md` files, `git`, or the `cp` CLI. `cp sync`
# auto-loads `SUPABASE_*` from an `.env` outside the tenant, and this tenant
# has committed-credential history (2026-07-31), so a read-deny on secrets
# is the one rule that must not be merely advisory. See #205.
_ENGINE_DENY_RULES = (
    "Read(**/.env)",
    "Read(**/.env.*)",
    "Read(**/*.pem)",
    "Read(**/*.key)",
    "Read(**/id_rsa)",
    "Read(**/credentials.json)",
)


def merge_permissions(existing: dict | None) -> tuple[dict, bool]:
    """Return (merged settings, changed) ensuring the engine's deny rules.

    Additive only. Every rule the tenant added — in `deny`, `allow`, or
    `ask` — is preserved in order; the engine appends only its own missing
    rules. `changed` is False iff every engine rule was already present.
    """
    settings: dict = dict(existing) if isinstance(existing, dict) else {}

    permissions = dict(settings.get("permissions") or {})
    deny = list(permissions.get("deny") or [])

    missing = [r for r in _ENGINE_DENY_RULES if r not in deny]
    if not missing:
        return settings, False

    permissions["deny"] = [*deny, *missing]
    settings["permissions"] = permissions
    return settings, True


# ──────────────────────────────────────────────────────────────────────
#  .mcp.json — registers the `cp-sources` stdio MCP server (`cxp mcp`)
# ──────────────────────────────────────────────────────────────────────

# Name of the engine's MCP server entry in a tenant's `.mcp.json`. Stable so
# re-syncs recognize + update our entry rather than appending duplicates.
_MCP_SERVER_NAME = "cp-sources"


def _mcp_server_entry() -> dict:
    """The engine's `cp-sources` server entry, in Claude Code's `.mcp.json` schema.

    The command was `cp` before 2026-08-22. Tenants synced under the old name
    carry a stale entry pointing at a binary that no longer exists, which takes
    the whole `cp-sources` server (and every tool on it) down. `merge_mcp_config`
    rewrites on difference, so any sync repairs it.
    """
    return {"command": "cxp", "args": ["mcp"]}


def merge_mcp_config(existing: dict | None) -> tuple[dict, bool]:
    """Return (merged `.mcp.json`, changed) ensuring the `cp-sources` server.

    Preserves every other server the tenant registered and any other keys.
    `changed` is False iff the `cp-sources` entry was already present and current.
    """
    config: dict = dict(existing) if isinstance(existing, dict) else {}

    servers = dict(config.get("mcpServers") or {})
    desired = _mcp_server_entry()
    changed = servers.get(_MCP_SERVER_NAME) != desired
    servers[_MCP_SERVER_NAME] = desired
    config["mcpServers"] = servers
    return config, changed


def install_into_tenant(tenant_root: Path) -> list[Path]:
    """Install the hook script + settings entry. Returns files written.

    Idempotent and non-fatal: a malformed existing settings.json is left in
    place (not overwritten) and reported via the returned list staying empty
    for that file. Caller logs written paths; nothing here raises.
    """
    written: list[Path] = []
    claude_dir = tenant_root / ".claude"
    hooks_dir = claude_dir / "hooks"

    # 1. Hook script — overwrite if content differs (engine-owned file).
    for script_name in (_HOOK_SCRIPT_NAME, _GUARD_SCRIPT_NAME):
        script_dest = hooks_dir / script_name
        source = _packaged_hook_source(script_name)
        if not script_dest.exists() or script_dest.read_text() != source:
            hooks_dir.mkdir(parents=True, exist_ok=True)
            script_dest.write_text(source)
            script_dest.chmod(0o755)
            written.append(script_dest)

    # 2. settings.json — merge, preserving tenant content.
    settings_path = claude_dir / "settings.json"
    existing: dict | None = None
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError):
            # Don't clobber a file we can't parse — a tenant may have
            # hand-written hooks we'd destroy. Skip the merge this sync.
            return written
        if not isinstance(existing, dict):
            return written

    merged, hook_changed = merge_settings(existing)
    merged, perms_changed = merge_permissions(merged)
    changed = hook_changed or perms_changed
    if changed:
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(merged, indent=2) + "\n")
        written.append(settings_path)

    # 3. .mcp.json — at the TENANT ROOT (not under .claude/), registering the
    #    `cp-sources` stdio MCP server. Same best-effort discipline as
    #    settings.json: a malformed existing file is left in place, not
    #    clobbered (a tenant may have hand-registered other servers).
    mcp_path = tenant_root / ".mcp.json"
    existing_mcp: dict | None = None
    if mcp_path.exists():
        try:
            existing_mcp = json.loads(mcp_path.read_text())
        except (json.JSONDecodeError, OSError):
            return written
        if not isinstance(existing_mcp, dict):
            return written

    merged_mcp, mcp_changed = merge_mcp_config(existing_mcp)
    if mcp_changed:
        mcp_path.write_text(json.dumps(merged_mcp, indent=2) + "\n")
        written.append(mcp_path)

    return written
