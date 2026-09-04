"""Tests for the per-channel digest outcome vocabulary — cp-engine #227.

The bug these lock down: `messages == ()` used to mean two different
things — "we read the channel and it was quiet" and "we never got to
look" — and the digest reported both as a skip. Everything here exists
to keep those two apart.
"""

from __future__ import annotations

import pytest

from cp_engine.slack import (
    OUTCOME_API_ERROR,
    OUTCOME_AUTH_FAILED,
    OUTCOME_CHANNEL_NOT_FOUND,
    OUTCOME_EMPTY,
    OUTCOME_NOT_IN_CHANNEL,
    OUTCOME_OK,
    ChannelOutcome,
    DigestRunRow,
    SlackError,
    classify_slack_error,
)


# ──────────────────────────────────────────────────────────────────────
#  Error classification
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code,expected",
    [
        ("not_in_channel", OUTCOME_NOT_IN_CHANNEL),
        ("channel_not_found", OUTCOME_CHANNEL_NOT_FOUND),
        ("is_archived", OUTCOME_CHANNEL_NOT_FOUND),
        ("invalid_auth", OUTCOME_AUTH_FAILED),
        ("token_revoked", OUTCOME_AUTH_FAILED),
        ("missing_scope", OUTCOME_AUTH_FAILED),
    ],
)
def test_known_slack_errors_map_to_outcomes(code: str, expected: str) -> None:
    assert classify_slack_error(code) == expected


def test_unknown_error_falls_back_to_api_error_not_silence() -> None:
    """An unrecognised code must surface as api_error, never as a quiet week."""
    assert classify_slack_error("ratelimited") == OUTCOME_API_ERROR
    assert classify_slack_error("some_new_slack_code") == OUTCOME_API_ERROR


def test_missing_code_is_api_error() -> None:
    assert classify_slack_error(None) == OUTCOME_API_ERROR
    assert classify_slack_error("") == OUTCOME_API_ERROR


def test_slack_error_carries_structured_code() -> None:
    exc = SlackError("conversations.history failed", code="not_in_channel")
    assert exc.code == "not_in_channel"
    assert classify_slack_error(exc.code) == OUTCOME_NOT_IN_CHANNEL


def test_slack_error_code_defaults_to_none() -> None:
    """Pre-existing raise sites that pass no code must still construct."""
    assert SlackError("boom").code is None


# ──────────────────────────────────────────────────────────────────────
#  The distinction the whole issue is about
# ──────────────────────────────────────────────────────────────────────


def test_empty_channel_is_readable() -> None:
    """Zero messages from a channel we CAN read is a true quiet week."""
    oc = ChannelOutcome(
        channel_id="C1", channel_name="#ggl_5151", messages=(), outcome=OUTCOME_EMPTY
    )
    assert oc.readable
    assert len(oc.messages) == 0


def test_unreadable_channel_is_not_readable_despite_also_having_no_messages() -> None:
    """Same empty message tuple, opposite meaning — this is the bug."""
    oc = ChannelOutcome(
        channel_id="C2",
        channel_name="",
        messages=(),
        outcome=OUTCOME_NOT_IN_CHANNEL,
        error_detail="conversations.history failed for C2: not_in_channel",
    )
    assert not oc.readable
    assert len(oc.messages) == 0


def test_readable_is_true_only_for_ok_and_empty() -> None:
    for outcome in (OUTCOME_OK, OUTCOME_EMPTY):
        assert ChannelOutcome("C", "#c", (), outcome).readable
    for outcome in (
        OUTCOME_NOT_IN_CHANNEL,
        OUTCOME_CHANNEL_NOT_FOUND,
        OUTCOME_AUTH_FAILED,
        OUTCOME_API_ERROR,
    ):
        assert not ChannelOutcome("C", "#c", (), outcome).readable


# ──────────────────────────────────────────────────────────────────────
#  Run-row payloads
# ──────────────────────────────────────────────────────────────────────


def test_run_row_payload_shape() -> None:
    row = DigestRunRow(
        week="2026-W35",
        project_code="ggl-5151-grc-narrative",
        channel_id="C09TS42SL2J",
        channel_name="#ggl_5151_grc_narrative",
        outcome=OUTCOME_EMPTY,
        message_count=0,
    )
    payload = row.to_payload()
    assert payload["week"] == "2026-W35"
    assert payload["project_code"] == "ggl-5151-grc-narrative"
    assert payload["outcome"] == OUTCOME_EMPTY
    assert payload["message_count"] == 0
    assert payload["error_detail"] is None


def test_run_row_blank_channel_name_normalises_to_null() -> None:
    """An unresolvable name is NULL in the DB, not an empty string."""
    row = DigestRunRow(
        week="2026-W35",
        project_code="x",
        channel_id="C1",
        channel_name="",
        outcome=OUTCOME_NOT_IN_CHANNEL,
    )
    assert row.to_payload()["channel_name"] is None


def test_no_channels_row_has_null_channel_id() -> None:
    """enable_slack=true with nothing bound (the 5178 case) still writes a row."""
    row = DigestRunRow(
        week="2026-W35",
        project_code="ggl-5178-ehs-pitch-deck",
        outcome="no_channels",
        error_detail="enable_slack=true but no channel bound",
    )
    payload = row.to_payload()
    # '' rather than None — a NULL cannot serve as an ON CONFLICT target,
    # which would let duplicate no_channels rows accumulate week on week.
    assert payload["channel_id"] == ""
    assert payload["outcome"] == "no_channels"


def test_outcome_vocabulary_matches_migration_check_constraint() -> None:
    """These strings are duplicated in mig 168's CHECK — drift breaks writes.

    Listed literally rather than imported so that changing a constant
    without updating the migration fails HERE, not in production.
    """
    from cp_engine import slack as _s

    assert {
        _s.OUTCOME_OK,
        _s.OUTCOME_EMPTY,
        _s.OUTCOME_NO_CHANNELS,
        _s.OUTCOME_NOT_IN_CHANNEL,
        _s.OUTCOME_CHANNEL_NOT_FOUND,
        _s.OUTCOME_AUTH_FAILED,
        _s.OUTCOME_API_ERROR,
        _s.OUTCOME_PLAN_ERROR,
        _s.OUTCOME_EXEC_ERROR,
    } == {
        "ok",
        "empty",
        "no_channels",
        "not_in_channel",
        "channel_not_found",
        "auth_failed",
        "api_error",
        "plan_error",
        "exec_error",
    }
