"""wrap_bundle's one-live-per-element collapse is the engine's (arch plan step 1c, H11).

Hosted carried its own `_wrap_one_live_per_element` / `_wrap_version_rank`
copy of `project_sources._one_live_per_element` / `_version_rank`, with its
own version regex. The copy's regex did not allow a space after the `v`
(`"v 2"` ranked -1 and lost to `"v1"`), which the engine's
`substance.version_number` parses as 2 — the one behavioural difference, and
the engine's reading is the right one.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cp_engine import project_sources  # noqa: E402


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "http://example.invalid")
    mp.setenv("SUPABASE_ANON_KEY", "x")
    import server as mod

    yield mod
    mp.undo()


def _row(eid, label, day="2026-09-01", scope=None, pid="p1", tag=""):
    return {"est_item_id": eid, "version_label": label, "version_date": day,
            "scope": scope, "project_id": pid, "tag": tag}


_CASES = {
    "v10_beats_v9": [_row("a", "v9", tag="old"), _row("a", "v10", tag="new")],
    "date_breaks_tie": [_row("a", "v2", "2026-09-02", tag="new"), _row("a", "v2", "2026-09-01", tag="old")],
    "unparseable_loses": [_row("a", "draft", "2026-09-30", tag="old"), _row("a", "v1", "2026-01-01", tag="new")],
    "account_rows_keyed_by_origin": [_row("_authored/x", "v1", scope="account", pid="p1"),
                                     _row("_authored/x", "v1", scope="account", pid="p2")],
    "idless_appended_last": [{"version_label": "v1"}, _row("a", "v1"), _row("b", "v1")],
    "space_after_v": [_row("a", "v1", tag="old"), _row("a", "v 2", tag="new")],
}


@pytest.mark.parametrize("name", sorted(_CASES))
def test_wrap_collapse_is_the_engine_collapse(server, name):
    rows = _CASES[name]
    assert server._wrap_one_live_per_element(list(rows)) == \
        project_sources._one_live_per_element(list(rows))


def test_a_space_after_v_now_ranks_as_its_number(server):
    """The behaviour change: `v 2` beats `v1`, as it does in every engine path."""
    kept = server._wrap_one_live_per_element(list(_CASES["space_after_v"]))
    assert [r["tag"] for r in kept] == ["new"]
