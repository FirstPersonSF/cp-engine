"""Tests for cp_engine.close_out — the workdir/spine/commitment reads `cxp wrap` uses.

`cxp close` (the checklist verb) was retired in architecture step 5a; the
readers it shared with `cxp wrap` stay.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cp_engine.close_out import element_from_row, find_close_workdir
from cp_engine.spine import SpineDirNotFound


def test_element_from_row_maps_substance_columns() -> None:
    el = element_from_row(
        {
            "id": "tel-5144/x/v2",
            "est_item_id": "_authored/synth-1",
            "framing": "What we learned",
            "layer": "Synthesis",
            "body": "  body text  ",
            "serves": ["d1"],
            "scope": "account",
        }
    )
    assert el.key == "_authored/synth-1"
    assert el.title == "What we learned"
    assert el.body_len == len("body text")
    assert el.serves == ("d1",)
    assert el.scope == "account"


# ── workdir resolution incl. inactive bins ────────────────────────────


def _mk(root: Path, rel: str) -> Path:
    d = root / rel
    d.mkdir(parents=True)
    (d / "cp.md").write_text("# stub\n", encoding="utf-8")
    return d


def test_find_close_workdir_live_dir(tmp_path: Path) -> None:
    d = _mk(tmp_path, "1p/teleflex/tel-5144-urolift-3-naming")
    assert find_close_workdir(tmp_path, "tel-5144") == (d, False)


def test_find_close_workdir_account_inactive_bin(tmp_path: Path) -> None:
    _mk(tmp_path, "1p/teleflex/tel-5149-other")  # keeps the account dir live
    d = _mk(tmp_path, "1p/teleflex/inactive/tel-5144-urolift-3-naming")
    assert find_close_workdir(tmp_path, "tel-5144") == (d, True)


def test_find_close_workdir_scope_inactive_whole_account(tmp_path: Path) -> None:
    d = _mk(tmp_path, "1p/inactive/oldco/old-1111-legacy")
    assert find_close_workdir(tmp_path, "old-1111") == (d, True)


def test_find_close_workdir_missing_raises(tmp_path: Path) -> None:
    (tmp_path / "1p").mkdir()
    with pytest.raises(SpineDirNotFound):
        find_close_workdir(tmp_path, "nope-9999")
