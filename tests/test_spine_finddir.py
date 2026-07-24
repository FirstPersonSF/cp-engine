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


def _write_cp_with_mc_id(dir_path: Path, mc_id: str) -> None:
    dir_path.mkdir(parents=True)
    (dir_path / "cp.md").write_text(
        f"---\nProject: X\nMC-id: {mc_id}\n---\n\n# X\n", encoding="utf-8"
    )


def test_find_spine_dir_uuid_first_resolves_code_slug_mismatch(tmp_path: Path) -> None:
    # The real bug: MC-2 `code` (`SLT-brand-campaign-26`) differs from the dir
    # slug (`slt-5196-brand-campaign-26`, slug-of-full_job_name). The code alone
    # can't name-match the dir — but the project's MC-id can, UUID-first.
    mc_id = "ec42e640-f93c-4047-b610-98bc1dc2ebfc"
    target = tmp_path / "1p" / "salesloft" / "slt-5196-brand-campaign-26"
    _write_cp_with_mc_id(target, mc_id)

    # name match fails (different string)…
    with pytest.raises(SpineDirNotFound):
        find_spine_dir(tmp_path, "SLT-brand-campaign-26")
    # …but UUID-first resolves it.
    assert find_spine_dir(tmp_path, "SLT-brand-campaign-26", mc2_id=mc_id) == target


def test_find_spine_dir_uuid_beats_name_across_accounts(tmp_path: Path) -> None:
    # UUID-first scans every account parent, so a stamped dir resolves even when
    # a same-code-shaped dir sits under a different company.
    mc_id = "11111111-2222-3333-4444-555555555555"
    right = tmp_path / "1p" / "salesloft" / "slt-5196-brand-campaign-26"
    _write_cp_with_mc_id(right, mc_id)
    (tmp_path / "1p" / "google" / "ggl-5168-activation").mkdir(parents=True)

    assert find_spine_dir(tmp_path, "anything", mc2_id=mc_id) == right


def test_find_spine_dir_unknown_uuid_still_raises(tmp_path: Path) -> None:
    # A new Deal with no working dir yet: neither the code nor the id resolves,
    # so the caller gets SpineDirNotFound (→ the webhook's "sync it first").
    _write_cp_with_mc_id(
        tmp_path / "1p" / "salesloft" / "slt-5195-vision-video-refresh",
        "62baf2a7-71bd-472f-a114-8c6cf08e6107",
    )
    with pytest.raises(SpineDirNotFound):
        find_spine_dir(tmp_path, "SLT-brand-campaign-26", mc2_id="deadbeef-0000")
