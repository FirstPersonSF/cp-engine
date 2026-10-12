"""Suite-wide guard: the code under test is THIS checkout's (#357).

The shared `.venv` installs `cp_engine` editable from the main clone. A
`pytest` run from a git worktree (`../cp-engine-<n>`) then imported MAIN's
`cp_engine`: green runs and failing controls alike said nothing about the
branch. `pythonpath = ["src"]` in pyproject.toml is the fix; this is the
check that the fix is in force, so losing it fails loudly instead of
silently testing the wrong tree.

It lives at the repo root, not in `tests/`, so a run of only
`prototypes/hosted-mcp` is guarded too.
"""
from __future__ import annotations

from pathlib import Path

import pytest


def cp_engine_outside_rootdir(rootdir: Path) -> str | None:
    """Where `cp_engine` was imported from, when that is NOT under `rootdir`."""
    import cp_engine

    where = Path(cp_engine.__file__).resolve()
    return None if where.is_relative_to(Path(rootdir).resolve()) else str(where)


def pytest_configure(config):
    stray = cp_engine_outside_rootdir(config.rootpath)
    if stray:
        raise pytest.UsageError(
            f"cp_engine imported from {stray}, outside this checkout "
            f"({config.rootpath}). The suite would test another tree's code. "
            f"Check `pythonpath` under [tool.pytest.ini_options] (#357)."
        )
