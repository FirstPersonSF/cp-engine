"""Tenant skills are discoverable from a hosted session (#299).

A Claude Code session finds `.claude/skills/*/SKILL.md` in its checkout. A
hosted session has the same files and, until this, no way to know they exist.
These drive the real tools against a real git repo on disk — the tree is a
clone, so a mocked filesystem would test nothing about the path that runs.

    python -m pytest prototypes/hosted-mcp/test_skills.py -v
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

SKILL_MD = """---
name: canonic-layers
description: The Canonic Canon model. Use when structuring content for Canonic.
---

# Canonic Layers

Eight layers.
"""


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def tenant(tmp_path: Path) -> Path:
    up = tmp_path / "upstream"
    (up / ".claude/skills/canonic-layers/references").mkdir(parents=True)
    (up / ".claude/skills/canonic-layers/SKILL.md").write_text(SKILL_MD)
    (up / ".claude/skills/canonic-layers/references/01-category-v03.md").write_text(
        "# Category Layer\n"
    )
    # A directory without SKILL.md is not a skill.
    (up / ".claude/skills/not-a-skill").mkdir()
    (up / "master-cp.md").write_text("# index\n")
    git("init", "-q", "-b", "main", cwd=up)
    git("config", "user.email", "t@example.com", cwd=up)
    git("config", "user.name", "t", cwd=up)
    git("add", "-A", cwd=up)
    git("commit", "-qm", "seed", cwd=up)
    return up


@pytest.fixture
def server(monkeypatch, tenant: Path):
    for k, v in {
        "MC2_API_BASE": "http://example.invalid",
        "SUPABASE_URL": "http://example.invalid",
        "SUPABASE_ANON_KEY": "x",
        "TENANT_REPO": str(tenant),
    }.items():
        monkeypatch.setenv(k, v)
    import server as mod

    monkeypatch.setattr(mod, "TENANT_REPO", str(tenant))
    monkeypatch.setattr(mod, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(mod, "caller_subject", lambda: "tester")
    monkeypatch.setattr(mod, "user_client", lambda: object())
    monkeypatch.setattr(mod, "audit", lambda *a, **k: None)
    mod._TREE_STATE.clear()
    return mod


def test_list_names_only_real_skills(server):
    out = server.list_skills()
    assert out["available"] is True
    names = [s["name"] for s in out["skills"]]
    assert names == ["canonic-layers"]
    sk = out["skills"][0]
    assert sk["description"].startswith("The Canonic Canon model")
    assert sk["path"] == ".claude/skills/canonic-layers/SKILL.md"
    assert sk["references"] == ["01-category-v03.md"]


def test_load_returns_the_instructions(server):
    out = server.load_skill("canonic-layers")
    assert out["available"] is True
    assert out["skill"] == "canonic-layers"
    assert "# Canonic Layers" in out["text"]
    assert "tree_head" in out


def test_load_reference_returns_the_source(server):
    out = server.load_skill("canonic-layers", reference="01-category-v03.md")
    assert out["available"] is True
    assert out["text"].startswith("# Category Layer")


def test_unknown_skill_points_at_list(server):
    out = server.load_skill("nope")
    assert "no such skill" in out["error"]
    assert "list_skills" in out["error"]


def test_missing_reference_is_named(server):
    out = server.load_skill("canonic-layers", reference="zz.md")
    assert "has no reference" in out["error"]


@pytest.mark.parametrize("bad", ["../master-cp", "a/b", "", "CANONIC"])
def test_traversal_in_name_is_refused_before_the_filesystem(server, bad):
    out = server.load_skill(bad)
    assert "plain directory name" in out["error"]


def test_traversal_in_reference_is_refused(server):
    out = server.load_skill("canonic-layers", reference="../SKILL.md")
    assert "plain file name" in out["error"]


def test_prompt_wraps_the_instructions(server):
    text = server.skill("canonic-layers")
    assert text.startswith("The tenant skill `canonic-layers` follows")
    assert "# Canonic Layers" in text


def test_prompt_reports_a_missing_skill(server):
    text = server.skill("nope")
    assert text.startswith("Could not load skill 'nope'")


def test_instructions_name_the_tools(server):
    instr = server.mcp_server.instructions
    assert "list_skills()" in instr and "load_skill(name)" in instr
