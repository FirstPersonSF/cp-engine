"""Tests for cp_engine.rfp_pipeline — build spec §3 (pipeline) and §5 (registry).

The spec asks for two rules to be "enforced in the tool, not left to the
model". Most of this file is those two rules, because they are the ones
with a real cost attached: a synthesised address bounces on exactly the
shop you most wanted, and a watch-out nobody surfaced changes the send
order after you have already sent.
"""

from __future__ import annotations

import pytest

from cp_engine.rfp_pipeline import (
    EMAIL_CONFIDENCE,
    STATUSES,
    TERMINAL_STATUSES,
    Pipeline,
    Respondent,
    VendorError,
    advance,
    render_pipeline,
    slugify,
    validate_email,
    validate_status,
)


# ──────────────────────────────────────────────────────────────────────
#  §5 rule 1 — never synthesise a contact email from a pattern
# ──────────────────────────────────────────────────────────────────────


def test_a_confirmed_address_is_storable() -> None:
    assert validate_email("hello@shop.com", "confirmed") == (
        "hello@shop.com", "confirmed")


def test_a_likely_address_is_storable() -> None:
    assert validate_email("hello@shop.com", "likely")[1] == "likely"


def test_an_unresearched_address_is_REFUSED_not_downgraded() -> None:
    """Storing it and hoping someone reads the flag is how a synthesised
    address reaches a real send."""
    with pytest.raises(VendorError, match="Refusing to store"):
        validate_email("jane.doe@shop.com", "unresearched")


def test_an_unpublished_confidence_cannot_carry_an_address() -> None:
    """'They publish none' and 'here is their address' contradict."""
    with pytest.raises(VendorError):
        validate_email("jane.doe@shop.com", "unpublished")


def test_the_broker_pattern_is_named_in_the_refusal() -> None:
    """first.last@ is the shape data brokers sell — say so."""
    with pytest.raises(VendorError, match="broker pattern"):
        validate_email("jane.doe@shop.com", "unresearched")


def test_a_non_pattern_address_is_still_refused_when_unvouched() -> None:
    """The rule is about VOUCHING, not about the shape of the string."""
    with pytest.raises(VendorError, match="Refusing to store"):
        validate_email("studio@shop.com", "unresearched")


def test_no_address_is_fine_at_any_confidence() -> None:
    for conf in EMAIL_CONFIDENCE:
        assert validate_email(None, conf) == (None, conf)
        assert validate_email("  ", conf)[0] is None


def test_a_malformed_address_is_rejected_before_confidence_is_considered() -> None:
    with pytest.raises(VendorError, match="well-formed"):
        validate_email("not-an-email", "confirmed")


def test_an_unknown_confidence_is_rejected() -> None:
    with pytest.raises(VendorError, match="email_confidence"):
        validate_email(None, "probably")


def test_an_unvouched_address_renders_as_absent_not_plausible() -> None:
    """Showing a plausible-looking string invites someone to paste it."""
    r = Respondent("Shop", email_confidence="unresearched")
    assert "not researched" in r.contact_display
    r2 = Respondent("Shop", email_confidence="unpublished")
    assert "publish none" in r2.contact_display


def test_a_likely_address_renders_with_a_warning_marker() -> None:
    r = Respondent("Shop", contact_email="a@b.com", email_confidence="likely")
    assert "⚠" in r.contact_display
    confirmed = Respondent("Shop", contact_email="a@b.com",
                           email_confidence="confirmed")
    assert "⚠" not in confirmed.contact_display


# ──────────────────────────────────────────────────────────────────────
#  §5 rule 2 — watch-outs are as valuable as credentials
# ──────────────────────────────────────────────────────────────────────


def test_watch_outs_get_their_own_section_in_the_render() -> None:
    pipe = Pipeline("sap-5198-2027-ad-videos", [
        Respondent("Shop A", status="sent"),
        Respondent("Shop B", status="sent",
                   watch_outs="Feature in production may constrain Q4 capacity."),
    ])
    out = render_pipeline(pipe)
    assert "Watch-outs" in out
    assert "change the send order" in out
    assert "constrain Q4 capacity" in out


