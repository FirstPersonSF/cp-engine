"""Layer vocabulary is the shared package's (arch plan step 1c, H16 part)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "http://example.invalid")
    mp.setenv("SUPABASE_ANON_KEY", "x")
    import server as mod

    yield mod
    mp.undo()


def test_layer_aliases_and_canon_layer_are_the_engines(server):
    from cp_engine import authored_element

    assert server._LAYER_ALIASES is authored_element.LAYER_ALIASES
    assert server.canon_layer is authored_element.canon_layer


@pytest.mark.parametrize("value", ["output", "Output", "Client Feedback", "source material",
                                   "Deliverables", "custom-kind", "", None])
def test_canon_layer_values(server, value):
    from cp_engine.authored_element import canon_layer

    assert server.canon_layer(value) == canon_layer(value)
