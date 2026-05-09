"""Tests for cp_engine.pin_resolver.

list_remote_tags hits the network and is exercised separately via
the integration smoke in scripts/release.py rehearsals; here we test
the pure resolution logic and the tenant-config reader.
"""

from __future__ import annotations

import pytest

from cp_engine.pin_resolver import (
    PinResolution,
    PinResolutionError,
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
