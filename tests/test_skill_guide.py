"""The skill guide (#395): one source — each skill's own frontmatter — for
`/cp-help`, the session-start hint, the shareable page and the tenant
CLAUDE.md trigger table.

What is being proven:

  * the frontmatter reader handles plugin commands, plugin skills and tenant
    skills (from a fixture dir), including a header YAML cannot parse and a
    tenant skill with no `triggers:` (fallback to the description);
  * the hint is ONE line, under the cap, generated — and degrades with
    "+N more" rather than wrapping;
  * the HTML page is self-contained, escapes its input, and carries light
    and dark tokens and a phone viewport;
  * DRIFT: the bundled snapshot equals the plugin files; every plugin skill
    with a trigger has a row in the rendered CLAUDE.md; every `/cp-*` the
    template names exists in the plugin; every label is 1-3 words;
  * the hook script is chained, parses, and stays silent outside a tenant.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from cp_engine import skill_guide as sg

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin"
TEMPLATE = REPO / "src" / "cp_engine" / "templates" / "CLAUDE.md.j2"
HOOK = PLUGIN / "hooks" / "skills-hint.sh"
CHAIN = PLUGIN / "hooks" / "session-start.sh"


def _tenant(tmp: Path) -> Path:
    t = tmp / "tenant"
    sk = t / ".claude" / "skills"
    (sk / "canonic-layers").mkdir(parents=True)
    (sk / "canonic-layers" / "SKILL.md").write_text(
        "---\nname: canonic-layers\n"
        "description: The Canonic (StoryOS) Canon model — eight Layers. Use whenever "
        'content is written FOR Canonic, or answering "where does this go in Canonic?".\n'
        "---\n\n# body\n"
    )
    # A colon-space inside an unquoted scalar: YAML refuses it.
    (sk / "bad-yaml").mkdir()
    (sk / "bad-yaml" / "SKILL.md").write_text(
        "---\nname: bad-yaml\ndescription: Does a thing: really. Use when the user says "
        '"do the thing".\n  - stray: [\n---\nbody\n'
    )
    (sk / "no-header").mkdir()
    (sk / "no-header" / "SKILL.md").write_text("# no frontmatter\n")
    (sk / "not-a-skill").mkdir()  # no SKILL.md
    (t / ".cp-engine.toml").write_text('[engine]\nversion = "~= 0.1"\n')
    return t


# ── reader ──────────────────────────────────────────────────────────────


def test_plugin_reader_sees_every_command_and_skill():
    names = {s.name for s in sg.read_plugin(PLUGIN)}
    on_disk = {p.stem for p in (PLUGIN / "commands").glob("*.md")} | {
        p.parent.name for p in (PLUGIN / "skills").glob("*/SKILL.md")
    }
    assert names == on_disk
    assert {"cp-help", "cp-prep", "cp-wrapup", "build-stakeholder"} <= names


def test_plugin_frontmatter_fields_are_read_not_derived():
    s = sg.find(sg.read_plugin(PLUGIN), "cp-wrapup")
    assert s is not None and s.kind == "skill"
    assert s.label == "wrap up"
    assert s.triggers[0] == "wrap up"
    assert s.example == "wrap up"
    cmd = sg.find(sg.read_plugin(PLUGIN), "cp-prep")
    assert cmd.kind == "command" and cmd.slash == "/cp-prep"
    assert sg.find(sg.read_plugin(PLUGIN), "cp-tools").hint is False


def test_tenant_skills_fall_back_to_the_description(tmp_path: Path):
    skills = {s.name: s for s in sg.read_tenant(_tenant(tmp_path))}
    assert set(skills) == {"canonic-layers", "bad-yaml", "no-header"}
    c = skills["canonic-layers"]
    assert c.kind == "tenant" and c.slash == "/canonic-layers"
    assert c.summary == "The Canonic Canon model", "first clause, parenthetical dropped"
    assert c.triggers == ("where does this go in Canonic?",)
    assert c.label == "canonic layers"
    assert c.example == "where does this go in Canonic?"


def test_unparseable_yaml_still_yields_name_and_description(tmp_path: Path):
    s = {s.name: s for s in sg.read_tenant(_tenant(tmp_path))}["bad-yaml"]
    assert s.description.startswith("Does a thing")
    assert s.triggers == ("do the thing",)


def test_missing_header_uses_the_directory_name(tmp_path: Path):
    s = {s.name: s for s in sg.read_tenant(_tenant(tmp_path))}["no-header"]
    assert s.summary == "" and s.example == "/no-header" and s.triggers == ()


def test_triggers_from_description_reads_quotes_after_use_when():
    d = ('Build a card. Use when the user says "create a stakeholder for X", '
         "“build a stakeholder card”, or supplies a URL.")
    assert sg.triggers_from_description(d) == (
        "create a stakeholder for X", "build a stakeholder card",
    )
    assert sg.triggers_from_description('Mentions "quoted" but no clause.') == ()


def test_summary_is_capped_at_a_word_boundary():
    s = sg.summary_from_description("word " * 60)
    assert len(s) <= sg.SUMMARY_MAX and s.endswith("…") and "  " not in s


def test_all_skills_merges_plugin_and_tenant_without_duplicates(tmp_path: Path):
    t = _tenant(tmp_path)
    (t / ".claude" / "skills" / "cp-wrapup").mkdir()
    (t / ".claude" / "skills" / "cp-wrapup" / "SKILL.md").write_text(
        "---\nname: cp-wrapup\ndescription: shadow\n---\n"
    )
    names = [s.name for s in sg.all_skills(plugin_root=PLUGIN, tenant_root=t)]
    assert names.count("cp-wrapup") == 1
    assert "canonic-layers" in names


def test_plugin_root_env_wins_over_the_snapshot(tmp_path: Path, monkeypatch):
    root = tmp_path / "plug"
    (root / "commands").mkdir(parents=True)
    (root / "commands" / "cp-only.md").write_text("---\ndescription: Only here.\n---\n")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    assert [s.name for s in sg.plugin_skills()] == ["cp-only"]
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT")
    assert "cp-wrapup" in [s.name for s in sg.plugin_skills()]


def test_find_accepts_name_label_trigger_and_slash():
    skills = sg.read_plugin(PLUGIN)
    for q in ("cp-wrapup", "wrap up", "/cp-prep", "prep sprint planning",
              "cp-engine:cp-wrapup", "WRAP THIS UP"):
        assert sg.find(skills, q) is not None, q
    assert sg.find(skills, "nope") is None


# ── hint ────────────────────────────────────────────────────────────────


def test_hint_is_one_short_line_of_labels(tmp_path: Path):
    skills = sg.all_skills(plugin_root=PLUGIN, tenant_root=_tenant(tmp_path))
    line = sg.render_hint(skills)
    assert "\n" not in line
    assert len(line) <= sg.HINT_MAX
    assert line.startswith("cp skills: wrap up · prep sprint planning")
    assert line.endswith("— /cp-help for details")
    assert "tool catalog" not in line and " help ·" not in line, "hint: false entries stay out"


def test_hint_overflow_says_how_many_more():
    many = [sg.Skill(name=f"s{i}", kind="skill", label=f"skill number {i}", summary="")
            for i in range(40)]
    line = sg.render_hint(many)
    assert len(line) <= sg.HINT_MAX and "\n" not in line
    assert re.search(r"\+\d+ more — /cp-help", line)


def test_every_plugin_label_is_one_to_three_words():
    """Drew's UI rule 1: labels are 1-3 words."""
    for s in sg.read_plugin(PLUGIN):
        assert 1 <= len(s.label.split()) <= 3, (s.name, s.label)
        assert s.summary and len(s.summary) <= sg.SUMMARY_MAX, (s.name, s.summary)
        assert s.example, s.name


