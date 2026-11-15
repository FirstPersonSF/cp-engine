"""Skills are discoverable from a hosted session: the tenant's (#299) and
the engine's own.

A Claude Code session finds `.claude/skills/*/SKILL.md` in its checkout. A
hosted session has the same files and, until this, no way to know they exist.
These drive the real tools against a real git repo on disk — the tree is a
clone, so a mocked filesystem would test nothing about the path that runs.

2026-11-15: the engine's shipped skills (`plugin/skills/*`) are served too,
each listing saying which source it came from, a tenant skill of the same
name winning, and the engine's readable with no tenant tree at all. The
engine directory in these tests is a fixture (`ENGINE_SKILLS_DIR`), except
where a test says it reads the real one.

    python -m pytest prototypes/hosted-mcp/test_skills.py -v
"""
from __future__ import annotations

import shutil
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


ENGINE_MD = """---
name: knowledge-check
description: The engine's copy. Use when the user says "knowledge check <code>".
---

# Knowledge check (engine)
"""

TENANT_OVERRIDE_MD = """---
name: knowledge-check
description: The tenant's own copy.
---

# Knowledge check (tenant)
"""


@pytest.fixture
def engine_dir(tmp_path: Path, monkeypatch) -> Path:
    d = tmp_path / "engine-skills"
    (d / "knowledge-check/references").mkdir(parents=True)
    (d / "knowledge-check/SKILL.md").write_text(ENGINE_MD)
    (d / "knowledge-check/references/page-template.html").write_text("<title>engine page</title>\n")
    (d / "engine-only").mkdir()
    (d / "engine-only/SKILL.md").write_text("---\nname: engine-only\ndescription: Only here.\n---\nbody\n")
    (d / "not-a-skill").mkdir()
    monkeypatch.setenv("ENGINE_SKILLS_DIR", str(d))
    return d


@pytest.fixture
def server(monkeypatch, tenant: Path, engine_dir: Path):
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
    yield mod
    # tree_root() cloned into a fresh `mkdtemp(prefix="hosted-cp-tree-")`;
    # remove it, or every test leaves one behind (see conftest.py).
    root = mod._TREE_STATE.get("root")
    mod._TREE_STATE.clear()
    if root and Path(root).parent.name.startswith("hosted-cp-tree-"):
        shutil.rmtree(Path(root).parent, ignore_errors=True)


def test_list_names_only_real_skills(server):
    out = server.list_skills()
    assert out["available"] is True
    names = [s["name"] for s in out["skills"] if s["source"] == "tenant"]
    assert names == ["canonic-layers"]
    assert "not-a-skill" not in [s["name"] for s in out["skills"]]
    sk = next(s for s in out["skills"] if s["name"] == "canonic-layers")
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


# ── the engine's own skills ──────────────────────────────────────────


def _no_tree(server, monkeypatch):
    """A server with no tenant tree configured at all."""
    monkeypatch.setattr(server, "TENANT_REPO", "")
    server._TREE_STATE.clear()

    def boom():
        raise AssertionError("the tenant tree must not be touched")

    monkeypatch.setattr(server, "tree_root", boom)


def test_the_listing_shows_both_sources(server):
    out = server.list_skills()
    by_name = {s["name"]: s for s in out["skills"]}
    assert {n: s["source"] for n, s in by_name.items()} == {
        "canonic-layers": "tenant", "engine-only": "engine", "knowledge-check": "engine"}
    assert [s["name"] for s in out["skills"]] == sorted(by_name), "one list, by name"
    eng = by_name["knowledge-check"]
    assert eng["path"] == "plugin/skills/knowledge-check/SKILL.md"
    assert eng["references"] == ["page-template.html"]
    assert eng["description"].startswith("The engine's copy")
    assert "overrides_engine" not in eng and out["overridden"] == []
    assert out["tenant_available"] is True and "tree_head" in out


def test_a_tenant_skill_of_the_same_name_wins_and_the_listing_says_so(server, tenant, monkeypatch):
    d = tenant / ".claude/skills/knowledge-check"
    (d / "references").mkdir(parents=True)
    (d / "SKILL.md").write_text(TENANT_OVERRIDE_MD)
    git("add", "-A", cwd=tenant)
    git("commit", "-qm", "override", cwd=tenant)
    server._TREE_STATE.clear()

    out = server.list_skills()
    rows = [s for s in out["skills"] if s["name"] == "knowledge-check"]
    assert len(rows) == 1, "listed once, not once per source"
    assert rows[0]["source"] == "tenant" and rows[0]["overrides_engine"] is True
    assert rows[0]["path"] == ".claude/skills/knowledge-check/SKILL.md"
    assert rows[0]["description"] == "The tenant's own copy."
    assert out["overridden"] == ["knowledge-check"]

    loaded = server.load_skill("knowledge-check")
    assert loaded["source"] == "tenant" and "(tenant)" in loaded["text"]
    # The tenant's copy has no such reference; the engine's is NOT mixed in.
    ref = server.load_skill("knowledge-check", reference="page-template.html")
    assert ref["source"] == "tenant" and "has no reference" in ref["error"]
    assert "text" not in ref
    assert server.skill("knowledge-check").startswith("The tenant skill `knowledge-check` follows")


def test_engine_skills_are_listed_with_no_tenant_tree(server, monkeypatch):
    _no_tree(server, monkeypatch)
    out = server.list_skills()
    assert out["available"] is True
    assert [(s["name"], s["source"]) for s in out["skills"]] == [
        ("engine-only", "engine"), ("knowledge-check", "engine")]
    assert out["tenant_available"] is False and "TENANT_REPO" in out["tenant_error"]
    assert "tree_head" not in out


