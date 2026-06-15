from pathlib import Path

import pytest

from cp_engine.spine import SpineDirNotFound, find_spine_dir


def test_find_spine_dir_under_1p_account(tmp_path: Path) -> None:
    target = tmp_path / "1p" / "infoblox" / "ibx-5153-ai-campaign"
    target.mkdir(parents=True)
    assert find_spine_dir(tmp_path, "ibx-5153") == target


def test_find_spine_dir_initiative_scope(tmp_path: Path) -> None:
    target = tmp_path / "firstpersonsf" / "mission-control"
    target.mkdir(parents=True)
    assert find_spine_dir(tmp_path, "mission-control") == target


def test_find_spine_dir_bare_code(tmp_path: Path) -> None:
    target = tmp_path / "canonic" / "storyos"
    target.mkdir(parents=True)
    assert find_spine_dir(tmp_path, "storyos") == target


def test_find_spine_dir_skips_inactive(tmp_path: Path) -> None:
    (tmp_path / "1p" / "infoblox" / "inactive" / "ibx-5153-old").mkdir(parents=True)
    live = tmp_path / "1p" / "infoblox" / "ibx-5153-ai-campaign"
    live.mkdir(parents=True)
    assert find_spine_dir(tmp_path, "ibx-5153") == live


def test_find_spine_dir_does_not_match_inactive_dir_itself(tmp_path: Path) -> None:
    # An `inactive` bin must never be returned as a project named "inactive".
    (tmp_path / "1p" / "infoblox" / "inactive").mkdir(parents=True)
    with pytest.raises(SpineDirNotFound):
        find_spine_dir(tmp_path, "inactive")


def test_find_spine_dir_raises_when_missing(tmp_path: Path) -> None:
    (tmp_path / "1p").mkdir()
    with pytest.raises(SpineDirNotFound):
        find_spine_dir(tmp_path, "nope-9999")


def test_find_spine_dir_no_partial_prefix_false_match(tmp_path: Path) -> None:
    # `ibx-51` must not match a dir named `ibx-5153-ai-campaign` (the prefix
    # match requires `<code>-` or exact `<code>`).
    (tmp_path / "1p" / "infoblox" / "ibx-5153-ai-campaign").mkdir(parents=True)
    with pytest.raises(SpineDirNotFound):
        find_spine_dir(tmp_path, "ibx-51")