# ── table, detail, HTML ─────────────────────────────────────────────────


def test_table_has_a_row_per_skill(tmp_path: Path):
    skills = sg.all_skills(plugin_root=PLUGIN, tenant_root=_tenant(tmp_path))
    out = sg.render_table(skills)
    rows = [ln for ln in out.splitlines() if ln.startswith("| **")]
    assert len(rows) == len(skills)
    assert all(ln.count(" | ") == 3 for ln in rows), "a stray pipe would break a row"


def test_detail_names_every_trigger():
    s = sg.find(sg.read_plugin(PLUGIN), "rfp-authoring")
    out = sg.render_detail(s)
    for t in s.triggers:
        assert f'"{t}"' in out


def test_html_is_self_contained_escaped_and_themed(tmp_path: Path):
    skills = sg.all_skills(plugin_root=PLUGIN, tenant_root=_tenant(tmp_path))
    skills.append(sg.Skill(name="x", kind="tenant", label="<script>", summary="a & b"))
    page = sg.render_html(skills, generated="2026-10-30")
    assert page.startswith("<!doctype html>")
    assert "<title>cp Skills</title>" in page
    assert 'name="viewport"' in page
    assert "prefers-color-scheme: dark" in page and ':root[data-theme="dark"]' in page
    assert "<script>" not in page and "&lt;script&gt;" in page and "a &amp; b" in page
    assert not re.search(r'(src|href)="https?://', page), "no external assets"
    assert "This tenant" in page and "Built in" in page
    assert page.count('class="card"') == len(skills)