def test_an_engine_skill_and_its_reference_load_with_no_tenant_tree(server, monkeypatch):
    _no_tree(server, monkeypatch)
    out = server.load_skill("knowledge-check")
    assert out["available"] is True and out["source"] == "engine"
    assert out["skill"] == "knowledge-check"
    assert out["path"] == "plugin/skills/knowledge-check/SKILL.md"
    assert "# Knowledge check (engine)" in out["text"] and out["truncated"] is False
    ref = server.load_skill("knowledge-check", reference="page-template.html")
    assert ref["available"] is True and ref["source"] == "engine"
    assert ref["reference"] == "page-template.html"
    assert ref["text"].startswith("<title>engine page</title>")
    assert server.skill("knowledge-check").startswith("The engine skill `knowledge-check` follows")


def test_an_engine_skill_loads_beside_a_tenant_tree_that_lacks_it(server):
    out = server.load_skill("knowledge-check")
    assert out["source"] == "engine" and "(engine)" in out["text"]
    assert server.load_skill("canonic-layers")["source"] == "tenant"


def test_an_engine_skill_is_capped_and_says_truncated(server, engine_dir, monkeypatch):
    monkeypatch.setattr(server, "TREE_MAX_FILE_BYTES", 64)
    (engine_dir / "knowledge-check/references/big.md").write_text("é" * 500)
    out = server.load_skill("knowledge-check", reference="big.md")
    assert out["truncated"] is True and out["bytes"] == 1000
    assert "[truncated at 64 bytes of 1000]" in out["text"]
    assert len(out["text"].split("\n")[0]) == 32


def test_the_team_gate_covers_engine_skills(server, monkeypatch):
    _no_tree(server, monkeypatch)
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (False, "team members only"))
    listed = server.list_skills()
    assert listed["available"] is False and listed["skills"] == []
    assert listed["error"] == "team members only"
    out = server.load_skill("knowledge-check")
    assert out["available"] is False and out["error"] == "team members only"
    assert "text" not in out


@pytest.mark.parametrize("bad", ["../engine-only", "a/b", "", "KNOWLEDGE"])
def test_traversal_in_an_engine_skill_name_is_refused(server, monkeypatch, bad):
    _no_tree(server, monkeypatch)
    assert "plain directory name" in server.load_skill(bad)["error"]


@pytest.mark.parametrize("bad", ["../SKILL.md", "../../engine-only/SKILL.md", "a/b.md", ".."])
def test_traversal_in_an_engine_reference_is_refused(server, monkeypatch, bad):
    _no_tree(server, monkeypatch)
    out = server.load_skill("knowledge-check", reference=bad)
    assert "plain file name" in out["error"] and "text" not in out


def test_a_symlink_out_of_the_engine_directory_is_refused(server, engine_dir, tmp_path, monkeypatch):
    _no_tree(server, monkeypatch)
    secret = tmp_path / "outside.txt"
    secret.write_text("not a skill file\n")
    (engine_dir / "knowledge-check/references/link.md").symlink_to(secret)
    out = server.load_skill("knowledge-check", reference="link.md")
    assert "escapes" in out["error"] and "text" not in out


def test_an_unknown_skill_with_no_tree_says_both_things(server, monkeypatch):
    _no_tree(server, monkeypatch)
    out = server.load_skill("canonic-layers")
    assert out["available"] is False
    assert "no such skill" in out["error"] and "TENANT_REPO" in out["error"]
    missing = server.load_skill("knowledge-check", reference="zz.md")
    assert "has no reference" in missing["error"]


def test_a_wrong_override_is_reported_not_papered_over(server, monkeypatch, tmp_path):
    monkeypatch.setenv("ENGINE_SKILLS_DIR", str(tmp_path / "nowhere"))
    assert server.engine_skills_dir() is None
    out = server.list_skills()
    assert "ENGINE_SKILLS_DIR" in out["engine_skills_error"]
    assert [s["source"] for s in out["skills"]] == ["tenant"]


def test_the_real_engine_directory_resolves_and_ships_the_knowledge_check(server, monkeypatch):
    """No override: server.py finds `plugin/skills` from the repo layout, and
    the skill the engine ships is loadable with its page template."""
    monkeypatch.delenv("ENGINE_SKILLS_DIR")
    _no_tree(server, monkeypatch)
    here = Path(__file__).resolve().parent
    assert server.engine_skills_dir() == (here.parents[1] / "plugin" / "skills").resolve()
    names = {s["name"] for s in server.list_skills()["skills"]}
    assert {"knowledge-check", "cp-wrapup", "build-stakeholder"} <= names
    md = server.load_skill("knowledge-check")
    assert md["source"] == "engine" and md["truncated"] is False
    page = server.load_skill("knowledge-check", reference="page-template.html")
    assert page["truncated"] is False and "Knowledge Check" in page["text"]


def test_the_image_layout_is_found_beside_server_py(server, monkeypatch, tmp_path):
    """In the image server.py is `/app/server.py` and the Dockerfile COPYs the
    skills to `/app/plugin/skills`: no repo root two levels up."""
    monkeypatch.delenv("ENGINE_SKILLS_DIR")
    app = tmp_path / "app"
    (app / "plugin/skills/knowledge-check").mkdir(parents=True)
    monkeypatch.setattr(server, "__file__", str(app / "server.py"))
    assert server.engine_skills_dir() == (app / "plugin/skills").resolve()


def test_the_instructions_say_skills_come_from_two_places(server):
    instr = server.mcp_server.instructions
    assert "`engine`" in instr and "`tenant`" in instr
    assert "TENANT SKILLS LIVE IN THE TREE TOO" not in instr
