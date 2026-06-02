"""Tests for cp_engine.pin_resolver.

list_remote_tags hits the network and is exercised separately via
the integration smoke in scripts/release.py rehearsals; here we test
the pure resolution logic and the tenant-config reader.
"""

from __future__ import annotations

import pytest

from unittest.mock import patch

from cp_engine.pin_resolver import (
    PinResolution,
    PinResolutionError,
    list_remote_tags,
    read_constraint,
    resolve,
)


def test_resolve_picks_highest_matching_tag() -> None:
    # `~= 0.5.0` is the PEP 440 way to mean "0.5.x only" — `~= 0.5`
    # admits 0.6, 0.7, ... up to 1.0. Tenants that want minor-bump
    # discipline need the three-part form.
    tags = ("v0.6.2", "v0.6.1", "v0.6.0", "v0.5.2", "v0.5.1", "v0.5.0")
    result = resolve("~= 0.5.0", tags)
    assert isinstance(result, PinResolution)
    assert result.tag == "v0.5.2"
    assert str(result.version) == "0.5.2"


def test_resolve_two_part_compatible_admits_higher_minors() -> None:
    # PEP 440 `~= 0.5` means `>= 0.5, < 1.0`, so 0.6.x is fair game.
    tags = ("v0.6.2", "v0.6.1", "v0.6.0", "v0.5.2")
    assert resolve("~= 0.5", tags).tag == "v0.6.2"


def test_resolve_three_part_compatible_pins_to_minor() -> None:
    tags = ("v0.6.2", "v0.6.1", "v0.6.0", "v0.5.2")
    assert resolve("~= 0.6.0", tags).tag == "v0.6.2"


def test_resolve_exact_version() -> None:
    tags = ("v0.6.2", "v0.6.1", "v0.6.0")
    assert resolve("== 0.6.1", tags).tag == "v0.6.1"


def test_resolve_no_match_raises() -> None:
    tags = ("v0.5.2", "v0.5.1")
    with pytest.raises(PinResolutionError, match="No tag matches"):
        resolve("~= 0.6.0", tags)


def test_resolve_invalid_constraint_raises() -> None:
    with pytest.raises(PinResolutionError, match="Invalid constraint"):
        resolve("not-a-spec", ("v0.5.0",))


def test_resolve_skips_non_version_tags() -> None:
    # The "v-banana" tag should be skipped without aborting the search.
    tags = ("v-banana", "v0.5.2", "v0.5.1")
    result = resolve("~= 0.5.0", tags)
    assert result.tag == "v0.5.2"


def test_resolve_skips_prereleases_for_stable_constraint() -> None:
    """`packaging.SpecifierSet.contains` admits a prerelease candidate
    when the spec lacks an explicit prerelease marker. With tags sorted
    newest-first, that would silently ship `v0.16.0a1` for `~= 0.15`.
    The resolver must drop prereleases regardless."""
    tags = ("v0.16.0a1", "v0.16.0", "v0.15.5")
    # `~= 0.15` would match v0.16.0a1 under default packaging semantics;
    # we want v0.16.0 (the highest stable that satisfies the constraint).
    assert resolve("~= 0.15", tags).tag == "v0.16.0"


def test_resolve_skips_prereleases_when_only_prerelease_matches() -> None:
    """If the only candidate satisfying the constraint is a prerelease,
    resolve must raise — never silently ship it."""
    tags = ("v0.16.0a1", "v0.15.5")
    with pytest.raises(PinResolutionError, match="No tag matches"):
        resolve("~= 0.16", tags)


def _ls_remote_stdout(tags: list[str]) -> str:
    """Render fake `git ls-remote --tags --refs` output for the given tag list."""
    return "\n".join(
        f"00000000000000000000000000000000\trefs/tags/{tag}" for tag in tags
    )


def test_list_remote_tags_filters_prereleases() -> None:
    """list_remote_tags must drop prereleases at the source so they never
    reach resolve(). Belt-and-suspenders with the resolve-level filter."""
    fake_stdout = _ls_remote_stdout(
        ["v0.16.0a1", "v0.16.0b2", "v0.16.0rc1", "v0.16.0", "v0.15.5"]
    )
    with patch("cp_engine.pin_resolver.subprocess.run") as mock_run:
        mock_run.return_value.stdout = fake_stdout
        tags = list_remote_tags("https://example.invalid/repo.git")
    assert tags == ("v0.16.0", "v0.15.5")


def test_list_remote_tags_keeps_stable_tags_in_descending_order() -> None:
    fake_stdout = _ls_remote_stdout(["v0.15.5", "v0.16.0", "v0.15.0", "v0.16.1"])
    with patch("cp_engine.pin_resolver.subprocess.run") as mock_run:
        mock_run.return_value.stdout = fake_stdout
        tags = list_remote_tags("https://example.invalid/repo.git")
    assert tags == ("v0.16.1", "v0.16.0", "v0.15.5", "v0.15.0")


def test_read_constraint_happy_path(tmp_path) -> None:
    cfg = tmp_path / ".cp-engine.toml"
    cfg.write_text(
        '[tenant]\nname = "x"\n\n[engine]\nversion = "~= 0.6"\n'
    )
    assert read_constraint(tmp_path) == "~= 0.6"


def test_read_constraint_missing_file_raises(tmp_path) -> None:
    with pytest.raises(PinResolutionError, match="No .cp-engine.toml"):
        read_constraint(tmp_path)


def test_read_constraint_missing_engine_section_raises(tmp_path) -> None:
    cfg = tmp_path / ".cp-engine.toml"
    cfg.write_text('[tenant]\nname = "x"\n')
    with pytest.raises(PinResolutionError, match=r"\[engine\]\.version is required"):
        read_constraint(tmp_path)


def test_read_constraint_empty_version_raises(tmp_path) -> None:
    cfg = tmp_path / ".cp-engine.toml"
    cfg.write_text('[engine]\nversion = ""\n')
    with pytest.raises(PinResolutionError, match=r"\[engine\]\.version is required"):
        read_constraint(tmp_path)
