"""The knowledge check ships in the engine (2026-11-15).

It was a tenant skill called the belief check: one tenant had it, and a
hosted session had it only where a tenant kept a copy. The owner decided on
2026-11-15 that it is the knowledge check everywhere and that the engine
ships it. These pin what the move must not lose:

  * the frontmatter that puts it in `/cp-help`, `cxp skills` and the tenant
    CLAUDE.md trigger table, with the phrases the owner named;
  * the page still says it is what Claude believes after one unattended pass
    and that the reader corrects it;
  * the template step works on both paths (a file in the plugin; a
    `load_skill` reference in a hosted session);
  * the renamed verb and key, and nothing left of the old ones;
  * nothing that only makes sense in the tenant it came from.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cp_engine import skill_guide as sg

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugin"
DIR = PLUGIN / "skills" / "knowledge-check"


@pytest.fixture(scope="module")
def skill() -> str:
    return (DIR / "SKILL.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def page() -> str:
    return (DIR / "references" / "page-template.html").read_text(encoding="utf-8")


def test_the_guide_lists_it_with_the_owners_phrases():
    s = sg.find(sg.read_plugin(PLUGIN), "knowledge-check")
    assert s is not None and s.kind == "skill" and s.name == "knowledge-check"
    assert s.label == "knowledge check"
    assert s.triggers[0] == "knowledge check <code>", "the first trigger is the CLAUDE.md row"
    assert set(s.triggers) >= {
        "check <code>", "what do you know about <code>",
        "is the spine right for <code>", "belief check <code>",
    }
    for phrase in ('"knowledge check <code>"', '"check <code>"',
                   '"what do you know about <code>"', '"is the spine right for <code>"',
                   "wrap up", '"belief check <code>"'):
        assert phrase in s.description, phrase
    assert "what do you believe about" not in s.description
    # Every way a person might ask finds it, the older phrase included.
    skills = sg.read_plugin(PLUGIN)
    for q in ("knowledge check", "knowledge-check", "belief check <code>",
              "cp-engine:knowledge-check"):
        assert sg.find(skills, q) is s or sg.find(skills, q) == s, q


def test_it_is_in_the_snapshot_an_installed_cxp_reads():
    names = [s.name for s in sg.load_snapshot()]
    assert "knowledge-check" in names
    assert names.index("cp-prep") < names.index("knowledge-check") < names.index("build-stakeholder")


@pytest.mark.parametrize("golden", ["claude-md-1p.md", "claude-md-canonic.md"])
def test_the_tenant_claude_md_has_its_row(golden: str):
    md = (REPO / "tests" / "fixtures" / "golden" / "render" / golden).read_text()
    assert "| `knowledge check <code>` | any |" in md
    assert "the `knowledge-check` skill. |" in md


def test_the_hosted_header_reader_gets_name_and_description(skill: str):
    """The hosted server reads the header line by line, not with YAML: the
    two fields must be single-line scalars."""
    head = skill[3: skill.index("\n---", 3)]
    fields = dict(
        m.groups() for m in (re.match(r"^(name|description):\s*(.*)$", ln)
                             for ln in head.splitlines()) if m
    )
    assert fields["name"] == "knowledge-check"
    assert fields["description"].startswith("Check and repair a project's memory")
    assert fields["description"].endswith("by hand.")


def test_the_page_keeps_saying_it_is_what_claude_believes(page: str):
    lede = re.search(r'<p class="lede">(.*?)</p>', page, re.S).group(1)
    assert lede.startswith("What Claude believes about {{PROJECT NAME}} after one unattended pass")
    assert "written for {{PERSON}} to correct" in lede
    assert "<title>{{CODE}} Knowledge Check</title>" in page
    assert "<h1>{{CODE}} Knowledge Check</h1>" in page
    assert "Belief Check" not in page


def test_the_page_still_saves_corrections_and_has_the_done_button(page: str):
    assert 'db.doc("corrections/"+id)' in page and 'db.doc("corrections/done")' in page
    assert 'id="done-btn"' in page
    for box in ("t-brief", "t-q1", "t-other"):
        assert f'id="{box}"' in page


def test_the_template_step_names_both_paths(skill: str):
    step = skill[skill.index("## 3. Write the page"): skill.index("**Five answers**")]
    flat = " ".join(step.split())
    assert "Copy `references/page-template.html` to the scratchpad" in flat
    assert "beside this file in the plugin" in flat
    assert '`load_skill("knowledge-check", reference="page-template.html")`' in flat
    assert "`truncated`" in flat


def test_the_verb_and_key_are_the_renamed_ones(skill: str):
    assert skill.count("set_knowledge_page(") == 2, "record the link, then mark it answered"
    assert "`knowledge_pages`" in skill
    for old in ("set_belief_page", "belief_pages", "belief-check", "Belief check", "Belief Check"):
        assert old not in skill, old
    # The one place the old name is meant to survive.
    assert skill.count("belief check") == 3  # description, trigger, the one-line history


def test_the_substance_is_kept(skill: str):
    for kept in (
        "**Claude is the operator:**", "**The person is the editor:**",
        "One project per run. Never sweep the tenant.",
        "## 1. Read the project cold", "## 2. Make the confident changes",
        "## 3. Write the page", "## 4. Apply the answers", "## What to report every time",
        "Retire or archive anything.", "Route more than a handful of items in one go.",
        "If you are not sure a change is right, it is a question, not a change.",
        "1. What is the brief?", "5. Who decides?",
        "six questions at most", "**I'm done** button",
        "`set_work_item_deliverable`", "`set_map_mark`", "`set_phase_agreement`",
        "`set_meeting_work_item`",
    ):
        assert kept in skill, kept


def test_nothing_in_it_belongs_to_one_tenant(skill: str, page: str):
    body = skill[skill.index("\n---", 3):]  # the header's example is a sample code, as elsewhere
    for text in (body, page):
        assert not re.search(r"\b(Drew|Tony|Marcello|Janet)\b", text)
        assert not re.search(r"\b[a-z0-9]{2,4}-\d{4}\b", text), "a real project code"
    flat = " ".join(body.split())
    assert "the owner, 2026-11-13" in flat and "the owner, 2026-11-14" in flat
    # Measured facts stay, phrased for any tenant.
    assert "Measured on one tenant on 2026-11-12" in " ".join(body.split())
    assert "81 proposals, 20 suggested links, 242 unassigned meetings" in " ".join(body.split())
