"""Tests for cp_engine.ingest — v0.8.6 transcript ingest engine.

Three parts:
- parse_transcript: speakers, gaps, duration, action items, mentioned codes.
- Plan validation: schema enforcement, helpful errors on malformed plans.
- execute_plan: write-verb behavior, idempotency, atomic execution.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from cp_engine.ingest import (
    IngestPlanError,
    execute_plan,
    parse_transcript,
)


# ──────────────────────────────────────────────────────────────────────
#  parse_transcript
# ──────────────────────────────────────────────────────────────────────


def test_parse_transcript_extracts_speakers_and_duration(tmp_path: Path) -> None:
    p = tmp_path / "t.txt"
    p.write_text("""
0:04 - Drew Fiero (FirstPerson)
  Hi all.

1:12 - Brandon Grande
  Hey.

2:30 - Marcello Grande (He/Him/His)
  Hello.
""")
    audit = parse_transcript(p)
    assert audit.speakers == ["Drew Fiero", "Brandon Grande", "Marcello Grande"]
    assert audit.duration_minutes == 2  # 2:30 → 150s → 2 min


def test_parse_transcript_detects_audio_gaps_above_threshold(tmp_path: Path) -> None:
    p = tmp_path / "t.txt"
    p.write_text("""
0:00 - A
  start
3:00 - A
  three minutes later
17:00 - A
  fourteen minutes later
""")
    audit = parse_transcript(p, gap_threshold_minutes=2)
    assert len(audit.gaps) == 2
    assert audit.gaps[0].start == "0:00"
    assert audit.gaps[0].end == "3:00"
    assert audit.gaps[0].duration_minutes == 3
    assert audit.gaps[1].duration_minutes == 14


def test_parse_transcript_ignores_small_gaps(tmp_path: Path) -> None:
    p = tmp_path / "t.txt"
    p.write_text("""
0:00 - A
0:30 - B
1:00 - A
""")
    audit = parse_transcript(p, gap_threshold_minutes=2)
    assert audit.gaps == []


def test_parse_transcript_extracts_action_items(tmp_path: Path) -> None:
    p = tmp_path / "t.txt"
    p.write_text("""
0:04 - Drew
  Let's discuss.
  ACTION ITEM: Email Art re: SLT 5175 - WATCH: https://example.com/clip?t=5
1:00 - Brandon
  ACTION ITEM: Draft 5 playbooks for Google 5168 activation
""")
    audit = parse_transcript(p)
    assert len(audit.action_items) == 2
    assert audit.action_items[0].text == "Email Art re: SLT 5175"
    # The action item appears under the 0:04 speaker turn — that's the nearest
    # preceding timestamp.
    assert audit.action_items[0].timestamp == "0:04"
    assert audit.action_items[1].text == "Draft 5 playbooks for Google 5168 activation"


def test_parse_transcript_matches_mentioned_codes_case_insensitively(tmp_path: Path) -> None:
    p = tmp_path / "t.txt"
    p.write_text("Some content about GGL-5168 and ibx-5167 and also Ggl-5151.")
    audit = parse_transcript(
        p, project_codes=("ggl-5168", "ibx-5167", "ggl-5151", "ggl-5188")
    )
    assert audit.mentioned_codes == ["ggl-5151", "ggl-5168", "ibx-5167"]


def test_parse_transcript_dedupes_speakers_keeping_first_seen_order(tmp_path: Path) -> None:
    p = tmp_path / "t.txt"
    p.write_text("""
0:00 - Drew
0:30 - Brandon
1:00 - Drew
1:30 - Marcello
2:00 - Brandon
""")
    audit = parse_transcript(p)
    assert audit.speakers == ["Drew", "Brandon", "Marcello"]


# ──────────────────────────────────────────────────────────────────────
#  Plan validation
# ──────────────────────────────────────────────────────────────────────


def test_validate_plan_accepts_minimal_valid_plan(tmp_path: Path) -> None:
    plan = {
        "transcript": {"source": "file", "path": "x.txt"},
        "projects": {"ggl-5168": {"asks": [{"text": "ask 1"}]}},
    }
    # No exception → valid. (We can't easily call execute_plan without
    # sprint files; just check validation doesn't raise via dry-run path.)
    from cp_engine.ingest import _validate_plan
    _validate_plan(plan)


def test_validate_plan_rejects_unknown_verbs(tmp_path: Path) -> None:
    from cp_engine.ingest import _validate_plan
    plan = {"projects": {"ggl-5168": {"random-verb": [{"text": "x"}]}}}
    with pytest.raises(IngestPlanError) as exc:
        _validate_plan(plan)
    assert "unknown verb" in str(exc.value)


def test_validate_plan_rejects_non_mapping_top_level() -> None:
    from cp_engine.ingest import _validate_plan
    with pytest.raises(IngestPlanError):
        _validate_plan("not a dict")  # type: ignore[arg-type]
    with pytest.raises(IngestPlanError):
        _validate_plan(["a", "b"])  # type: ignore[arg-type]


def test_validate_plan_accepts_shorthand_verb_names() -> None:
    from cp_engine.ingest import _validate_plan
    # "asks" should be accepted as shorthand for "record-ask"
    plan = {"projects": {"p1": {"asks": [{"text": "x"}], "decisions": [{"text": "y"}]}}}
    _validate_plan(plan)  # no raise


# ──────────────────────────────────────────────────────────────────────
#  execute_plan end-to-end
# ──────────────────────────────────────────────────────────────────────


def _scaffold_minimal_sprint_file(path: Path, code: str = "ggl-5168") -> None:
    """Write a minimal sprint-file body that matches the v0.8.5 template."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"""---
Project: {code} — Test Project
Sprint: 2026-W20
---

# {code} — Test Project · Sprint W19 (May 11 – May 17, 2026)

## Client communication

### Outbound
- _<message — `[status · date]` prefix>_

### Open asks
- _<what we need from them — `[open · date · who]` prefix>_

### Inbound
- _<what they told us — `[date · who]` prefix>_

### Stakeholders
- _<person and role — `[name · role · context]` prefix>_

## Dependencies & risks

- _<risk — `[severity · category · date]` prefix>_

## Meeting notes & decisions

