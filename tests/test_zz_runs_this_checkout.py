"""The suite must import the cp_engine in THIS checkout (see pyproject's
`pythonpath`). A worktree run that imported the main clone's editable install
tested the wrong code without a single failure to say so."""

from pathlib import Path

import cp_engine


def test_cp_engine_is_imported_from_this_checkout():
    here = Path(__file__).resolve().parents[1]
    assert Path(cp_engine.__file__).resolve().is_relative_to(here / "src"), (
        f"imported {cp_engine.__file__}, not {here / 'src'}"
    )
