"""Tests for `cp_engine.claude_settings` — the engine-managed `.claude/` hook.

Covers the settings.json merge (idempotency, tenant-hook preservation,
malformed-file safety) and the end-to-end install into a tenant tree.
"""

from __future__ import annotations

import json
from pathlib import Path

from cp_engine.claude_settings import (
    _HOOK_SENTINEL,
    _MCP_SERVER_NAME,
    install_into_tenant,
    merge_mcp_config,
    merge_permissions,
    merge_settings,
)


def _engine_entries(settings: dict) -> list:
    return [
        e
        for e in settings["hooks"]["SessionStart"]
        if any(_HOOK_SENTINEL in h.get("command", "") for h in e.get("hooks", []))
    ]


def test_merge_into_empty_adds_one_entry():
    merged, changed = merge_settings(None)
    assert changed is True
    assert len(_engine_entries(merged)) == 1


def test_merge_is_idempotent():
    once, _ = merge_settings(None)
    twice, changed = merge_settings(once)
    assert changed is False
    assert twice == once
    assert len(_engine_entries(twice)) == 1


def test_merge_preserves_tenant_hooks():
    tenant = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "echo tenant-own"}]}
            ],
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}
            ],
        },
        "someOtherKey": {"keep": "me"},
    }
    merged, changed = merge_settings(tenant)
    assert changed is True
    # tenant's own SessionStart hook survived
    cmds = [
        h["command"]
        for e in merged["hooks"]["SessionStart"]
        for h in e["hooks"]
    ]
    assert "echo tenant-own" in cmds
    # the tenant's own PreToolUse entry survives, first and unmodified; the
    # engine's region guard (#205) is appended after it, never in place of it
    pre_tool = merged["hooks"]["PreToolUse"]
    assert pre_tool[0] == tenant["hooks"]["PreToolUse"][0]
    guard_cmds = [
        h["command"] for e in pre_tool for h in e["hooks"]
    ]
    assert any("guard-engine-regions.py" in c for c in guard_cmds)
    assert merged["someOtherKey"] == {"keep": "me"}
    # exactly one engine entry
    assert len(_engine_entries(merged)) == 1


def test_merge_replaces_stale_engine_entry_no_duplicates():
    # Simulate an older engine entry with a different command shape.
    stale = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "python OLD/check-cp-engine-version.py"}]}
            ]
        }
    }
    merged, changed = merge_settings(stale)
    assert changed is True
    # still exactly one engine entry — the stale one was replaced, not appended
    assert len(_engine_entries(merged)) == 1
    # and re-merging is now a no-op
    again, changed2 = merge_settings(merged)
    assert changed2 is False


# ── .mcp.json merge ────────────────────────────────────────────────────


def test_merge_mcp_into_none_adds_cp_sources():
    merged, changed = merge_mcp_config(None)
    assert changed is True
    assert merged["mcpServers"][_MCP_SERVER_NAME] == {"command": "cp", "args": ["mcp"]}


def test_merge_mcp_is_idempotent():
    once, _ = merge_mcp_config(None)
    twice, changed = merge_mcp_config(once)
    assert changed is False
    assert twice == once


def test_merge_mcp_preserves_other_servers():
    tenant = {
        "mcpServers": {
            "tenant-own": {"command": "node", "args": ["server.js"]},
        },
        "someOtherKey": {"keep": "me"},
    }
    merged, changed = merge_mcp_config(tenant)
    assert changed is True
    # tenant's own server survived untouched
    assert merged["mcpServers"]["tenant-own"] == {"command": "node", "args": ["server.js"]}
    # cp-sources added
    assert merged["mcpServers"][_MCP_SERVER_NAME] == {"command": "cp", "args": ["mcp"]}
    # unrelated keys untouched
    assert merged["someOtherKey"] == {"keep": "me"}