### Decisions
""")


def _make_tenant(tmp_path: Path, *, with_weekly_cp: bool = False) -> Path:
    """Build a minimal tenant scaffold with a W19 sprint file for ggl-5168.

    Pass ``with_weekly_cp=True`` to also scaffold a weekly-cp.md
    (Phase B's account_decisions block needs one to write to).
    """
    week_dir = tmp_path / "sprints" / "2026-W20"
    week_dir.mkdir(parents=True)
    _scaffold_minimal_sprint_file(week_dir / "ggl-5168.md", "ggl-5168")
    (week_dir / "_week.md").write_text("## Themes\n\n- _<theme>_\n")
    if with_weekly_cp:
        # Minimal weekly-cp shape — handwritten Decisions list + a marker
        # so account-decision insert has somewhere to anchor.
        (tmp_path / "weekly-cp.md").write_text("""# Weekly CP

## Quick Resume

placeholder

## Decisions (cross-cutting, last 4 weeks)

3. **An older decision.** (2026-05-08, source: weekly account meeting)

2. **An even older one.** (2026-05-08, source: ggl-5136)

1. **The oldest.** (2026-05-08, source: weekly account meeting)

<!-- cp-engine:start themes-strip -->
<!-- cp-engine:end themes-strip -->

## Active research

placeholder
""")
    return tmp_path


def test_execute_plan_writes_inbound_and_replaces_placeholder(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "ggl-5168": {
                "inbound": [{"text": "Rena approved Round 3", "date": "2026-05-12", "who": "Rena"}],
            }
        }
    }
    result = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    assert result.errors == []
    assert len(result.files_written) == 1
    sprint_body = (tenant / "sprints" / "2026-W20" / "ggl-5168.md").read_text()
    # Placeholder is gone, real bullet is there, hash marker present.
    assert "_<what they told us" not in sprint_body
    assert "[2026-05-12 · Rena] Rena approved Round 3" in sprint_body
    assert "cp:hash=" in sprint_body


def test_execute_plan_is_idempotent_via_content_hash(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "ggl-5168": {
                "asks": [{"text": "Approve Round 3", "who": "Rena", "date": "2026-05-12"}],
            }
        }
    }
    # First run: writes the bullet.
    r1 = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    assert r1.skipped_duplicate == 0
    assert len(r1.files_written) == 1
    body_after_first = (tenant / "sprints" / "2026-W20" / "ggl-5168.md").read_text()
    # Second run: dedupes.
    r2 = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    assert r2.skipped_duplicate == 1
    assert r2.files_written == []
    body_after_second = (tenant / "sprints" / "2026-W20" / "ggl-5168.md").read_text()
    assert body_after_first == body_after_second


def test_execute_plan_writes_decision_with_cross_cutting_flag(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "ggl-5168": {
                "decisions": [
                    {"text": "Drop Claude team plan", "date": "2026-05-12", "cross_cutting": True},
                    {"text": "Marcello drafts 5 decks", "date": "2026-05-12"},  # default False
                ]
            }
        }
    }
    execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    body = (tenant / "sprints" / "2026-W20" / "ggl-5168.md").read_text()
    assert "[decision · 2026-05-12][cross-cutting] Drop Claude team plan" in body
    # Non-cross-cutting decision: no [cross-cutting] marker.
    assert "[decision · 2026-05-12] Marcello drafts 5 decks" in body
    assert "[cross-cutting] Marcello" not in body


def test_execute_plan_collects_errors_for_missing_sprint_files(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "missing-code": {"asks": [{"text": "x"}]},
            "ggl-5168": {"asks": [{"text": "valid"}]},
        }
    }
    result = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    # The missing project produced an error (no prior sprint file to scaffold
    # from either); the valid one still wrote.
    assert any("sprint file missing for missing-code" in e for e in result.errors)
    assert len(result.files_written) == 1


def test_execute_plan_auto_scaffolds_missing_sprint_file(tmp_path: Path) -> None:
    """When the target sprint file is missing but a prior week's file exists
    for the same project, execute_plan scaffolds the target file from the
    prior week and proceeds with the ingest — rather than dropping the plan.

    This is the v0.13.0 fix for the most common auto-ingest failure class:
    _planning_monday rolls forward late-in-week, so a Wed-Sun meeting tries
    to land in next week's sprint dir which sync hasn't created yet.
    """
    tenant = _make_tenant(tmp_path)  # has W20 ggl-5168 already
    # Rewrite the W20 file as a "prior week" — i.e. give it the Project CP
    # nav link that scaffold_from_prior reads.
    (tenant / "sprints" / "2026-W20" / "ggl-5168.md").write_text(
        "---\nProject: ggl-5168 — Test Project\n"
        "Sprint: 2026-W20\nPriorSprint: 2026-W19\n---\n\n"
        "# ggl-5168 — Test Project · Sprint W20 (May 11 – May 17, 2026)\n\n"
        "← [Project CP](../../1p/google/ggl-5168-test/cp.md) · "
        "[Master](../../master-cp.md) · [Prior sprint](../2026-W19/ggl-5168.md)\n\n"
        "<!-- cp-engine:start sprint-facts -->\n| | |\n|---|---|\n"
        "| Owner | Drew |\n<!-- cp-engine:end sprint-facts -->\n\n"
        "<!-- cp-engine:start where-it-stands -->\n## Where it stands\n\n"
        "<!-- cp-engine:end where-it-stands -->\n\n"
        "<!-- cp-engine:start carry-forward -->\n## Carried over from 2026-W19\n\n"
        "<!-- cp-engine:end carry-forward -->\n\n"
        "## Client communication\n### Open asks\n"
    )
    # Plan targets W21 — which doesn't exist yet.
    plan = {
        "projects": {
            "ggl-5168": {
                "inbound": [{"text": "Late-week meeting note", "date": "2026-05-22", "who": "Rena"}],
            }
        }
    }
    # Wed May 20 2026 = weekday 2, so _planning_monday rolls forward to
    # next Monday (May 25, ISO W22).
    result = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 20))
    assert result.errors == []
    target = tenant / "sprints" / "2026-W22" / "ggl-5168.md"
    assert target.exists()
    body = target.read_text()
    assert "Late-week meeting note" in body


def test_execute_plan_falls_back_to_error_when_no_prior_exists(tmp_path: Path) -> None:
    """A first-ever-ingest for a project that has no prior sprint file at all
    still errors and drops the plan — there's nothing to scaffold from."""
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "brand-new-code": {"asks": [{"text": "First time"}]},
        }
    }
    result = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    assert any("sprint file missing for brand-new-code" in e for e in result.errors)
    assert result.files_written == []


def test_execute_plan_writes_theme_to_week_md(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path)
    plan = {
        "themes": [
            {"text": "Maria transition; Activation pop-up Round 3", "date": "2026-05-12"},
        ]
    }
    result = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    assert result.errors == []
    week_body = (tenant / "sprints" / "2026-W20" / "_week.md").read_text()
    assert "[theme · 2026-05-12] Maria transition" in week_body


def test_execute_plan_writes_slack_digest_under_client_communication(
    tmp_path: Path,
) -> None:
    """The Slack digest pipeline (P.3) writes one bullet per week under
    `## Client communication / ### Slack digest`. The subsection is
    auto-created if missing (the v0.8.5 template doesn't include it)."""
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "ggl-5168": {
                "slack_digest": [
                    {
                        "text": (
                            "Quiet week — Maria sent Geoff revision specs for "
                            "the pop-up preso; Geoff turned them around next day."
                        ),
                        "week": "2026-W20",
                    }
                ],
            }
        }
    }
    result = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    assert result.errors == []
    body = (tenant / "sprints" / "2026-W20" / "ggl-5168.md").read_text()
    # The auto-created subsection lives under Client communication.
    assert "### Slack digest" in body
    assert "[2026-W20 · Slack] Quiet week — Maria sent Geoff" in body
    assert "cp:hash=" in body


def test_execute_plan_slack_digest_idempotent_same_week(tmp_path: Path) -> None:
    """Re-running for the same `(code, week)` is a no-op (hash dedup)."""
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "ggl-5168": {
                "slack_digest": [
                    {"text": "Week summary.", "week": "2026-W20"}
                ],
            }
        }
    }
    r1 = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    r2 = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    assert r1.errors == [] and r2.errors == []
    assert len(r1.files_written) == 1
    assert r2.skipped_duplicate == 1
    assert r2.files_written == []


def test_execute_plan_slack_digest_writes_to_target_week_not_today(
    tmp_path: Path,
) -> None:
    """The Sunday cron runs in W20 but digests W19. `week_iso` overrides
    the today→week derivation so the bullet lands in the right sprint
    file."""
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "ggl-5168": {
                "slack_digest": [
                    {"text": "Last week's chatter.", "week": "2026-W20"}
                ],
            }
        }
    }
    # today is a W20 date, but the digest should still land in W19.
    result = execute_plan(
        plan, tenant_root=tenant, today=date(2026, 5, 18), week_iso="2026-W20"
    )
    assert result.errors == []
    w19 = (tenant / "sprints" / "2026-W20" / "ggl-5168.md").read_text()
    assert "[2026-W20 · Slack] Last week's chatter." in w19


def test_execute_plan_close_ask_flips_open_to_closed(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path)
    # First, write an open ask.
    plan_open = {
        "projects": {
            "ggl-5168": {
                "asks": [{"text": "Approve Round 3", "who": "Rena", "date": "2026-05-08"}],
            }
        }
    }
    execute_plan(plan_open, tenant_root=tenant, today=date(2026, 5, 12))
    # Then close it.
    plan_close = {
        "projects": {
            "ggl-5168": {
                "close-ask": [{"text": "Approve Round 3"}],
            }
        }
    }
    result = execute_plan(plan_close, tenant_root=tenant, today=date(2026, 5, 12))
    assert result.errors == []
    body = (tenant / "sprints" / "2026-W20" / "ggl-5168.md").read_text()
    assert "[open · 2026-05-08" not in body
    assert "[closed · 2026-05-08" in body


def test_close_ask_appends_closed_by_marker_when_provided(tmp_path):
    """A close-ask item with closed_by='clickup' appends an audit-trail marker."""
    from cp_engine.ingest import _write_close_ask

    sprint = tmp_path / "sprint.md"
    sprint.write_text(
        "## Client communication\n\n### Open asks\n\n"
        "- [open · 2026-05-20 · Drew] Confirm ISCI code <!-- cp:hash=abc12345 -->\n"
    )
    item = {"match": "<!-- cp:hash=abc12345 -->", "closed_by": "clickup"}
    changed = _write_close_ask("ggl-5168", item, sprint)
    assert changed is True
    body = sprint.read_text()
    # Status flipped.
    assert "[closed · 2026-05-20 · Drew]" in body
    assert "[open · 2026-05-20 · Drew]" not in body
    # Marker appended.
    assert "<!-- cp:closed-by=clickup -->" in body


def test_close_ask_omits_marker_when_closed_by_absent(tmp_path):
    """Human-run close-ask (no closed_by field) must NOT add a marker."""
    from cp_engine.ingest import _write_close_ask

    sprint = tmp_path / "sprint.md"
    sprint.write_text(
        "## Client communication\n\n### Open asks\n\n"
        "- [open · 2026-05-20 · Drew] Confirm ISCI <!-- cp:hash=abc12345 -->\n"
    )
    item = {"match": "<!-- cp:hash=abc12345 -->"}  # no closed_by
    changed = _write_close_ask("ggl-5168", item, sprint)
    assert changed is True
    body = sprint.read_text()
    assert "[closed · 2026-05-20 · Drew]" in body
    assert "cp:closed-by" not in body  # NO marker


# ──────────────────────────────────────────────────────────────────────
#  Phase B — account_decisions
# ──────────────────────────────────────────────────────────────────────


def test_execute_plan_writes_account_decision_to_weekly_cp(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path, with_weekly_cp=True)
    plan = {
        "account_decisions": [
            {
                "text": "All Google consultant invoices route through Brandon",
                "company": "google",
                "date": "2026-05-13",
            }
        ]
    }
    result = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    assert result.errors == []
    assert (tenant / "weekly-cp.md") in result.files_written
    body = (tenant / "weekly-cp.md").read_text()
    # Highest existing was #3, so new one should be #4.
    assert "4. **All Google consultant invoices route through Brandon**" in body
    assert "(2026-05-13, source: account: google)" in body
    # Hash marker present for idempotency.
    assert "cp:hash=" in body