def test_no_watch_outs_means_no_empty_section() -> None:
    pipe = Pipeline("x", [Respondent("Shop A", status="sent")])
    assert "Watch-outs" not in render_pipeline(pipe)


def test_unvouched_addresses_are_surfaced_before_sending() -> None:
    pipe = Pipeline("x", [
        Respondent("Shop A", status="not_sent", email_confidence="unresearched"),
        Respondent("Shop B", status="not_sent", contact_email="a@b.com",
                   email_confidence="confirmed"),
    ])
    out = render_pipeline(pipe)
    assert "No confirmed address" in out
    assert "Do not synthesise one" in out
    assert "Shop A" in out.split("No confirmed address")[1]
    assert "Shop B" not in out.split("No confirmed address")[1]


def test_already_sent_respondents_are_not_nagged_about_addresses() -> None:
    pipe = Pipeline("x", [
        Respondent("Shop A", status="sent", email_confidence="unresearched"),
    ])
    assert "No confirmed address" not in render_pipeline(pipe)


# ──────────────────────────────────────────────────────────────────────
#  §3 — the status ladder
# ──────────────────────────────────────────────────────────────────────


def test_the_ladder_matches_the_spec_exactly() -> None:
    assert STATUSES == (
        "not_sent", "sent", "acknowledged", "responded",
        "shortlisted", "selected", "declined", "passed",
    )


def test_declined_and_passed_are_different_facts() -> None:
    """They said no vs. we did not choose them — both worth keeping."""
    assert "declined" in TERMINAL_STATUSES
    assert "passed" in TERMINAL_STATUSES
    assert "declined" != "passed"


def test_a_forward_move_is_silent() -> None:
    assert advance("sent", "responded") == ("responded", None)


def test_a_backwards_move_is_allowed_but_reported() -> None:
    """A correction is legitimate; a silent one is not."""
    status, note = advance("shortlisted", "sent")
    assert status == "sent"
    assert note and "backwards" in note


def test_reopening_a_terminal_state_is_flagged() -> None:
    status, note = advance("selected", "shortlisted")
    assert status == "shortlisted"
    assert note and "terminal" in note


def test_staying_in_a_terminal_state_is_not_flagged() -> None:
    assert advance("declined", "declined") == ("declined", None)


def test_an_unknown_status_is_rejected() -> None:
    with pytest.raises(VendorError, match="status"):
        validate_status("maybe")


# ──────────────────────────────────────────────────────────────────────
#  Registry keys + rendering
# ──────────────────────────────────────────────────────────────────────


def test_slugify_is_stable_across_punctuation_and_case() -> None:
    assert slugify("SGS Trilogy") == "sgs-trilogy"
    assert slugify("  Grace Point Media  ") == "grace-point-media"
    assert slugify("A&G") == "a-g"


def test_a_name_that_slugifies_to_nothing_is_rejected() -> None:
    with pytest.raises(VendorError):
        slugify("!!!")


def test_an_empty_pipeline_says_so() -> None:
    assert "_No respondents yet._" in render_pipeline(Pipeline("x"))


def test_respondents_render_in_ladder_order() -> None:
    pipe = Pipeline("x", [
        Respondent("Late", status="selected"),
        Respondent("Early", status="not_sent"),
    ])
    out = render_pipeline(pipe)
    assert out.index("Early") < out.index("Late")


def test_the_summary_counts_by_status() -> None:
    pipe = Pipeline("x", [
        Respondent("A", status="sent"),
        Respondent("B", status="sent"),
        Respondent("C", status="responded"),
    ])
    out = render_pipeline(pipe)
    assert "3 invited" in out
    assert "sent: 2" in out
    assert "responded: 1" in out


def test_vocabulary_matches_the_migration_check_constraints() -> None:
    """Listed literally so a constant change without a migration fails HERE."""
    assert set(STATUSES) == {
        "not_sent", "sent", "acknowledged", "responded",
        "shortlisted", "selected", "declined", "passed",
    }
    assert set(EMAIL_CONFIDENCE) == {
        "confirmed", "likely", "unpublished", "unresearched",
    }
