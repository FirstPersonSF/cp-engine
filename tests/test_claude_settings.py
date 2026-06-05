"""Tests for `cp_engine.claude_settings` — the engine-managed `.claude/` hook.

Covers the settings.json merge (idempotency, tenant-hook preservation,
malformed-file safety) and the end-to-end install into a tenant tree.
"""

from __future__ import annotations

import json
from pathlib import Path

from cp_engine.claude_settings import (
    _HOOK_SENTINEL,
    install_into_tenant,
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
    # unrelated sections + keys untouched
    assert merged["hooks"]["PreToolUse"] == tenant["hooks"]["PreToolUse"]
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
    assert data["permissions"] == {"allow": ["Bash(ls:*)"]}
    assert len(_engine_entries(data)) == 1