def test_account_decision_inserts_before_engine_marker(tmp_path: Path) -> None:
    """Account decisions should land in the handwritten section, not
    inside any engine-managed strip region."""
    tenant = _make_tenant(tmp_path, with_weekly_cp=True)
    plan = {
        "account_decisions": [
            {"text": "Test", "company": "google", "date": "2026-05-13"}
        ]
    }
    execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    body = (tenant / "weekly-cp.md").read_text()
    # Find the position of the new decision line and the first engine marker.
    decision_pos = body.find("4. **Test**")
    marker_pos = body.find("<!-- cp-engine:start themes-strip -->")
    assert decision_pos > 0
    assert marker_pos > 0
    assert decision_pos < marker_pos, (
        "account-decision should land before engine markers, "
        "not inside the engine-managed regions"
    )


def test_account_decision_is_idempotent(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path, with_weekly_cp=True)
    plan = {
        "account_decisions": [
            {"text": "Same decision twice", "company": "google", "date": "2026-05-13"}
        ]
    }
    r1 = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    assert r1.skipped_duplicate == 0
    body_after_first = (tenant / "weekly-cp.md").read_text()

    r2 = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    assert r2.skipped_duplicate == 1
    assert r2.files_written == []
    body_after_second = (tenant / "weekly-cp.md").read_text()
    assert body_after_first == body_after_second


def test_account_decision_renumbers_correctly_when_no_existing_decisions(tmp_path: Path) -> None:
    """If weekly-cp.md has no existing numbered decisions, start at #1."""
    week_dir = tmp_path / "sprints" / "2026-W20"
    week_dir.mkdir(parents=True)
    _scaffold_minimal_sprint_file(week_dir / "ggl-5168.md", "ggl-5168")
    (week_dir / "_week.md").write_text("## Themes\n\n- _<theme>_\n")
    # Empty weekly-cp.md (no existing decisions).
    (tmp_path / "weekly-cp.md").write_text("# Weekly CP\n\n## Active research\n\nplaceholder\n")
    plan = {
        "account_decisions": [
            {"text": "First decision", "company": "google", "date": "2026-05-13"}
        ]
    }
    result = execute_plan(plan, tenant_root=tmp_path, today=date(2026, 5, 12))
    assert result.errors == []
    body = (tmp_path / "weekly-cp.md").read_text()
    assert "1. **First decision**" in body


def test_account_decision_validates_required_fields(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path, with_weekly_cp=True)
    # Missing 'text'
    r1 = execute_plan(
        {"account_decisions": [{"company": "google", "date": "2026-05-13"}]},
        tenant_root=tenant, today=date(2026, 5, 12),
    )
    assert any("missing 'text'" in e for e in r1.errors)
    # Missing 'company'
    r2 = execute_plan(
        {"account_decisions": [{"text": "x", "date": "2026-05-13"}]},
        tenant_root=tenant, today=date(2026, 5, 12),
    )
    assert any("missing 'company'" in e for e in r2.errors)


def test_validate_plan_rejects_account_decisions_not_a_list() -> None:
    from cp_engine.ingest import _validate_plan
    with pytest.raises(IngestPlanError, match="account_decisions must be a list"):
        _validate_plan({"account_decisions": "not a list"})  # type: ignore[arg-type]


def test_validate_plan_accepts_account_decisions_alongside_other_blocks() -> None:
    """Plan with projects + themes + account_decisions all together."""
    from cp_engine.ingest import _validate_plan
    plan = {
        "projects": {"ggl-5168": {"asks": [{"text": "x"}]}},
        "themes": [{"text": "t", "date": "2026-05-13"}],
        "account_decisions": [
            {"text": "d", "company": "google", "date": "2026-05-13"}
        ],
    }
    _validate_plan(plan)  # no raise


def test_account_decision_errors_when_weekly_cp_missing(tmp_path: Path) -> None:
    """If weekly-cp.md doesn't exist, account_decisions errors cleanly
    (doesn't raise; logs to result.errors)."""
    week_dir = tmp_path / "sprints" / "2026-W20"
    week_dir.mkdir(parents=True)
    _scaffold_minimal_sprint_file(week_dir / "ggl-5168.md", "ggl-5168")
    (week_dir / "_week.md").write_text("## Themes\n\n")
    # No weekly-cp.md.
    plan = {
        "account_decisions": [
            {"text": "x", "company": "google", "date": "2026-05-13"}
        ]
    }
    result = execute_plan(plan, tenant_root=tmp_path, today=date(2026, 5, 12))
    assert any("weekly-cp.md missing" in e for e in result.errors)
    assert result.files_written == []


# ──────────────────────────────────────────────────────────────────────
#  Phase D.4 — account_summary
# ──────────────────────────────────────────────────────────────────────


def test_account_summary_creates_section_and_writes_bullet(tmp_path: Path) -> None:
    """First account_summary auto-creates the ## Account summaries section."""
    tenant = _make_tenant(tmp_path, with_weekly_cp=True)
    plan = {
        "account_summary": {
            "text": "Maria gave a status across all five GGL projects this week. "
            "5168 launch slipped to 6/8; 5151 interviews wrap; 5176 in client review.",
            "company": "google",
            "week": "2026-W21",
        }
    }
    result = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    assert result.errors == []
    body = (tenant / "weekly-cp.md").read_text()
    assert "## Account summaries" in body
    assert "[2026-W21 · GOOGLE] Maria gave a status" in body
    assert "cp:hash=" in body


def test_account_summary_appends_to_existing_section(tmp_path: Path) -> None:
    """A second account_summary for a different company lands as a sibling
    bullet under the existing section, not a duplicate section header."""
    tenant = _make_tenant(tmp_path, with_weekly_cp=True)
    # First summary creates the section.
    execute_plan(
        {
            "account_summary": {
                "text": "Google week summary.",
                "company": "google",
                "week": "2026-W21",
            }
        },
        tenant_root=tenant,
        today=date(2026, 5, 12),
    )
    # Second summary appends.
    execute_plan(
        {
            "account_summary": {
                "text": "Infoblox week summary.",
                "company": "ibx",
                "week": "2026-W21",
            }
        },
        tenant_root=tenant,
        today=date(2026, 5, 12),
    )
    body = (tenant / "weekly-cp.md").read_text()
    # Exactly one section header, both bullets present.
    assert body.count("## Account summaries") == 1
    assert "[2026-W21 · GOOGLE] Google week summary." in body
    assert "[2026-W21 · IBX] Infoblox week summary." in body


def test_account_summary_idempotent_same_company_same_week(tmp_path: Path) -> None:
    """Re-running for (company, week) is a no-op via hash dedup."""
    tenant = _make_tenant(tmp_path, with_weekly_cp=True)
    plan = {
        "account_summary": {
            "text": "Week summary.",
            "company": "google",
            "week": "2026-W21",
        }
    }
    r1 = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    r2 = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    assert r1.errors == [] and r2.errors == []
    assert len(r1.files_written) == 1
    assert r2.files_written == []
    assert r2.skipped_duplicate == 1


def test_account_summary_same_company_different_week_writes_both(
    tmp_path: Path,
) -> None:
    """The hash key embeds week, so a different week's summary doesn't dedup."""
    tenant = _make_tenant(tmp_path, with_weekly_cp=True)
    execute_plan(
        {
            "account_summary": {
                "text": "Week 19 summary.",
                "company": "google",
                "week": "2026-W20",
            }
        },
        tenant_root=tenant,
        today=date(2026, 5, 6),
    )
    execute_plan(
        {
            "account_summary": {
                "text": "Week 20 summary.",
                "company": "google",
                "week": "2026-W21",
            }
        },
        tenant_root=tenant,
        today=date(2026, 5, 12),
    )
    body = (tenant / "weekly-cp.md").read_text()
    assert "[2026-W20 · GOOGLE]" in body
    assert "[2026-W21 · GOOGLE]" in body


def test_account_summary_validates_required_fields(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path, with_weekly_cp=True)
    # Missing 'text'
    r = execute_plan(
        {"account_summary": {"company": "google", "week": "2026-W21"}},
        tenant_root=tenant,
        today=date(2026, 5, 12),
    )
    assert any("missing 'text'" in e for e in r.errors)
    # Missing 'company'
    r = execute_plan(
        {"account_summary": {"text": "x", "week": "2026-W21"}},
        tenant_root=tenant,
        today=date(2026, 5, 12),
    )
    assert any("missing 'company'" in e for e in r.errors)
    # Missing 'week'
    r = execute_plan(
        {"account_summary": {"text": "x", "company": "google"}},
        tenant_root=tenant,
        today=date(2026, 5, 12),
    )
    assert any("missing 'week'" in e for e in r.errors)


# ──────────────────────────────────────────────────────────────────────
#  Retired Quick Resume verbs (exec-summary cutover)
#
# `current_work`, `next_up`, `blockers` USED to write scalar lines into
# the project cp.md's `quick-resume` region. Auto-ingest no longer writes
# project cp.md state at all (the model authors an exec-summary at wrap-up
# instead). These verbs are now RETIRED: recognized-and-ignored so stale
# in-flight webhook plans don't hard-fail during the deploy window, but
# they perform NO write. Per-meeting truth still flows to the sprint file.
# ──────────────────────────────────────────────────────────────────────