def test_cli_skills_table_hint_detail_and_html(tmp_path: Path, monkeypatch):
    from cp_engine.cli import main

    t = _tenant(tmp_path)
    monkeypatch.chdir(t)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(PLUGIN))
    r = CliRunner()
    out = r.invoke(main, ["skills"])
    assert out.exit_code == 0 and "| **wrap up** |" in out.output and "canonic layers" in out.output
    out = r.invoke(main, ["skills", "--hint"])
    assert out.exit_code == 0 and out.output.count("\n") == 1
    out = r.invoke(main, ["skills", "wrap up"])
    assert out.exit_code == 0 and out.output.startswith("## wrap up")
    out = r.invoke(main, ["skills", "nope"])
    assert out.exit_code == 1
    page = tmp_path / "guide.html"
    out = r.invoke(main, ["skills", "--html", str(page)])
    assert out.exit_code == 0 and page.read_text().startswith("<!doctype html>")
    out = r.invoke(main, ["skills", "--json"])
    assert {d["name"] for d in json.loads(out.output)} >= {"cp-help", "canonic-layers"}


# ── drift ───────────────────────────────────────────────────────────────


def test_snapshot_matches_the_plugin_frontmatter():
    """An installed cxp has no plugin/ dir; it reads this snapshot. Edit a
    skill's frontmatter without regenerating and this fails."""
    want = sg.snapshot_data(sg.read_plugin(PLUGIN))
    have = json.loads(sg.SNAPSHOT.read_text("utf-8"))
    assert have == want, (
        "plugin skill frontmatter changed: run "
        "`python -m cp_engine.skill_guide --write-snapshot` and commit the JSON"
    )


def _trigger_table(md: str) -> str:
    start = md.index("## Trigger phrases")
    return md[start: md.index("\n## ", start + 5)]


@pytest.mark.parametrize("golden", ["claude-md-1p.md", "claude-md-canonic.md"])
def test_every_plugin_trigger_has_a_row_in_claude_md(golden: str):
    """The goldens are byte-checked against `render_claude_md` by
    test_golden_render, so this reads what a tenant actually receives."""
    md = (REPO / "tests" / "fixtures" / "golden" / "render" / golden).read_text()
    table = _trigger_table(md)
    for s in sg.read_plugin(PLUGIN):
        if s.triggers:
            assert f"| `{s.triggers[0]}` |" in table, (
                f"{s.name}'s first trigger is missing from the CLAUDE.md trigger table"
            )


def test_every_slash_command_claude_md_names_exists():
    names = {s.name for s in sg.read_plugin(PLUGIN)}
    for cmd in set(re.findall(r"`/(cp-[a-z-]+)`", TEMPLATE.read_text())):
        assert cmd in names, f"CLAUDE.md template names /{cmd}, which the plugin lacks"


def test_skill_rows_skip_phrases_with_a_hand_written_row():
    rows = sg.claude_md_rows(sg.read_plugin(PLUGIN), TEMPLATE.read_text())
    phrases = [r["phrase"] for r in rows]
    assert "wrap up" not in phrases, "hand-written row exists; do not duplicate"
    assert "prep sprint planning" in phrases


# ── hook ────────────────────────────────────────────────────────────────


def _fake_cxp(tmp: Path) -> Path:
    b = tmp / "bin"
    b.mkdir()
    (b / "cxp").write_text(
        '#!/usr/bin/env bash\n[ "$1 $2" = "skills --hint" ] && '
        'echo "cp skills: wrap up — /cp-help for details"\nexit 0\n'
    )
    (b / "cxp").chmod(0o755)
    return b


def _run_hook(cwd: Path, path: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(HOOK)], cwd=cwd, input="", capture_output=True, text=True,
        env={"PATH": f"{path}:/usr/bin:/bin", "HOME": str(cwd)}, timeout=30,
    )


def test_hook_prints_the_generated_line_inside_a_tenant(tmp_path: Path):
    r = _run_hook(_tenant(tmp_path), str(_fake_cxp(tmp_path)))
    assert r.returncode == 0
    assert r.stdout == "cp skills: wrap up — /cp-help for details\n"


def test_hook_is_silent_outside_a_tenant_and_without_cxp(tmp_path: Path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    r = _run_hook(outside, str(_fake_cxp(tmp_path)))
    assert r.returncode == 0 and r.stdout == ""
    empty = tmp_path / "empty"
    empty.mkdir()
    r = _run_hook(_tenant(tmp_path), str(empty))
    assert r.returncode == 0 and r.stdout == ""


def test_hook_survives_a_cxp_without_skills(tmp_path: Path):
    b = tmp_path / "old"
    b.mkdir()
    (b / "cxp").write_text("#!/usr/bin/env bash\necho 'No such command' >&2\nexit 2\n")
    (b / "cxp").chmod(0o755)
    r = _run_hook(_tenant(tmp_path), str(b))
    assert r.returncode == 0 and r.stdout == ""


def test_hook_is_chained_last_parses_and_is_executable():
    src = CHAIN.read_text()
    # #402: captured (and trimmed to one line) rather than printed directly.
    assert 'bash "$here/skills-hint.sh" </dev/null | head -n 1 || true' in src
    assert src.index("session-register.sh") < src.index("skills-hint.sh")
    r = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert os.access(HOOK, os.X_OK)
    assert "cxp skills --hint" in HOOK.read_text(), "the line is generated, not hand-written"
