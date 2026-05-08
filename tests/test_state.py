"""Tests for cp_engine.state pure helpers (scope_for, dir_slug)."""

from __future__ import annotations

import pytest

from cp_engine.state import dir_slug, scope_for


# ──────────────────────────────────────────────────────────────────────
#  scope_for
# ──────────────────────────────────────────────────────────────────────


def test_scope_for_known_kinds() -> None:
    assert scope_for("client") == "1p"
    assert scope_for("self-fpsf") == "firstpersonsf"
    assert scope_for("self-canonic") == "canonic"


def test_scope_for_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown company_kind"):
        scope_for("partner")


# ──────────────────────────────────────────────────────────────────────
#  dir_slug
# ──────────────────────────────────────────────────────────────────────


def test_dir_slug_engagement_strips_code_prefix() -> None:
    """`GGL 5177 Event Safety Playbook` should produce a clean tail
    without doubling the code."""
    assert dir_slug("ggl-5177", "GGL 5177 Event Safety Playbook") == "ggl-5177-event-safety-playbook"


def test_dir_slug_lowercase_normalized() -> None:
    assert dir_slug("ggl-5177", "Event Safety Playbook") == "ggl-5177-event-safety-playbook"


def test_dir_slug_punctuation_collapsed_to_hyphens() -> None:
    """Apostrophes, slashes, parens, etc. become hyphens; runs collapse."""
    assert dir_slug("ggl-5168", "Playbooks (Activation) — Phase II") == "ggl-5168-playbooks-activation-phase-ii"


def test_dir_slug_repo_with_name_equal_to_code() -> None:
    """When name matches the code, slug is just the code (no doubling)."""
    assert dir_slug("mc-2", "mc-2") == "mc-2"
    assert dir_slug("storyos", "storyos") == "storyos"


def test_dir_slug_empty_name() -> None:
    assert dir_slug("ggl-5177", "") == "ggl-5177"
    assert dir_slug("ggl-5177", None) == "ggl-5177"


def test_dir_slug_name_starts_with_hyphenated_code() -> None:
    """`ggl-5177-something` should not double the code prefix."""
    assert dir_slug("ggl-5177", "ggl-5177-something") == "ggl-5177-something"


def test_dir_slug_strips_trailing_punctuation() -> None:
    assert dir_slug("ggl-5168", "Playbooks!!!") == "ggl-5168-playbooks"


def test_dir_slug_ascii_only() -> None:
    """Non-ASCII characters get collapsed to hyphens (we don't transliterate)."""
    # No Unicode handling — keep it simple; non-ASCII becomes hyphens.
    result = dir_slug("xyz-1", "café résumé")
    # The exact form depends on the regex; we just confirm it stays ASCII-safe
    # and starts with the code prefix.
    assert result.startswith("xyz-1-")
    assert all(c.isascii() for c in result)