def _make_tenant_with_project_cp(
    tmp_path: Path,
    *,
    code: str = "ggl-5168",
) -> Path:
    """Build a tenant with both a sprint file AND a project cp.md (with an
    engine-managed exec-summary region) for the given code under
    1p/google/<slug>/. Slug is the code itself (no name-suffix), so the
    project dir is 1p/google/<code>/cp.md."""
    tenant = _make_tenant(tmp_path)
    project_cp = tenant / "1p" / "google" / code / "cp.md"
    project_cp.parent.mkdir(parents=True, exist_ok=True)
    project_cp.write_text(
        "# Test Project — Project CP\n\n"
        "<!-- cp-engine:start exec-summary -->\n"
        "## Exec Summary\n\n"
        "_(authored at wrap-up)_\n"
        "<!-- cp-engine:end exec-summary -->\n"
    )
    return tenant


def test_auto_ingest_ignores_retired_quick_resume_verbs(tmp_path: Path) -> None:
    """A plan containing the retired `current_work`/`next_up`/`blockers`
    verbs is silently ignored: no error, and the project cp.md is left
    byte-identical (auto-ingest no longer writes cp.md state)."""
    tenant = _make_tenant_with_project_cp(tmp_path)
    cp_path = tenant / "1p" / "google" / "ggl-5168" / "cp.md"
    before = cp_path.read_bytes()
    plan = {
        "projects": {
            "ggl-5168": {
                "current_work": "Tony+Geoff shipped 5 playbooks; awaiting feedback.",
                "next_up": "Draft chapter 2 by 5/20.",
                "blockers": "None.",
            },
        },
    }
    result = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    # (a) No error raised or recorded.
    assert result.errors == []
    # (b) cp.md is byte-identical — nothing was written.
    assert cp_path.read_bytes() == before
    # (c) cp.md never even appears in files_written.
    assert cp_path not in result.files_written


def test_retired_verb_does_not_poison_sprint_file_verbs(tmp_path: Path) -> None:
    """A mixed plan with a retired QR verb AND a normal sprint-file verb
    (record-inbound) still processes the sprint-file verb correctly. The
    retired verb is ignored; the sprint file IS written; cp.md is not."""
    tenant = _make_tenant_with_project_cp(tmp_path)
    cp_path = tenant / "1p" / "google" / "ggl-5168" / "cp.md"
    before = cp_path.read_bytes()
    plan = {
        "projects": {
            "ggl-5168": {
                "current_work": "Should be ignored entirely.",
                "record-inbound": [
                    {"text": "Inbound bullet", "date": "2026-05-13", "who": "Jane"},
                ],
            },
        },
    }
    result = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    assert result.errors == []
    sprint_path = tenant / "sprints" / "2026-W20" / "ggl-5168.md"
    # Sprint-file verb processed.
    assert sprint_path in result.files_written
    assert "Inbound bullet" in sprint_path.read_text()
    # cp.md untouched (byte-identical) and absent from files_written.
    assert cp_path.read_bytes() == before
    assert cp_path not in result.files_written


def test_retired_verbs_validate_without_error(tmp_path: Path) -> None:
    """The validation pass tolerates the retired verbs — they don't raise
    IngestPlanError even though they're not advertised in _SUPPORTED_VERBS."""
    tenant = _make_tenant_with_project_cp(tmp_path)
    plan = {
        "projects": {
            # Retired verb carrying its old scalar shape (a string).
            "ggl-5168": {"current_work": "stale string value"},
        },
    }
    # Should not raise; should not record an error.
    result = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    assert result.errors == []


def test_retired_verbs_not_advertised_but_recognized() -> None:
    """The retired verbs are OUT of _SUPPORTED_VERBS (no longer advertised)
    but live in _RETIRED_VERBS so the consumer recognizes-and-ignores them
    rather than hard-failing a stale in-flight plan."""
    from cp_engine.ingest import _RETIRED_VERBS, _SUPPORTED_VERBS

    for verb in ("current_work", "next_up", "blockers"):
        assert verb not in _SUPPORTED_VERBS
        assert verb in _RETIRED_VERBS


def _make_tenant_w22(tmp_path: Path) -> Path:
    """Build a tenant whose primary sprint dir is 2026-W22.

    The resolve-risk tests assert on a W22 file because the task spec uses
    dates around 2026-05-27/28 (which would route to W23 under the
    Wed-Sun planning-monday roll-forward). Building W22 directly lets us
    pass `week_iso="2026-W22"` to keep `today=2026-05-28` semantically
    meaningful for the resolved-at marker without routing surprises.
    """
    week_dir = tmp_path / "sprints" / "2026-W22"
    week_dir.mkdir(parents=True)
    _scaffold_minimal_sprint_file(week_dir / "ggl-5168.md", "ggl-5168")
    (week_dir / "_week.md").write_text("## Themes\n\n- _<theme>_\n")
    return tmp_path


def test_execute_plan_resolve_risk_flips_escalated_to_resolved(tmp_path: Path) -> None:
    """A resolve-risk plan item flips `[escalated · ...]` to `[resolved · ...]`
    on the bullet whose cp_hash matches. Date in the bullet stays as raised_date;
    a `cp:resolved-at=<today>` comment is appended for audit."""
    tenant = _make_tenant_w22(tmp_path)
    # Seed an escalated risk via the normal ingest path so the bullet has a hash.
    execute_plan(
        {"projects": {"ggl-5168": {"risks": [
            {"text": "Client keeps reopening locked script", "severity": "escalated",
             "category": "scope", "date": "2026-05-27"},
        ]}}},
        tenant_root=tenant, today=date(2026, 5, 27), week_iso="2026-W22",
    )
    body_before = (tenant / "sprints" / "2026-W22" / "ggl-5168.md").read_text()
    import re as _re
    h = _re.search(r"cp:hash=([0-9a-f]{8})", body_before).group(1)
    result = execute_plan(
        {"projects": {"ggl-5168": {"resolve-risk": [
            {"hash": h, "closed_by": "slack"},
        ]}}},
        tenant_root=tenant, today=date(2026, 5, 28), week_iso="2026-W22",
    )
    assert result.errors == []
    body_after = (tenant / "sprints" / "2026-W22" / "ggl-5168.md").read_text()
    assert "[resolved · scope · 2026-05-27]" in body_after
    assert "[escalated · scope · 2026-05-27]" not in body_after
    assert "cp:resolved-at=2026-05-28" in body_after
    assert "cp:closed-by=slack" in body_after


def test_resolve_risk_is_idempotent(tmp_path: Path) -> None:
    """Running resolve-risk twice with the same hash is a no-op on the second
    call — the bullet is already resolved, no further mutation."""
    tenant = _make_tenant_w22(tmp_path)
    execute_plan(
        {"projects": {"ggl-5168": {"risks": [
            {"text": "Recurring escalation", "severity": "escalated",
             "category": "scope", "date": "2026-05-27"},
        ]}}},
        tenant_root=tenant, today=date(2026, 5, 27), week_iso="2026-W22",
    )
    import re as _re
    body0 = (tenant / "sprints" / "2026-W22" / "ggl-5168.md").read_text()
    h = _re.search(r"cp:hash=([0-9a-f]{8})", body0).group(1)
    execute_plan(
        {"projects": {"ggl-5168": {"resolve-risk": [{"hash": h}]}}},
        tenant_root=tenant, today=date(2026, 5, 28), week_iso="2026-W22",
    )
    body1 = (tenant / "sprints" / "2026-W22" / "ggl-5168.md").read_text()
    r2 = execute_plan(
        {"projects": {"ggl-5168": {"resolve-risk": [{"hash": h}]}}},
        tenant_root=tenant, today=date(2026, 5, 28), week_iso="2026-W22",
    )
    body2 = (tenant / "sprints" / "2026-W22" / "ggl-5168.md").read_text()
    assert body1 == body2  # second run is byte-identical
    assert r2.skipped_duplicate == 1


def test_resolve_risk_silently_dedupes_when_hash_not_found(tmp_path: Path) -> None:
    """Hash not found in sprint file → silent no-op + skipped_duplicate increments.

    Slack-button reruns on stale digest messages are routine (risk was
    already resolved, message scrolled but lingers). Surfacing those as
    errors creates noise. The right semantic is 'nothing to do.'

    Distinguishes from 'missing hash field' (still raises) — that's a
    genuinely malformed plan, not a stale-message click.
    """
    tenant = _make_tenant_w22(tmp_path)
    result = execute_plan(
        {"projects": {"ggl-5168": {"resolve-risk": [{"hash": "deadbeef"}]}}},
        tenant_root=tenant, today=date(2026, 5, 28), week_iso="2026-W22",
    )
    assert result.errors == []
    assert result.skipped_duplicate == 1


