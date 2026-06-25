import inspect

import cp_engine.mcp_server as m


def test_add_version_sel_includes_important_and_note():
    body = inspect.getsource(m.add_spine_version)
    # the prior-row fetch (_SEL) must select the new columns as standalone
    # fields so carry-forward of important/note works across versions.
    assert "origin, important, note" in body
