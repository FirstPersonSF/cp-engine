"""The cp surface may shrink but not silently grow (architecture plan step 0).

The 2026-09-30 review found 64 hosted tools, 68 `cxp` commands and 36 MC-2
tables, with roughly a quarter to a third of each carrying 90% of real use.
This test fails when any count rises above `surface_ceiling.json`, so adding
surface becomes a visible decision in a commit, not a side effect. Lowering the
ceiling after a retirement keeps the ratchet tight.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import click

ROOT = Path(__file__).resolve().parents[1]
CEILING = json.loads((ROOT / "tests" / "surface_ceiling.json").read_text())


def _hosted_tool_count() -> int:
    tree = ast.parse((ROOT / "prototypes" / "hosted-mcp" / "server.py").read_text())
    sizes = {
        node.target.id: len(node.value.keys)
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and getattr(node.target, "id", "") in ("READ_ONLY_TOOLS", "MAIN_ONLY_TOOLS")
    }
    assert set(sizes) == {"READ_ONLY_TOOLS", "MAIN_ONLY_TOOLS"}, (
        "the hosted tool classification dicts moved — point this test at them"
    )
    return sum(sizes.values())


def _cxp_command_count() -> int:
    from cp_engine.cli import main

    def walk(group: click.Group) -> int:
        n = 0
        for cmd in group.commands.values():
            n += 1
            if isinstance(cmd, click.Group):
                n += walk(cmd)
        return n

    return walk(main)


def _table_count() -> int:
    from cp_engine import mc2_db

    return sum(
        1 for k, v in vars(mc2_db.Tables).items() if k.isupper() and isinstance(v, str)
    )


def test_hosted_tools_do_not_grow():
    n = _hosted_tool_count()
    assert n <= CEILING["hosted_tools"], (
        f"{n} hosted tools > ceiling {CEILING['hosted_tools']}: adding a tool is a "
        "decision — raise tests/surface_ceiling.json in the same commit and say why"
    )


def test_cxp_commands_do_not_grow():
    n = _cxp_command_count()
    assert n <= CEILING["cxp_commands"], (
        f"{n} cxp commands > ceiling {CEILING['cxp_commands']}: raise "
        "tests/surface_ceiling.json deliberately, or retire one"
    )


def test_mc2_tables_do_not_grow():
    n = _table_count()
    assert n <= CEILING["mc2_tables"], (
        f"{n} MC-2 tables > ceiling {CEILING['mc2_tables']}: raise "
        "tests/surface_ceiling.json deliberately"
    )
