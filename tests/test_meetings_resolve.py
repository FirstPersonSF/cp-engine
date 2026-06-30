"""Tests for resolving a meeting's project_tags to a project id."""

from cp_engine.meetings import resolve_meeting_project


def test_resolves_first_matching_tag():
    mapping = {"IBX 5167 DDI Platform Video": "pid-5167"}

    def fake(client, code):
        return mapping.get(code)

    assert resolve_meeting_project(
        object(),
        ["IBX 5167 DDI Platform Video"],
        resolver=fake,
    ) == ("pid-5167", "IBX 5167 DDI Platform Video")


def test_untagged_returns_none():
    def fake(client, code):
        return None

    assert resolve_meeting_project(object(), ["untagged"], resolver=fake) == (
        None,
        None,
    )


def test_empty_tags_returns_none():
    def fake(client, code):  # pragma: no cover - should never be called
        raise AssertionError("resolver should not be called for empty tags")

    assert resolve_meeting_project(object(), [], resolver=fake) == (None, None)


def test_skips_unresolvable_tag_and_tries_next():
    mapping = {"IBX 5167 DDI Platform Video": "pid-5167"}

    def fake(client, code):
        return mapping.get(code)

    assert resolve_meeting_project(
        object(),
        ["  ", "Unknown Meeting", "IBX 5167 DDI Platform Video"],
        resolver=fake,
    ) == ("pid-5167", "IBX 5167 DDI Platform Video")


def test_untagged_marker_is_case_insensitive():
    # The `.lower()` in the skip check is load-bearing: an "UNTAGGED"/"Untagged"
    # marker must be skipped (and the resolver never consulted for it).
    def fake(client, code):  # pragma: no cover - should never be called
        raise AssertionError("resolver should not be called for an untagged marker")

    assert resolve_meeting_project(object(), ["UNTAGGED"], resolver=fake) == (None, None)
    assert resolve_meeting_project(object(), ["Untagged"], resolver=fake) == (None, None)


def test_whitespace_only_tag_returns_none():
    # A lone whitespace tag is skipped on its own (not just when masked by a
    # later resolvable tag) → (None, None), resolver never called.
    def fake(client, code):  # pragma: no cover - should never be called
        raise AssertionError("resolver should not be called for a whitespace tag")

    assert resolve_meeting_project(object(), ["   "], resolver=fake) == (None, None)