def test_resolve_risk_raises_when_hash_field_missing(tmp_path: Path) -> None:
    """Missing 'hash' field is genuinely bad input — surface it as an error."""
    tenant = _make_tenant_w22(tmp_path)
    result = execute_plan(
        {"projects": {"ggl-5168": {"resolve-risk": [{"closed_by": "slack"}]}}},
        tenant_root=tenant, today=date(2026, 5, 28), week_iso="2026-W22",
    )
    assert any("missing 'hash'" in e for e in result.errors)


def test_resolve_risk_omits_closed_by_marker_when_absent(tmp_path: Path) -> None:
    """When closed_by is absent, no cp:closed-by marker is appended."""
    tenant = _make_tenant_w22(tmp_path)
    execute_plan(
        {"projects": {"ggl-5168": {"risks": [
            {"text": "Some risk", "severity": "escalated",
             "category": "scope", "date": "2026-05-27"},
        ]}}},
        tenant_root=tenant, today=date(2026, 5, 27), week_iso="2026-W22",
    )
    body0 = (tenant / "sprints" / "2026-W22" / "ggl-5168.md").read_text()
    import re as _re
    h = _re.search(r"cp:hash=([0-9a-f]{8})", body0).group(1)
    execute_plan(
        {"projects": {"ggl-5168": {"resolve-risk": [{"hash": h}]}}},
        tenant_root=tenant, today=date(2026, 5, 28), week_iso="2026-W22",
    )
    body1 = (tenant / "sprints" / "2026-W22" / "ggl-5168.md").read_text()
    assert "cp:resolved-at=2026-05-28" in body1
    assert "cp:closed-by" not in body1


def test_resolve_risk_is_in_supported_verbs() -> None:
    """Regression guard: _validate_plan rejects any unknown verb, so the
    dispatch table addition is useless without this list edit."""
    from cp_engine.ingest import _SUPPORTED_VERBS
    assert "resolve-risk" in _SUPPORTED_VERBS


# ──────────────────────────────────────────────────────────────────────
#  Snooze writer (Task 1.2 — v0.14.0)
# ──────────────────────────────────────────────────────────────────────


def test_snooze_ask_appends_marker_by_hash(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path)
    execute_plan(
        {"projects": {"ggl-5168": {"asks": [
            {"text": "Approve Round 3", "who": "Rena", "date": "2026-05-12"},
        ]}}},
        tenant_root=tenant, today=date(2026, 5, 12),
    )
    body0 = (tenant / "sprints" / "2026-W20" / "ggl-5168.md").read_text()
    import re as _re
    h = _re.search(r"cp:hash=([0-9a-f]{8})", body0).group(1)
    r = execute_plan(
        {"projects": {"ggl-5168": {"snooze-ask": [
            {"hash": h, "until": "2026-06-01"},
        ]}}},
        tenant_root=tenant, today=date(2026, 5, 12),  # Same Tuesday so file stays in W20
        week_iso="2026-W20",
    )
    assert r.errors == []
    body1 = (tenant / "sprints" / "2026-W20" / "ggl-5168.md").read_text()
    assert "cp:snoozed-until=2026-06-01" in body1
    # Ask is still [open], not flipped to closed.
    assert "[open · 2026-05-12 · Rena]" in body1


def test_snooze_risk_appends_marker_by_hash(tmp_path: Path) -> None:
    tenant = _make_tenant_w22(tmp_path)
    execute_plan(
        {"projects": {"ggl-5168": {"risks": [
            {"text": "Some risk", "severity": "escalated",
             "category": "scope", "date": "2026-05-27"},
        ]}}},
        tenant_root=tenant, today=date(2026, 5, 27), week_iso="2026-W22",
    )
    body0 = (tenant / "sprints" / "2026-W22" / "ggl-5168.md").read_text()
    import re as _re
    h = _re.search(r"cp:hash=([0-9a-f]{8})", body0).group(1)
    execute_plan(
        {"projects": {"ggl-5168": {"snooze-risk": [
            {"hash": h, "until": "2026-07-01"},
        ]}}},
        tenant_root=tenant, today=date(2026, 5, 28), week_iso="2026-W22",
    )
    body1 = (tenant / "sprints" / "2026-W22" / "ggl-5168.md").read_text()
    assert "cp:snoozed-until=2026-07-01" in body1
    assert "[escalated · scope · 2026-05-27]" in body1  # severity unchanged


def test_snooze_replaces_prior_snooze(tmp_path: Path) -> None:
    """Re-snoozing the same item REPLACES the prior until-date, not stacks."""
    tenant = _make_tenant(tmp_path)
    execute_plan(
        {"projects": {"ggl-5168": {"asks": [
            {"text": "Approve Round 3", "who": "Rena", "date": "2026-05-12"},
        ]}}},
        tenant_root=tenant, today=date(2026, 5, 12),
    )
    import re as _re
    body0 = (tenant / "sprints" / "2026-W20" / "ggl-5168.md").read_text()
    h = _re.search(r"cp:hash=([0-9a-f]{8})", body0).group(1)
    # Snooze to date A.
    execute_plan(
        {"projects": {"ggl-5168": {"snooze-ask": [{"hash": h, "until": "2026-06-01"}]}}},
        tenant_root=tenant, today=date(2026, 5, 12), week_iso="2026-W20",
    )
    # Snooze to date B.
    execute_plan(
        {"projects": {"ggl-5168": {"snooze-ask": [{"hash": h, "until": "2026-07-01"}]}}},
        tenant_root=tenant, today=date(2026, 5, 12), week_iso="2026-W20",
    )
    body1 = (tenant / "sprints" / "2026-W20" / "ggl-5168.md").read_text()
    # Only the LATEST until-date is present.
    assert "cp:snoozed-until=2026-07-01" in body1
    assert "cp:snoozed-until=2026-06-01" not in body1
    # Only ONE snooze marker total on the line.
    assert body1.count("cp:snoozed-until=") == 1


def test_snooze_writer_preserves_existing_regex_matching(tmp_path: Path) -> None:
    """REGRESSION GUARD: after snooze-writer mutates a bullet, the existing
    _OPEN_ASK_RE / _RISK_RE must STILL match the line. The hash marker must
    stay at end-of-line; the snooze marker must come BEFORE it.

    This guards against the v0.14 design failure mode where appending the
    snooze marker after the hash would break the digest scanner entirely.
    """
    from cp_engine.attention_digest import _OPEN_ASK_RE, _RISK_RE
    tenant = _make_tenant(tmp_path)
    # Seed an ask + risk
    execute_plan(
        {"projects": {"ggl-5168": {
            "asks": [{"text": "Approve mocks", "who": "Rena", "date": "2026-05-12"}],
            "risks": [{"text": "Scope creep", "severity": "escalated",
                       "category": "scope", "date": "2026-05-12"}],
        }}},
        tenant_root=tenant, today=date(2026, 5, 12),
    )
    body0 = (tenant / "sprints" / "2026-W20" / "ggl-5168.md").read_text()
    assert _OPEN_ASK_RE.search(body0), "open ask should match pre-snooze"
    assert _RISK_RE.search(body0), "risk should match pre-snooze"
    ask_hash = _OPEN_ASK_RE.search(body0).group("hash")
    risk_hash = _RISK_RE.search(body0).group("hash")

    execute_plan(
        {"projects": {"ggl-5168": {
            "snooze-ask": [{"hash": ask_hash, "until": "2026-07-01"}],
            "snooze-risk": [{"hash": risk_hash, "until": "2026-07-01"}],
        }}},
        tenant_root=tenant, today=date(2026, 5, 12), week_iso="2026-W20",
    )
    body1 = (tenant / "sprints" / "2026-W20" / "ggl-5168.md").read_text()
    ask_m = _OPEN_ASK_RE.search(body1)
    risk_m = _RISK_RE.search(body1)
    assert ask_m, "open ask must still match _OPEN_ASK_RE after snoozing"
    assert risk_m, "risk must still match _RISK_RE after snoozing"
    assert ask_m.group("hash") == ask_hash
    assert risk_m.group("hash") == risk_hash
    assert "cp:snoozed-until=2026-07-01" in ask_m.group(0)
    assert "cp:snoozed-until=2026-07-01" in risk_m.group(0)

    from cp_engine.attention_digest import _find_past_due_asks, _find_escalated_risks
    pasts = _find_past_due_asks(
        sprint_files=[tenant / "sprints" / "2026-W20" / "ggl-5168.md"],
        today=date(2026, 5, 28),
    )
    risks = _find_escalated_risks(
        sprint_files=[tenant / "sprints" / "2026-W20" / "ggl-5168.md"],
        today=date(2026, 5, 28), window_days=30,
    )
    assert pasts == [] and risks == [], (
        "snoozed-until in the future should suppress these from the digest"
    )

    from cp_engine.attention_digest import _strip_snooze_marker
    raw_text = ask_m.group("text")
    cleaned = _strip_snooze_marker(raw_text)
    assert "cp:snoozed-until" not in cleaned
    assert "Approve mocks" in cleaned


# ──────────────────────────────────────────────────────────────────────
#  Task 3.3 — hash-based close-ask (Slack button payload)
# ──────────────────────────────────────────────────────────────────────


