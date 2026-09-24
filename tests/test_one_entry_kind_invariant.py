"""#301 — no code path names the retired `initiatives` table or its column.

mc-2 migration 192 merged `initiatives` into `projects` and 194 retired the
table; every owner-scoped table has exactly one owner column, `project_id`.
A read that names `initiative_id` is a 42703 PostgREST turns into an empty
result (v0.123.1: every sources manifest silently skipped), so the retired
names must not survive anywhere code runs — engine, webhook, hosted server,
vendored tree. Comments and docstrings may still tell the history.

Allow-list: nothing.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TREES = (
    _ROOT / "src" / "cp_engine",
    _ROOT / "webhook",
    _ROOT / "prototypes" / "hosted-mcp" / "server.py",
    _ROOT / "prototypes" / "hosted-mcp" / "vendor",
)
_RETIRED = re.compile(r'Tables\.INITIATIVES|["\']initiatives["\']|initiative_id')


def _python_files():
    for tree in _TREES:
        if tree.is_file():
            yield tree
        else:
            yield from sorted(p for p in tree.rglob("*.py") if "__pycache__" not in p.parts)


def _docstring_lines(source: str) -> set[int]:
    """Line numbers (start..end) of every module/class/def docstring."""
    lines: set[int] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(
                getattr(body[0], "value", None), ast.Constant
            ) and isinstance(body[0].value.value, str):
                lines.update(range(body[0].lineno, body[0].end_lineno + 1))
    return lines


def _code_only(source: str) -> list[tuple[int, str]]:
    """(line, text) for every token that is not a comment or a docstring."""
    doc_lines = _docstring_lines(source)
    out: list[tuple[int, str]] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and tok.start[0] in doc_lines:
            continue
        out.append((tok.start[0], tok.string))
    return out


@pytest.mark.parametrize("path", list(_python_files()), ids=lambda p: str(p.relative_to(_ROOT)))
def test_no_code_path_names_the_retired_initiatives_table_or_column(path: Path):
    source = path.read_text(encoding="utf-8")
    hits = [
        f"{path.relative_to(_ROOT)}:{line}: {text.strip()}"
        for line, text in _code_only(source)
        if _RETIRED.search(text)
    ]
    assert not hits, (
        "retired initiative names still reachable in code (mc-2 mig 192/194, #301):\n"
        + "\n".join(hits)
    )


def test_the_invariant_sees_through_comments_but_not_code():
    """CONTROL: a comment may say `initiative_id`; a filter string may not."""
    clean = (
        '"""initiative_id in the module docstring."""\n'
        "x = 1  # initiative_id lived here\n"
        'def f():\n    """initiative_id in a def docstring."""\n    return x\n'
    )
    assert not [t for _, t in _code_only(clean) if _RETIRED.search(t)]
    dirty = 'q = client.table("commitments").eq("initiative_id", 1)\n'
    assert [t for _, t in _code_only(dirty) if _RETIRED.search(t)]
    dirty_f = 'q = f"project_id.eq.{x},initiative_id.eq.{x}"\n'
    assert [t for _, t in _code_only(dirty_f) if _RETIRED.search(t)]
