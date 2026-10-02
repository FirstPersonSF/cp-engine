"""Hosted-suite guard: no test module may leave a tenant-tree clone behind.

`server.tree_root()` clones the tenant repo into
`tempfile.mkdtemp(prefix="hosted-cp-tree-")` on first use. That is right for
the long-lived server (one clone per container) and wrong for a test, where
every fixture that clears `_TREE_STATE` and calls a tree tool made a fresh
clone nobody removed — ~1,600 of them accumulated in $TMPDIR.

This fixture snapshots the matching directories before each test module and
fails the module's teardown if new ones are still there afterwards.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

TREE_PREFIX = "hosted-cp-tree-"


def _tree_dirs() -> set[str]:
    root = Path(tempfile.gettempdir())
    return {p.name for p in root.glob(f"{TREE_PREFIX}*") if p.is_dir()}


@pytest.fixture(autouse=True, scope="module")
def _no_leaked_tree_clones(request):
    before = _tree_dirs()
    yield
    leaked = sorted(_tree_dirs() - before)
    if leaked:
        pytest.fail(
            f"{request.module.__name__} left {len(leaked)} {TREE_PREFIX}* "
            f"dir(s) in {tempfile.gettempdir()}: {leaked[:5]}",
            pytrace=False,
        )