def test_close_ask_matches_by_hash_when_provided(tmp_path: Path) -> None:
    """close-ask with a `hash` field matches by cp:hash marker, not substring.

    Required by v0.14's Slack-action handler which passes only hash in the
    button payload — there's no text to substring-match against.
    """
    tenant = _make_tenant(tmp_path)
    execute_plan(
        {"projects": {"ggl-5168": {"asks": [
            {"text": "Approve Round 3", "who": "Rena", "date": "2026-05-12"},
        ]}}},
        tenant_root=tenant, today=date(2026, 5, 12),
    )
    import re as _re
    body0 = (tenant / "sprints" / "2026-W20" / "ggl-5168.md").read_text()
    h = _re.search(r"cp:hash=([0-9a-f]{8})", body0).group(1)
    r = execute_plan(
        {"projects": {"ggl-5168": {"close-ask": [{"hash": h, "closed_by": "slack"}]}}},
        tenant_root=tenant, today=date(2026, 5, 28), week_iso="2026-W20",
    )
    assert r.errors == []
    body1 = (tenant / "sprints" / "2026-W20" / "ggl-5168.md").read_text()
    assert "[closed · 2026-05-12 · Rena]" in body1
    assert "cp:closed-by=slack" in body1


def test_close_ask_falls_back_to_text_match_when_hash_absent(tmp_path: Path) -> None:
    """Backward-compat: existing ClickUp pipeline + hand-written close-ask plans
    still match by substring text when no hash is provided."""
    tenant = _make_tenant(tmp_path)
    execute_plan(
        {"projects": {"ggl-5168": {"asks": [
            {"text": "Approve Round 3", "who": "Rena", "date": "2026-05-12"},
        ]}}},
        tenant_root=tenant, today=date(2026, 5, 12),
    )
    r = execute_plan(
        {"projects": {"ggl-5168": {"close-ask": [
            {"text": "Approve Round 3", "closed_by": "clickup"},
        ]}}},
        tenant_root=tenant, today=date(2026, 5, 28), week_iso="2026-W20",
    )
    assert r.errors == []
    body1 = (tenant / "sprints" / "2026-W20" / "ggl-5168.md").read_text()
    assert "[closed · 2026-05-12 · Rena]" in body1
    assert "cp:closed-by=clickup" in body1


# ──────────────────────────────────────────────────────────────────────
#  set-milestone + set-client-ask-task (v0.15 — forward-looking sprint planning)
# ──────────────────────────────────────────────────────────────────────
#
# These verbs do NOT touch the sprint markdown — they insert proposal
# rows into the existing ``clickup_task_proposals`` Supabase table.
# Milestones live in ClickUp; the dashboard surfaces these proposals
# behind a human review-and-approve gate, then the webhook approve path
# (Task 3) creates the real ClickUp tasks.


def _fake_supabase_for_project(*, project_id: str = "p1",
                                clickup_list_id: str | None = "L1",
                                code: str = "ggl-5168",
                                enable_clickup: bool = True,
                                existing_hashes: list[str] | None = None):
    """Mock the chained-builder pattern used by the supabase-py client.

    Returns a ``unittest.mock.MagicMock`` (un-annotated to avoid a
    module-level MagicMock import in this otherwise dependency-light file).

    Configures TWO chain endpoints:

    1. Project lookup (``_resolve_proposal_project``):
        client.table("projects").select(...).eq("number", n).execute().data
        → [{"id": project_id, "number": <int>, "clickup_list_id": ..., "enable_clickup": True}]

    2. Dedupe lookup (``_proposal_already_present``):
        client.table("clickup_task_proposals").select("id, status")
            .eq("cp_ask_hash", h).in_("status", [...]).execute().data
        → [{"id": "...", "status": "pending"}] if `h in existing_hashes`,
          else [].
        ``existing_hashes`` defaults to []; pass a list to simulate
        pre-existing rows so the milestone/client-ask-task verb
        idempotently skips its insert.

    Insert calls (``client.table("clickup_task_proposals").insert(row).execute()``)
    return a MagicMock — tests assert by inspecting ``client.table.call_args_list``
    and the captured insert payload.
    """
    from unittest.mock import MagicMock

    number = int(code.rsplit("-", 1)[-1]) if code.rsplit("-", 1)[-1].isdigit() else None
    project_row = {
        "id": project_id,
        "number": number,
        "clickup_list_id": clickup_list_id,
        "enable_clickup": enable_clickup,
    }
    existing = set(existing_hashes or [])

    client = MagicMock()
    # Project SELECT chain → list of rows.
    select_chain = client.table.return_value.select.return_value.eq.return_value
    select_chain.execute.return_value.data = [project_row]

    # Dedupe SELECT chain (extra `.in_(...)` step) → list of rows when the
    # cp_ask_hash matches `existing_hashes`, else []. We dynamically inspect
    # what hash was passed in by reading ``client.table.return_value
    # .select.return_value.eq.call_args``. Because MagicMock returns the
    # same chain endpoint regardless of arguments, the side_effect on
    # ``.in_(...).execute()`` is where we branch on the captured hash.
    in_chain = (
        client.table.return_value.select.return_value
        .eq.return_value.in_.return_value
    )

    def _dedupe_execute(*_args, **_kwargs):
        # The eq() call right before .in_() carried the cp_ask_hash as its
        # second positional arg: .eq("cp_ask_hash", h).
        eq_calls = client.table.return_value.select.return_value.eq.call_args_list
        last_hash = None
        for call in reversed(eq_calls):
            args = call.args
            if len(args) >= 2 and args[0] == "cp_ask_hash":
                last_hash = args[1]
                break
        result = MagicMock()
        result.data = [{"id": "x", "status": "pending"}] if last_hash in existing else []
        return result

    in_chain.execute.side_effect = _dedupe_execute
    return client


def _fake_supabase_for_initiative(*, initiative_id: str = "i1",
                                   clickup_list_id: str | None = "L9",
                                   code: str = "mission-control",
                                   enable_clickup: bool = True,
                                   existing_hashes: list[str] | None = None):
    """Mirror ``_fake_supabase_for_project`` but for an INITIATIVE (slug code).

    ``_resolve_proposal_project`` routes a slug code (no trailing number)
    through the ``initiatives`` table via ``.eq("code", code)``. The mocked
    chain endpoint is shared with the project path, so we just hand it an
    initiative-shaped row (``code`` slug, no ``number``).
    """
    from unittest.mock import MagicMock

    initiative_row = {
        "id": initiative_id,
        "code": code,
        "clickup_list_id": clickup_list_id,
        "enable_clickup": enable_clickup,
    }
    existing = set(existing_hashes or [])

    client = MagicMock()
    select_chain = client.table.return_value.select.return_value.eq.return_value
    select_chain.execute.return_value.data = [initiative_row]

    in_chain = (
        client.table.return_value.select.return_value
        .eq.return_value.in_.return_value
    )

    def _dedupe_execute(*_args, **_kwargs):
        eq_calls = client.table.return_value.select.return_value.eq.call_args_list
        last_hash = None
        for call in reversed(eq_calls):
            args = call.args
            if len(args) >= 2 and args[0] == "cp_ask_hash":
                last_hash = args[1]
                break
        result = MagicMock()
        result.data = [{"id": "x", "status": "pending"}] if last_hash in existing else []
        return result

    in_chain.execute.side_effect = _dedupe_execute
    return client


def _last_insert_row(client) -> dict:
    """Pull the dict passed to the most recent ``insert(...)`` call."""
    insert_calls = client.table.return_value.insert.call_args_list
    assert insert_calls, "no insert() was called on the mock"
    args, _kwargs = insert_calls[-1]
    return args[0]


def test_resolve_proposal_project_tags_kind_project() -> None:
    """A numbered code resolves to a projects row tagged kind='project'."""
    from cp_engine.ingest import _resolve_proposal_project
    sb = _fake_supabase_for_project(code="ggl-5168")
    resolved = _resolve_proposal_project(sb, "ggl-5168")
    assert resolved is not None
    assert resolved["kind"] == "project"


def test_resolve_proposal_project_tags_kind_initiative() -> None:
    """A slug code resolves to an initiatives row tagged kind='initiative'."""
    from cp_engine.ingest import _resolve_proposal_project
    sb = _fake_supabase_for_initiative(code="mission-control")
    resolved = _resolve_proposal_project(sb, "mission-control")
    assert resolved is not None
    assert resolved["kind"] == "initiative"


def test_set_milestone_initiative_writes_initiative_id(tmp_path: Path) -> None:
    """For an initiative-resolved code, the milestone proposal must write
    initiative_id (not project_id) to satisfy migration 081's
    num_nonnulls(project_id, initiative_id) == 1 CHECK.
    """
    tenant = _make_tenant(tmp_path)
    _scaffold_minimal_sprint_file(
        tenant / "sprints" / "2026-W20" / "mission-control.md", "mission-control"
    )
    sb = _fake_supabase_for_initiative(code="mission-control")
    plan = {
        "projects": {
            "mission-control": {
                "set-milestone": [
                    {
                        "deliverable": "Ship spine UI",
                        "date": "2026-06-30",
                        "owner": "tony",
                        "confidence": "high",
                    }
                ]
            }
        }
    }
    result = execute_plan(
        plan,
        tenant_root=tenant,
        today=date(2026, 5, 12),
        supabase=sb,
        meeting_id="meet-init",
    )
    assert result.errors == [], result.errors
    row = _last_insert_row(sb)
    assert row["initiative_id"] == "i1"
    assert row.get("project_id") is None


