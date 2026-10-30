"""Hosted-suite guard: no test module may leave a tenant-tree clone behind.

`server.tree_root()` clones the tenant repo into
`tempfile.mkdtemp(prefix="hosted-cp-tree-")` on first use. That is right for
the long-lived server (one clone per container) and wrong for a test, where
every fixture that clears `_TREE_STATE` and calls a tree tool made a fresh
clone nobody removed — ~1,600 of them accumulated in $TMPDIR.

This fixture records every such directory THIS process creates during a test
module and fails the module's teardown if any of them still exists afterwards.

It counts constructions, not the contents of $TMPDIR (#386). The first version
diffed a glob of the shared temp dir before and after each module, so a clone
made by ANY other process in that window — a second suite run from another
worktree, a local hosted server — was charged to whichever module was open:
a teardown ERROR on a random module's last case (`[promote_uphill]`,
`test_canonical_write_code`), never when the file ran alone.
"""
from __future__ import annotations

import os
import tempfile

import pytest

TREE_PREFIX = "hosted-cp-tree-"


@pytest.fixture(autouse=True, scope="module")
def _no_leaked_tree_clones(request):
    made: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        if os.path.basename(os.fspath(path)).startswith(TREE_PREFIX):
            made.append(os.fspath(path))
        return path

    mp = pytest.MonkeyPatch()
    mp.setattr(tempfile, "mkdtemp", recording_mkdtemp)
    try:
        yield
    finally:
        mp.undo()
    leaked = sorted(os.path.basename(p) for p in made if os.path.isdir(p))
    if leaked:
        pytest.fail(
            f"{request.module.__name__} left {len(leaked)} {TREE_PREFIX}* "
            f"dir(s) in {tempfile.gettempdir()}: {leaked[:5]}",
            pytrace=False,
        )
