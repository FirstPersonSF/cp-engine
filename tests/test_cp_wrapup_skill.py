"""The /cp-wrapup skill must stay true to BOTH surfaces it drives (#280).

WHY A SECOND PATH EXISTS. The skill's steps assume a checkout: they edit
`cp.md` with the Edit tool and shell out to `cxp`. A session reaching the
tenant only through the `cp-hosted` MCP server has neither, so a teammate
working that way had a ritual with no runnable steps — and the tenant's
trigger table sent them to it by name.

WHY THESE TESTS. A skill is documentation the model EXECUTES, so a verb that
was renamed or never shipped does not raise — the model improvises a
plausible, wrong wrap-up. That is worse here than in most places, because the
output of a wrong wrap-up is a `· updated` stamp: it makes a stale summary
look current, and every staleness check in the system reads the stamp.

These bind the hosted prose to the hosted server's actual tool registry.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SKILL = (
    Path(__file__).resolve().parent.parent
    / "plugin" / "skills" / "cp-wrapup" / "SKILL.md"
)
_SERVER = (
    Path(__file__).resolve().parent.parent
    / "prototypes" / "hosted-mcp" / "server.py"
)


@pytest.fixture(scope="module")
def skill() -> str:
    return _SKILL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def hosted_half(skill: str) -> str:
    """Only the hosted branch.

    Scoping matters: the CLI half legitimately names `cxp brief` and the Edit
    tool, and a whole-file search for "cxp" would let a hosted step that
    wrongly told the model to shell out slip through.
    """
    start = skill.index("## The hosted path")
    end = skill.index("\n## Notes")
    return skill[start:end]


@pytest.fixture(scope="module")
def hosted_tools() -> set[str]:
    """Every `@mcp_server.tool()` the hosted server registers.

    Parsed from the AST rather than imported: the module exits at import
    without credentials, and grep would match the string inside a comment —
    which is exactly how the tool count was miscounted as 58 on 2026-09-17.
    """
    tree = ast.parse(_SERVER.read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            func = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(func, ast.Attribute) and func.attr == "tool":
                names.add(node.name)
    return names


def test_the_skill_has_both_paths(skill: str) -> None:
    """The routing step must come BEFORE step 1.

    Discovering the wrong path at step 3 means the Exec Summary was already
    half-written by a path that cannot finish.
    """
    assert "## 0 — Which path are you on?" in skill
    assert "## The hosted path" in skill
    assert skill.index("## 0 —") < skill.index("## 1 —")


def test_every_hosted_verb_it_names_is_registered(hosted_half: str, hosted_tools: set[str]) -> None:
    """THE BINDING. Checked against the server's registry, not a list here."""
    named = {
        "get_project_state",
        "capture_project_state",
        "spine_lint",
        "seal_sweep",
        "seal_to_deliverable",
        "commitments_sweep",
        "resolve_commitment",
        "word_count_check",
        "capture_session",
        "add_element_source",
        "list_spine_elements",
        "pull_spine_element",
    }
    import re

    # Word-boundary, not substring: `"seal_sweep" in "seal_sweep_all"` is True,
    # so a plain `in` test passes against a skill naming a verb that does not
    # exist — which is the single defect this test exists to catch.
    def _named_in(text: str, verb: str) -> bool:
        return re.search(rf"(?<![\w-]){re.escape(verb)}(?![\w-])", text) is not None

    for verb in named:
        assert _named_in(hosted_half, verb), (
            f"test names {verb!r} but the skill does not"
        )
    missing = named - hosted_tools
    assert not missing, f"the hosted path names unregistered verbs: {missing}"

    # The converse, and the half that actually bites: every snake_case token
    # the hosted half presents AS a verb must be registered. Without this a
    # renamed or invented verb sails through, because the set above is a list
    # maintained here rather than a reading of the prose.
    cited = {
        m.group(1)
        for m in re.finditer(r"`([a-z][a-z0-9_]*_[a-z0-9_]+)(?:\s[^`]*)?`", hosted_half)
    }
    # Parameter names and field names are cited in backticks too; they are not
    # verbs and are asserted separately by the field test below.
    not_verbs = {
        "where_it_stands", "next_up", "project_code", "cp_engine",
        "weekly_cp", "all_deliverables", "undated_only",
    }
    unknown = {c for c in cited - not_verbs if c not in hosted_tools}
    assert not unknown, (
        f"the hosted path cites verbs the server does not register: {unknown}"
    )


def test_the_bulleted_fields_it_warns_about_are_the_real_ones(hosted_half: str) -> None:
    """`where_it_stands`/`next_up`/`blockers` REPLACE all bullets.

    The skill tells the model to read before writing because of that. If a
    field were renamed, the warning would point at nothing and the model
    would drop bullets it never resent.
    """
    tree = ast.parse(_SERVER.read_text(encoding="utf-8"))
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "capture_project_state"
    )
    params = {a.arg for a in fn.args.args}
    for field in ("status", "objective", "where_it_stands", "next_up", "blockers"):
        assert field in params, f"capture_project_state lost {field!r}"
        assert field in hosted_half, f"hosted path omits {field!r}"


def test_it_does_not_promise_an_updates_field_that_does_not_exist(hosted_half: str) -> None:
    """THE GAP, ASSERTED. The CLI path appends one dated Update per session;
    the hosted verb takes no `updates` argument, so that history does not
    advance from a hosted session.

    The skill must SAY so rather than carry a step nobody can run. If the verb
    ever grows the parameter, this fails and the prose gets rewritten — which
    is the point.
    """
    tree = ast.parse(_SERVER.read_text(encoding="utf-8"))
    fn = next(
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "capture_project_state"
    )
    params = {a.arg for a in fn.args.args}
    assert "updates" not in params, (
        "capture_project_state now takes `updates` — the hosted path's "
        "'no hosted equivalent' caveat is stale and must be rewritten"
    )
    assert "no hosted equivalent" in hosted_half


def test_the_hosted_path_never_tells_the_model_to_shell_out(hosted_half: str) -> None:
    """THE CONTROL. A hosted session has no `cxp` and no Edit tool.

    A hosted step naming either is unrunnable, and the model discovers that
    only after the Exec Summary is already written. `cxp brief` is the one
    that nearly leaked: it is the CLI step 5's verb, and the canon check has
    no hosted equivalent — so the hosted half must route it to the spine
    verbs, mentioning `cxp brief` ONLY to say it is unavailable.
    """
    for line in hosted_half.splitlines():
        if "cxp" not in line:
            continue
        assert "CLI-only" in line, (
            f"hosted path tells the model to run cxp: {line.strip()!r}"
        )


def test_it_states_what_the_hosted_path_cannot_do(hosted_half: str) -> None:
    """A step silently skipped is the same failure as a stamp silently stale.

    Rotation and the tenant-file sweeps have no verb behind them; the skill
    must hand them to the user rather than drop them.
    """
    marker = "### What the hosted path cannot do"
    assert marker in hosted_half
    # Scope to the SECTION. Every one of these words also appears in the steps
    # above (h5 explains rotation; h1 explains commits), so searching the whole
    # hosted half passes even with the section emptied out.
    section = hosted_half[hosted_half.index(marker):]
    for owed in ("rotation", "weekly-cp.md", "improvements.md", "Commit and push"):
        assert owed in section, f"cannot-do list omits {owed}"