def test_set_client_ask_task_initiative_writes_initiative_id(tmp_path: Path) -> None:
    """For an initiative-resolved code, the client-ask proposal must write
    initiative_id (not project_id) to satisfy migration 081's CHECK.
    """
    tenant = _make_tenant(tmp_path)
    _scaffold_minimal_sprint_file(
        tenant / "sprints" / "2026-W20" / "mission-control.md", "mission-control"
    )
    sb = _fake_supabase_for_initiative(code="mission-control")
    plan = {
        "projects": {
            "mission-control": {
                "set-client-ask-task": [
                    {
                        "what": "Confirm spine UI scope",
                        "from_party": "tony",
                    }
                ]
            }
        }
    }
    result = execute_plan(
        plan,
        tenant_root=tenant,
        today=date(2026, 5, 12),
        supabase=sb,
        meeting_id="meet-init",
    )
    assert result.errors == [], result.errors
    row = _last_insert_row(sb)
    assert row["initiative_id"] == "i1"
    assert row.get("project_id") is None


def test_set_milestone_validates() -> None:
    """Well-formed milestone plan passes _validate_plan."""
    from cp_engine.ingest import _validate_plan
    plan = {
        "projects": {
            "ggl-5168": {
                "set-milestone": [
                    {
                        "deliverable": "Pop-up final to Rena",
                        "date": "2026-06-06",
                        "owner": "brandon",
                        "confidence": "medium",
                    }
                ]
            }
        }
    }
    _validate_plan(plan)  # no raise


def test_set_client_ask_task_validates() -> None:
    """Well-formed client-ask plan passes _validate_plan."""
    from cp_engine.ingest import _validate_plan
    plan = {
        "projects": {
            "ggl-5168": {
                "set-client-ask-task": [
                    {
                        "what": "Round 3 pop-up feedback",
                        "from_party": "rena",
                        "expected_by": "2026-06-02",
                    }
                ]
            }
        }
    }
    _validate_plan(plan)  # no raise


def test_unknown_verb_still_rejected() -> None:
    """Adding new verbs must not weaken the unknown-verb gate."""
    from cp_engine.ingest import _validate_plan
    plan = {"projects": {"ggl-5168": {"set-imaginary-thing": [{"text": "x"}]}}}
    with pytest.raises(IngestPlanError, match="unknown verb"):
        _validate_plan(plan)


def test_set_milestone_inserts_row(tmp_path: Path) -> None:
    """Handler inserts the expected row shape into clickup_task_proposals."""
    tenant = _make_tenant(tmp_path)
    sb = _fake_supabase_for_project(code="ggl-5168")
    plan = {
        "projects": {
            "ggl-5168": {
                "set-milestone": [
                    {
                        "deliverable": "Pop-up final to Rena",
                        "date": "2026-06-06",
                        "owner": "brandon",
                        "confidence": "medium",
                        "depends_on": ["pop-up-r3-feedback"],
                        "linked_to": ["ggl-5177"],
                    }
                ]
            }
        }
    }
    result = execute_plan(
        plan,
        tenant_root=tenant,
        today=date(2026, 5, 12),
        supabase=sb,
        meeting_id="meet-abc",
    )
    assert result.errors == [], result.errors
    row = _last_insert_row(sb)
    assert row["project_id"] == "p1"
    assert row["clickup_list_id"] == "L1"
    assert row["task_type"] == "milestone"
    assert row["is_milestone"] is True
    # The description must include the milestone-deliverable text so a
    # reviewer in the dashboard can see what's being proposed without
    # joining tables. Date may be folded into the description (no
    # `due_date` column on the proposals table today).
    assert "Pop-up final to Rena" in row["description"]
    assert "2026-06-06" in row["description"]
    assert row["milestone_confidence"] == "medium"
    assert row["milestone_depends_on"] == ["pop-up-r3-feedback"]
    assert row["milestone_linked_to"] == ["ggl-5177"]
    assert row["meeting_id"] == "meet-abc"
    assert row["status"] == "pending"


def test_set_milestone_missing_clickup_list_raises(tmp_path: Path) -> None:
    """Project with no clickup_list_id surfaces as an ingest error.

    (Result.errors collects per-verb exceptions; the verb wraps its
    own ValueError.) No row is inserted.
    """
    tenant = _make_tenant(tmp_path)
    sb = _fake_supabase_for_project(code="ggl-5168", clickup_list_id=None)
    plan = {
        "projects": {
            "ggl-5168": {
                "set-milestone": [
                    {
                        "deliverable": "Pop-up final",
                        "date": "2026-06-06",
                        "owner": "brandon",
                        "confidence": "medium",
                    }
                ]
            }
        }
    }
    result = execute_plan(
        plan, tenant_root=tenant, today=date(2026, 5, 12), supabase=sb
    )
    assert any("clickup_list_id" in e for e in result.errors), result.errors
    # No insert happened — the project routing failed before that.
    assert not sb.table.return_value.insert.called


def test_set_client_ask_task_inserts_row(tmp_path: Path) -> None:
    """Handler inserts a client_ask proposal with the expected shape."""
    tenant = _make_tenant(tmp_path)
    sb = _fake_supabase_for_project(code="ggl-5168")
    plan = {
        "projects": {
            "ggl-5168": {
                "set-client-ask-task": [
                    {
                        "what": "Round 3 pop-up feedback",
                        "from_party": "rena",
                        "expected_by": "2026-06-02",
                    }
                ]
            }
        }
    }
    result = execute_plan(
        plan,
        tenant_root=tenant,
        today=date(2026, 5, 12),
        supabase=sb,
        meeting_id="meet-abc",
    )
    assert result.errors == [], result.errors
    row = _last_insert_row(sb)
    assert row["project_id"] == "p1"
    assert row["clickup_list_id"] == "L1"
    assert row["task_type"] == "client_ask"
    assert row["is_milestone"] is False
    assert "Round 3 pop-up feedback" in row["description"]
    # Stakeholder name preserved in description until we add a structured field.
    assert "rena" in row["description"].lower()
    assert "2026-06-02" in row["description"]
    assert row["meeting_id"] == "meet-abc"
    assert row["status"] == "pending"


def test_set_client_ask_task_optional_expected_by(tmp_path: Path) -> None:
    """expected_by is optional; absence does not error and is not in description."""
    tenant = _make_tenant(tmp_path)
    sb = _fake_supabase_for_project(code="ggl-5168")
    plan = {
        "projects": {
            "ggl-5168": {
                "set-client-ask-task": [
                    {"what": "Joe to confirm date", "from_party": "joe"}
                ]
            }
        }
    }
    result = execute_plan(
        plan, tenant_root=tenant, today=date(2026, 5, 12), supabase=sb
    )
    assert result.errors == [], result.errors
    row = _last_insert_row(sb)
    assert "Joe to confirm date" in row["description"]


def test_set_milestone_no_supabase_is_skipped(tmp_path: Path) -> None:
    """No supabase client → verb is skipped silently (best-effort), not a hard error.

    Mirrors the resilience pattern in webhook/clickup_propose: ClickUp
    routing must never break the primary auto-ingest contract.
    """
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "ggl-5168": {
                "set-milestone": [
                    {
                        "deliverable": "Pop-up final",
                        "date": "2026-06-06",
                        "owner": "brandon",
                        "confidence": "medium",
                    }
                ],
                # …with a regular file-write verb in the same plan to
                # prove the milestone branch doesn't poison the primary
                # path.
                "asks": [{"text": "A normal ask", "who": "Rena", "date": "2026-05-12"}],
            }
        }
    }
    result = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    # The ask was still written; the milestone was silently skipped.
    assert any("ggl-5168.md" in str(p) for p in result.files_written)
    # No error surfaced for the silently-skipped milestone.
    assert not any("set-milestone" in e for e in result.errors), result.errors


def test_set_milestone_is_in_supported_verbs() -> None:
    """Regression guard: both new verbs must be in the supported tuple."""
    from cp_engine.ingest import _SUPPORTED_VERBS
    assert "set-milestone" in _SUPPORTED_VERBS
    assert "set-client-ask-task" in _SUPPORTED_VERBS


# ──────────────────────────────────────────────────────────────────────
#  v0.15.1 — cp_ask_hash dedupe on ClickUp-proposal verbs
# ──────────────────────────────────────────────────────────────────────
#
# Without dedupe, the v0.13 rerun endpoint re-inserts the same milestone /
# client-ask N times. The hash recipe matches webhook/clickup_propose's
# record-ask path so all proposal verbs use a single round-tripping hash.


def test_set_milestone_inserts_cp_ask_hash(tmp_path: Path) -> None:
    """Hash recipe: _content_hash(code, 'set-milestone', deliverable)."""
    from cp_engine.ingest import _content_hash
    tenant = _make_tenant(tmp_path)
    sb = _fake_supabase_for_project(code="ggl-5168")
    plan = {
        "projects": {
            "ggl-5168": {
                "set-milestone": [
                    {
                        "deliverable": "Pop-up final to Rena",
                        "date": "2026-06-06",
                        "owner": "brandon",
                        "confidence": "medium",
                    }
                ]
            }
        }
    }
    result = execute_plan(
        plan, tenant_root=tenant, today=date(2026, 5, 12), supabase=sb,
    )
    assert result.errors == [], result.errors
    row = _last_insert_row(sb)
    expected = _content_hash("ggl-5168", "set-milestone", "Pop-up final to Rena")
    assert row["cp_ask_hash"] == expected