def test_install_writes_script_and_settings(tmp_path: Path):
    written = install_into_tenant(tmp_path)
    script = tmp_path / ".claude" / "hooks" / "check-cp-engine-version.py"
    settings = tmp_path / ".claude" / "settings.json"
    assert script in written and settings in written
    assert script.exists() and settings.exists()
    # script is executable
    assert script.stat().st_mode & 0o111
    # settings has our entry
    data = json.loads(settings.read_text())
    assert len(_engine_entries(data)) == 1
    # .mcp.json written at tenant root with the cp-sources server
    mcp = tmp_path / ".mcp.json"
    assert mcp in written and mcp.exists()
    mcp_data = json.loads(mcp.read_text())
    assert mcp_data["mcpServers"][_MCP_SERVER_NAME] == {"command": "cp", "args": ["mcp"]}


def test_install_preserves_tenant_mcp_servers(tmp_path: Path):
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"tenant-own": {"command": "node"}}}) + "\n"
    )
    install_into_tenant(tmp_path)
    data = json.loads((tmp_path / ".mcp.json").read_text())
    assert data["mcpServers"]["tenant-own"] == {"command": "node"}
    assert data["mcpServers"][_MCP_SERVER_NAME] == {"command": "cp", "args": ["mcp"]}


def test_install_does_not_clobber_malformed_mcp(tmp_path: Path):
    (tmp_path / ".mcp.json").write_text("{ not valid json ")
    written = install_into_tenant(tmp_path)
    assert (tmp_path / ".mcp.json").read_text() == "{ not valid json "
    assert (tmp_path / ".mcp.json") not in written


def test_install_is_idempotent(tmp_path: Path):
    install_into_tenant(tmp_path)
    written_second = install_into_tenant(tmp_path)
    assert written_second == []  # nothing changed on the second run


def test_install_does_not_clobber_malformed_settings(tmp_path: Path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    bad = claude / "settings.json"
    bad.write_text("{ this is not valid json ")
    written = install_into_tenant(tmp_path)
    # settings.json was NOT touched (still the bad content)
    assert bad.read_text() == "{ this is not valid json "
    assert bad not in written
    # but the script still got installed
    assert (claude / "hooks" / "check-cp-engine-version.py") in written


def test_install_preserves_existing_tenant_settings_on_disk(tmp_path: Path):
    claude = tmp_path / ".claude"
    claude.mkdir()
    (claude / "settings.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}) + "\n"
    )
    install_into_tenant(tmp_path)
    data = json.loads((claude / "settings.json").read_text())
    # the tenant's allow list is untouched; the engine only ADDS its own
    # credential deny rules (#205), never rewrites what the tenant wrote
    assert data["permissions"]["allow"] == ["Bash(ls:*)"]
    assert "Read(**/.env)" in data["permissions"]["deny"]
    assert len(_engine_entries(data)) == 1


# ── permissions.deny (#205) ───────────────────────────────────────────
# The deny list is a flat array of strings with nowhere to hang a sentinel,
# so _ENGINE_DENY_RULES itself is the engine's claim. That makes
# additive-only behavior the property to protect: a re-sync must never
# remove or reorder a rule the tenant wrote.


def test_deny_rules_are_added_to_an_empty_file():
    merged, changed = merge_permissions(None)
    assert changed is True
    assert "Read(**/.env)" in merged["permissions"]["deny"]


def test_deny_merge_is_idempotent():
    merged, _ = merge_permissions(None)
    again, changed = merge_permissions(merged)
    assert changed is False
    assert again["permissions"]["deny"] == merged["permissions"]["deny"]


def test_deny_merge_preserves_tenant_rules_and_order():
    tenant = {
        "permissions": {
            "deny": ["Read(**/my-secret)"],
            "allow": ["Bash(uv run *)"],
        },
        "model": "keep-me",
    }
    merged, changed = merge_permissions(tenant)
    assert changed is True
    # tenant's rule stays first; engine rules append after it
    assert merged["permissions"]["deny"][0] == "Read(**/my-secret)"
    assert merged["permissions"]["allow"] == ["Bash(uv run *)"]
    assert merged["model"] == "keep-me"


def test_deny_merge_does_not_mutate_its_input():
    tenant = {"permissions": {"deny": ["Read(**/mine)"]}}
    merge_permissions(tenant)
    assert tenant["permissions"]["deny"] == ["Read(**/mine)"]
