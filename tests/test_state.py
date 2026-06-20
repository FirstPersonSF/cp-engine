"""Tests for cp_engine.state pure helpers (scope_for, dir_slug)."""

from __future__ import annotations

import pytest

from cp_engine.state import (
    CarryForward,
    ClientAsk,
    Deliverable,
    HorizonItem,
    InboundUpdate,
    MeetingNotes,
    Outbound,
    PersonHours,
    ProjectState,
    Risk,
    SprintFacts,
    SprintFile,
    WhereItStands,
    account_scope_for,
    company_slug,
    dir_slug,
    scope_for,
    slug_full_job_name,
)

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
#  company_slug — kebab-case a company name for use as a dir name
# ──────────────────────────────────────────────────────────────────────


def test_company_slug_simple_name() -> None:
    assert company_slug("Infoblox") == "infoblox"


def test_company_slug_multi_word_kebab_cases() -> None:
    assert company_slug("Sentinel One") == "sentinel-one"


def test_company_slug_strips_non_alphanumeric() -> None:
    # Punctuation collapses to single hyphens, no leading/trailing.
    assert company_slug("AT&T, Inc.") == "at-t-inc"


def test_company_slug_falls_back_to_unknown_when_empty() -> None:
    # Empty / None / whitespace-only must not produce '' which would
    # leave a project at 1p//<dir_slug>/ — a broken path.
    assert company_slug(None) == "unknown"
    assert company_slug("") == "unknown"
    assert company_slug("   ") == "unknown"


# ──────────────────────────────────────────────────────────────────────
#  account_scope_for — full project working-dir parent: <scope>/<account>
# ──────────────────────────────────────────────────────────────────────


def _project_with(*, company_kind: str, company_name: str | None) -> ProjectState:
    """Minimal ProjectState fixture for scope tests."""
    return ProjectState(
        code="dummy",
        name="Dummy",
        source="engagement",
        company_kind=company_kind,
        company_code="DUM",
        company_name=company_name,
        status="Open",
        is_internal=False,
        owner=None,
        last_touched=None,
        deadline=None,
        deal_stage=None,
        budget=None,
    )


def test_account_scope_for_client_includes_company_slug() -> None:
    p = _project_with(company_kind="client", company_name="Infoblox")
    assert account_scope_for(p) == "1p/infoblox"


def test_account_scope_for_client_with_multiword_company() -> None:
    p = _project_with(company_kind="client", company_name="Sentinel One")
    assert account_scope_for(p) == "1p/sentinel-one"


def test_account_scope_for_client_missing_name_falls_back() -> None:
    # A client row with no company_name still needs a deterministic
    # parent dir — `1p/unknown/` rather than a path with `//`.
    p = _project_with(company_kind="client", company_name=None)
    assert account_scope_for(p) == "1p/unknown"


def test_account_scope_for_non_client_is_unchanged() -> None:
    # FPSF / Canonic already nest by self-company at the scope level;
    # account_scope_for is a no-op there (= scope_for).
    fpsf = _project_with(company_kind="self-fpsf", company_name="First Person")
    canonic = _project_with(company_kind="self-canonic", company_name="Canonic")
    assert account_scope_for(fpsf) == "firstpersonsf"
    assert account_scope_for(canonic) == "canonic"


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


# ──────────────────────────────────────────────────────────────────────
#  slug_full_job_name
# ──────────────────────────────────────────────────────────────────────


def test_slug_basic() -> None:
    assert (
        slug_full_job_name("IBX 5192 Platform Sales Readiness Summit")
        == "ibx-5192-platform-sales-readiness-summit"
    )


def test_slug_punctuation_and_plus() -> None:
    assert slug_full_job_name("GGL 5188 Calendar + Maintenance") == "ggl-5188-calendar-maintenance"
    assert slug_full_job_name("GGL 5136 go/safety website") == "ggl-5136-go-safety-website"


def test_slug_trims_and_collapses() -> None:
    assert slug_full_job_name("  SAP   5171   Display Ads 26 ") == "sap-5171-display-ads-26"


def test_slug_empty_returns_empty() -> None:
    assert slug_full_job_name("") == ""
    assert slug_full_job_name(None) == ""


def test_client_ask_constructs_with_defaults() -> None:
    a = ClientAsk(text="Volume forecast", asked_date="2026-05-04", status="open", who="Maria")
    assert a.status == "open"
    assert a.who == "Maria"


def test_risk_includes_category_and_severity() -> None:
    r = Risk(
        text="Legal slip",
        severity="escalated",
        category="contract",
        raised_date="2026-05-04",
        why_it_matters="Pushes contract into next sprint.",
    )
    assert r.severity == "escalated"
    assert r.category == "contract"


def test_horizon_item_bucket_required() -> None:
    h = HorizonItem(text="Open beta launch", bucket="decision", target_date="2026-W22")
    assert h.bucket == "decision"
    assert h.target_date == "2026-W22"


def test_outbound_status_field() -> None:
    o = Outbound(text="Counter-proposal", status="sent", date="2026-05-09")
    assert o.status == "sent"


def test_inbound_update_holds_who_and_quote() -> None:
    u = InboundUpdate(date="2026-05-09", who="Maria", text="Tier-2 doesn't match")
    assert u.who == "Maria"


def test_deliverable_position_drives_priority() -> None:
    d1 = Deliverable(text="Pricing finalized", position=1)
    d2 = Deliverable(text="Deck reviewed", position=2)
    assert d1.position < d2.position


def test_meeting_notes_holds_decisions_and_prose() -> None:
    m = MeetingNotes(
        source="From sprint planning · May 11",
        attendees="Drew + Tony",
        duration="22 min",
        decisions=("Hold tier-2 cap firm.",),
        discussion_prose="Spent most of the time on…",
    )
    assert len(m.decisions) == 1


def test_sprint_file_aggregates_all_sections_and_computes_total() -> None:
    sf = SprintFile(
        project_code="peb",
        week_iso="2026-W19",
        week_start="2026-05-11",
        week_end="2026-05-17",
        prior_sprint="2026-W18",
        facts=SprintFacts(
            stage="Negotiation", owner="Drew", budget_short="$45,000",
            last_touched_short="2 days ago",
            last_sprint_hours_line="Drew 6.5h · Tony 2h",
            sessions_this_week=3, open_issues=3,
        ),
        where_it_stands=WhereItStands(
            last_session_date="2026-05-09",
            last_session_who="Drew",
            last_session_summary="Reviewed pricing v3.",
            recent_commits=(),
            open_tracked_issues=(),
        ),
        carry_forward=CarryForward(asks=(), risks=(), horizon=()),
        client_outbound=(),
        client_open_asks=(),
        client_inbound=(),
        risks=(
            Risk(
                text="Legal slip", severity="escalated",
                category="contract", raised_date="2026-05-04",
            ),
        ),
        allocation=(
            PersonHours(person_name="Drew", hours=6.0),
            PersonHours(person_name="Tony", hours=2.0),
        ),
        deliverables=(),
        definition_of_done="",
        horizon=(),
        meeting_notes=None,
    )
    assert sf.total_allocation_hours == 8.0
    assert sf.escalated_risk_count == 1
