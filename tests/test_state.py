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
    parent_path_for,
    path_for,
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
#  parent_path_for — the dir that CONTAINS a project's working dir
#  (the pre-#302 `account_scope_for` contract, generalised to the tree;
#  every path shape is covered in tests/test_paths_tree.py)
# ──────────────────────────────────────────────────────────────────────


def _project_with(*, company_kind: str, company_name: str | None) -> ProjectState:
    """Minimal ProjectState fixture for scope tests."""
    return ProjectState(
        code="dummy",
        name="Dummy",
        has_agreement=True,
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


def test_parent_path_for_client_includes_company_slug() -> None:
    p = _project_with(company_kind="client", company_name="Infoblox")
    assert parent_path_for(p, {}) == "1p/infoblox"
    assert path_for(p, {}) == "1p/infoblox/dummy"


def test_parent_path_for_client_with_multiword_company() -> None:
    p = _project_with(company_kind="client", company_name="Sentinel One")
    assert parent_path_for(p, {}) == "1p/sentinel-one"


def test_parent_path_for_client_missing_name_falls_back() -> None:
    # A client row with no company_name still needs a deterministic
    # parent dir — `1p/unknown/` rather than a path with `//`.
    p = _project_with(company_kind="client", company_name=None)
    assert parent_path_for(p, {}) == "1p/unknown"


def test_parent_path_for_non_client_is_the_scope() -> None:
    # FPSF / Canonic already nest by self-company at the scope level;
    # a top-level self-company workstream sits directly under it.
    fpsf = _project_with(company_kind="self-fpsf", company_name="First Person")
    canonic = _project_with(company_kind="self-canonic", company_name="Canonic")
    assert parent_path_for(fpsf, {}) == "firstpersonsf"
    assert parent_path_for(canonic, {}) == "canonic"
    assert path_for(fpsf, {}) == "firstpersonsf/dummy"


# ──────────────────────────────────────────────────────────────────────
#  dir_slug
# ──────────────────────────────────────────────────────────────────────


# New contract (Task 3): `code` is now the canonical full_job_name slug, so
# the dir IS the slugified code. `name` is ignored — it no longer contributes
# a tail (the code already carries the description). These tests assert the
# slugified-code contract.


def test_dir_slug_descriptive_code_no_doubling() -> None:
    """A descriptive code passes through unchanged; the name does NOT get
    appended (which would double the description)."""
    assert (
        dir_slug(
            "ibx-5192-platform-sales-readiness-summit",
            "Platform Sales Readiness Summit",
        )
        == "ibx-5192-platform-sales-readiness-summit"
    )


def test_dir_slug_lowercases_and_trims() -> None:
    assert dir_slug("GGL-5168-Activation", "Activation") == "ggl-5168-activation"


def test_dir_slug_collapses_nonalnum() -> None:
    assert dir_slug("tel-5113-2025-collateral", None) == "tel-5113-2025-collateral"


def test_dir_slug_name_ignored() -> None:
    # name no longer affects output
    assert dir_slug("ggl-5168-activation", "totally different name") == "ggl-5168-activation"


def test_dir_slug_punctuation_collapsed_to_hyphens() -> None:
    """Apostrophes, slashes, parens, em-dashes, etc. in the code become
    hyphens; runs collapse; leading/trailing hyphens trim."""
    assert dir_slug("GGL-5168 Playbooks (Activation) — Phase II") == "ggl-5168-playbooks-activation-phase-ii"


def test_dir_slug_repo_code() -> None:
    """A bare repo slug passes straight through."""
    assert dir_slug("mc-2", "mc-2") == "mc-2"
    assert dir_slug("storyos") == "storyos"


def test_dir_slug_name_defaults_to_none() -> None:
    """`name` is now optional (defaults to None)."""
    assert dir_slug("ggl-5177") == "ggl-5177"
    assert dir_slug("ggl-5177", None) == "ggl-5177"
    assert dir_slug("ggl-5177", "") == "ggl-5177"


def test_dir_slug_ascii_only() -> None:
    """Non-ASCII characters in the code get collapsed to hyphens (we don't
    transliterate)."""
    result = dir_slug("café-résumé")
    assert result.startswith("caf")
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


# ──────────────────────────────────────────────────────────────────────
#  display_name — label-driven reference style (#305)
# ──────────────────────────────────────────────────────────────────────


def _ws(code, name, *, label=None, parent_code=None, has_agreement=False,
        company_kind="client"):
    from datetime import datetime, timezone

    from cp_engine.state import ProjectState

    return ProjectState(
        code=code, name=name, company_kind=company_kind, company_code="GGL",
        company_name="Google", status="Open", is_internal=False, owner=None,
        last_touched=datetime(2026, 9, 24, tzinfo=timezone.utc), deadline=None,
        parent_code=parent_code, has_agreement=has_agreement, label=label,
    )


def test_display_name_job_is_short_code_plus_short_name() -> None:
    """`name` is MC-2's full_job_name; the `<CO> <number>` head is dropped
    and the SHORT code prepended — never `<code> <full_job_name>` (the
    #303 form doubled the identity)."""
    from cp_engine.state import display_name

    job = _ws("ggl-5168-activation", "GGL 5168 Activation", label="job", has_agreement=True)
    assert display_name(job) == "ggl-5168 Activation"
    glued = _ws("ibx-5167-ddi-video", "IBX5167 DDI Video", label="job", has_agreement=True)
    assert display_name(glued) == "ibx-5167 DDI Video"
    # A name without the head is kept whole.
    plain = _ws("ggl-5136-go-safety-website", "go/safety website", label="job", has_agreement=True)
    assert display_name(plain) == "ggl-5136 go/safety website"


def test_display_name_account_program_initiative_are_the_name_alone() -> None:
    from cp_engine.state import display_name

    assert display_name(_ws("ggl-5216-google", "Google", label="account")) == "Google"
    assert display_name(_ws("ggl-5300-go-safety", "Go Safety", label="program")) == "Go Safety"
    mc = _ws("1pi-9005-mission-control", "Mission Control", label="initiative",
             company_kind="self-fpsf")
    assert display_name(mc) == "Mission Control"


def test_display_name_derives_the_label_from_the_roster_when_unset() -> None:
    from cp_engine.state import display_name

    account = _ws("ggl-5216-google", "Google")
    job = _ws("ggl-5168-activation", "GGL 5168 Activation", parent_code=account.code,
              has_agreement=True)
    by_code = {account.code: account, job.code: job}
    assert display_name(account, by_code) == "Google"  # has children → program? no: label rule
    assert display_name(job, by_code) == "ggl-5168 Activation"


def test_short_name_and_short_code_helpers() -> None:
    from cp_engine.state import short_code, short_name

    assert short_code("ggl-5168-activation") == "ggl-5168"
    assert short_code("GGL 5168 Activation") == "ggl-5168"
    assert short_code("not-a-code") == "not-a-code"
    assert short_name("GGL 5168 Activation", "ggl-5168-activation") == "Activation"
    assert short_name("GGL 5168", "ggl-5168") == "GGL 5168"  # strip would leave nothing
    assert short_name("Activation") == "Activation"