def test_set_milestone_dedupes_existing_pending(tmp_path: Path) -> None:
    """Re-inserting the same milestone is a silent no-op when an existing
    row in pending/approved status already carries the same cp_ask_hash."""
    from cp_engine.ingest import _content_hash
    tenant = _make_tenant(tmp_path)
    deliverable = "Pop-up final to Rena"
    expected_hash = _content_hash("ggl-5168", "set-milestone", deliverable)
    sb = _fake_supabase_for_project(
        code="ggl-5168", existing_hashes=[expected_hash],
    )
    plan = {
        "projects": {
            "ggl-5168": {
                "set-milestone": [
                    {
                        "deliverable": deliverable,
                        "date": "2026-06-06",
                        "owner": "brandon",
                        "confidence": "medium",
                    }
                ]
            }
        }
    }
    result = execute_plan(
        plan, tenant_root=tenant, today=date(2026, 5, 12), supabase=sb,
    )
    assert result.errors == [], result.errors
    # Dedupe path: no insert was issued.
    assert not sb.table.return_value.insert.called


def test_set_client_ask_task_inserts_cp_ask_hash(tmp_path: Path) -> None:
    """Hash recipe: _content_hash(code, 'set-client-ask-task', what)."""
    from cp_engine.ingest import _content_hash
    tenant = _make_tenant(tmp_path)
    sb = _fake_supabase_for_project(code="ggl-5168")
    plan = {
        "projects": {
            "ggl-5168": {
                "set-client-ask-task": [
                    {
                        "what": "Round 3 pop-up feedback",
                        "from_party": "rena",
                        "expected_by": "2026-06-02",
                    }
                ]
            }
        }
    }
    result = execute_plan(
        plan, tenant_root=tenant, today=date(2026, 5, 12), supabase=sb,
    )
    assert result.errors == [], result.errors
    row = _last_insert_row(sb)
    expected = _content_hash(
        "ggl-5168", "set-client-ask-task", "Round 3 pop-up feedback"
    )
    assert row["cp_ask_hash"] == expected


def test_set_client_ask_task_dedupes_existing_pending(tmp_path: Path) -> None:
    """Same dedupe semantic for client-ask-task."""
    from cp_engine.ingest import _content_hash
    tenant = _make_tenant(tmp_path)
    what = "Round 3 pop-up feedback"
    expected_hash = _content_hash("ggl-5168", "set-client-ask-task", what)
    sb = _fake_supabase_for_project(
        code="ggl-5168", existing_hashes=[expected_hash],
    )
    plan = {
        "projects": {
            "ggl-5168": {
                "set-client-ask-task": [
                    {"what": what, "from_party": "rena"}
                ]
            }
        }
    }
    result = execute_plan(
        plan, tenant_root=tenant, today=date(2026, 5, 12), supabase=sb,
    )
    assert result.errors == [], result.errors
    assert not sb.table.return_value.insert.called


def test_set_milestone_dedupe_lookup_failure_falls_back_to_insert(
    tmp_path: Path,
) -> None:
    """If the dedupe Supabase query errors, _proposal_already_present must
    fall back to allowing the insert — the auto-ingest contract is that
    ClickUp routing never silently drops a real milestone.
    """
    from unittest.mock import MagicMock
    tenant = _make_tenant(tmp_path)
    sb = _fake_supabase_for_project(code="ggl-5168")
    # Make the dedupe lookup raise. The project lookup chain is separate
    # (it's `.eq.return_value.execute`) so it stays intact.
    sb.table.return_value.select.return_value.eq.return_value.in_.return_value.execute.side_effect = RuntimeError(
        "boom"
    )
    plan = {
        "projects": {
            "ggl-5168": {
                "set-milestone": [
                    {
                        "deliverable": "Pop-up final to Rena",
                        "date": "2026-06-06",
                        "owner": "brandon",
                        "confidence": "medium",
                    }
                ]
            }
        }
    }
    result = execute_plan(
        plan, tenant_root=tenant, today=date(2026, 5, 12), supabase=sb,
    )
    assert result.errors == [], result.errors
    # Insert WAS attempted despite the dedupe failure.
    assert sb.table.return_value.insert.called


# ──────────────────────────────────────────────────────────────────────
#  Fix 2 (v0.15.2): sanitize newlines in LLM text fields
# ──────────────────────────────────────────────────────────────────────


def _read_sprint_body(tenant: Path) -> str:
    return (tenant / "sprints" / "2026-W20" / "ggl-5168.md").read_text()


def test_ingest_sanitizes_multiline_inbound_text(tmp_path: Path) -> None:
    """Multi-line LLM text collapses to a single line so the trailing
    ``<!-- cp:hash=... -->`` stays on the same line as the bullet (or
    future close-ask / snooze-ask regex matching will silently fail)."""
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "ggl-5168": {
                "inbound": [
                    {
                        "text": "Line one\nLine two",
                        "date": "2026-05-12",
                        "who": "Rena",
                    }
                ]
            }
        }
    }
    execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    body = _read_sprint_body(tenant)
    assert "Line one Line two" in body
    # Critical: the bullet + hash marker are on the same physical line.
    for line in body.splitlines():
        if "cp:hash=" in line and "Line one" in line:
            assert "Line two" in line, line
            break
    else:
        raise AssertionError("expected single-line bullet with cp:hash trailer")


def test_ingest_sanitizes_multiline_ask_text(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "ggl-5168": {
                "asks": [
                    {
                        "text": "Send us\nthe brief",
                        "who": "Rena",
                        "date": "2026-05-12",
                    }
                ]
            }
        }
    }
    execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    body = _read_sprint_body(tenant)
    assert "Send us the brief" in body


def test_ingest_sanitizes_multiline_decision_text(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "ggl-5168": {
                "decisions": [
                    {"text": "Drop\nClaude\nteam plan", "date": "2026-05-12"}
                ]
            }
        }
    }
    execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    body = _read_sprint_body(tenant)
    assert "Drop Claude team plan" in body


def test_ingest_sanitizes_multiline_risk_text(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "ggl-5168": {
                "risks": [
                    {
                        "text": "Vendor\ndelivery slips",
                        "severity": "watching",
                        "category": "vendor",
                        "date": "2026-05-12",
                    }
                ]
            }
        }
    }
    execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    body = _read_sprint_body(tenant)
    assert "Vendor delivery slips" in body


def test_ingest_sanitizes_multiline_stakeholder(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "ggl-5168": {
                "stakeholders": [
                    {
                        "name": "Rena\nLee",
                        "role": "VP\nmarketing",
                        "context": "owns\nthe brief",
                    }
                ]
            }
        }
    }
    execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    body = _read_sprint_body(tenant)
    assert "Rena Lee" in body
    assert "VP marketing" in body
    assert "owns the brief" in body


def test_ingest_sanitizes_multiline_slack_digest(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "ggl-5168": {
                "slack_digest": [
                    {
                        "text": "Quiet week.\nOne question on briefing.",
                        "week": "2026-W20",
                    }
                ]
            }
        }
    }
    execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    body = _read_sprint_body(tenant)
    assert "Quiet week. One question on briefing." in body


# ──────────────────────────────────────────────────────────────────────
#  Fix 3 (v0.15.2): handlers honor today= for date defaulting
# ──────────────────────────────────────────────────────────────────────


def test_ingest_ask_honors_today_for_default_asked_date(tmp_path: Path) -> None:
    """When ``date`` is omitted, the ask's ``asked`` field defaults to the
    ``today=`` kwarg threaded through execute_plan — not wall-clock today.
    Without the fix, backfilled/test-time ingests stamp the real date."""
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "ggl-5168": {
                "asks": [{"text": "Approve Round 3", "who": "Rena"}],
            }
        }
    }
    # week_iso pinned so today= can be a backfill date that lives in a
    # different ISO week from the scaffolded sprint file.
    execute_plan(
        plan,
        tenant_root=tenant,
        today=date(2025, 1, 1),
        week_iso="2026-W20",
    )
    body = _read_sprint_body(tenant)
    assert "[open · 2025-01-01 · Rena]" in body


def test_ingest_decision_honors_today_for_default_date(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "ggl-5168": {
                "decisions": [{"text": "Ship the thing"}],
            }
        }
    }
    execute_plan(
        plan,
        tenant_root=tenant,
        today=date(2025, 1, 1),
        week_iso="2026-W20",
    )
    body = _read_sprint_body(tenant)
    assert "[decision · 2025-01-01]" in body


def test_ingest_risk_honors_today_for_default_date(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "ggl-5168": {
                "risks": [
                    {
                        "text": "Schedule slip",
                        "severity": "watching",
                        "category": "schedule",
                    }
                ]
            }
        }
    }
    execute_plan(
        plan,
        tenant_root=tenant,
        today=date(2025, 1, 1),
        week_iso="2026-W20",
    )
    body = _read_sprint_body(tenant)
    assert "[watching · schedule · 2025-01-01]" in body
