"""Shipped skills and commands must invoke the CLI as `cxp`, never `cp` (#317).

WHY. The CLI was renamed `cp` -> `cxp` in v0.102.0 because `cp` resolves to
`/bin/cp` on every macOS/Linux machine. The prose in `plugin/` is documentation
the model EXECUTES: `FETCH_OUT=$(cp fathom-fetch …)` does not raise a useful
error — it runs the system copy command, fails with a usage message, and the
model improvises. #218 fixed CLAUDE.md; the skills and commands kept the old
name for another two months.

The subcommand list is read from the live click group, not hand-written, so a
new subcommand is covered the day it ships.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cp_engine.cli import main

_PLUGIN = Path(__file__).resolve().parent.parent / "plugin"


def _bare_cp_re() -> re.Pattern[str]:
    subs = sorted(main.commands, key=len, reverse=True)
    return re.compile(r"(?<![\w/.-])cp (" + "|".join(map(re.escape, subs)) + r")\b")


def test_pattern_catches_the_defect() -> None:
    """Control: the guard must FAIL on the shape it exists to catch."""
    rx = _bare_cp_re()
    assert rx.search('FETCH_OUT=$(cp fathom-fetch "<id>")')
    assert rx.search("enforced by `cp exec-lint <code>`")
    assert not rx.search("`cxp exec-lint <code>`")
    assert not rx.search("run /cp-ingest on it")


@pytest.mark.parametrize(
    "path", sorted(_PLUGIN.rglob("*.md")), ids=lambda p: str(p.relative_to(_PLUGIN))
)
def test_no_bare_cp_invocations(path: Path) -> None:
    rx = _bare_cp_re()
    hits = [
        f"{n}: {line.strip()}"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if rx.search(line)
    ]
    assert not hits, "use `cxp`, not `cp` (/bin/cp):\n" + "\n".join(hits)
