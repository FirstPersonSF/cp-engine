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
Sprint: 2026-W19
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
    week_dir = tmp_path / "sprints" / "2026-W19"
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
    sprint_body = (tenant / "sprints" / "2026-W19" / "ggl-5168.md").read_text()
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
    body_after_first = (tenant / "sprints" / "2026-W19" / "ggl-5168.md").read_text()
    # Second run: dedupes.
    r2 = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    assert r2.skipped_duplicate == 1
    assert r2.files_written == []
    body_after_second = (tenant / "sprints" / "2026-W19" / "ggl-5168.md").read_text()
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
    body = (tenant / "sprints" / "2026-W19" / "ggl-5168.md").read_text()
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
    # The missing project produced an error; the valid one still wrote.
    assert any("sprint file missing for missing-code" in e for e in result.errors)
    assert len(result.files_written) == 1


def test_execute_plan_writes_theme_to_week_md(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path)
    plan = {
        "themes": [
            {"text": "Maria transition; Activation pop-up Round 3", "date": "2026-05-12"},
        ]
    }
    result = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    assert result.errors == []
    week_body = (tenant / "sprints" / "2026-W19" / "_week.md").read_text()
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
                        "week": "2026-W19",
                    }
                ],
            }
        }
    }
    result = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 12))
    assert result.errors == []
    body = (tenant / "sprints" / "2026-W19" / "ggl-5168.md").read_text()
    # The auto-created subsection lives under Client communication.
    assert "### Slack digest" in body
    assert "[2026-W19 · Slack] Quiet week — Maria sent Geoff" in body
    assert "cp:hash=" in body


def test_execute_plan_slack_digest_idempotent_same_week(tmp_path: Path) -> None:
    """Re-running for the same `(code, week)` is a no-op (hash dedup)."""
    tenant = _make_tenant(tmp_path)
    plan = {
        "projects": {
            "ggl-5168": {
                "slack_digest": [
                    {"text": "Week summary.", "week": "2026-W19"}
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
                    {"text": "Last week's chatter.", "week": "2026-W19"}
                ],
            }
        }
    }
    # today is a W20 date, but the digest should still land in W19.
    result = execute_plan(
        plan, tenant_root=tenant, today=date(2026, 5, 18), week_iso="2026-W19"
    )
    assert result.errors == []
    w19 = (tenant / "sprints" / "2026-W19" / "ggl-5168.md").read_text()
    assert "[2026-W19 · Slack] Last week's chatter." in w19


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
    body = (tenant / "sprints" / "2026-W19" / "ggl-5168.md").read_text()
    assert "[open · 2026-05-08" not in body
    assert "[closed · 2026-05-08" in body


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
    result = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 13))
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
    execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 13))
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
    r1 = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 13))
    assert r1.skipped_duplicate == 0
    body_after_first = (tenant / "weekly-cp.md").read_text()

    r2 = execute_plan(plan, tenant_root=tenant, today=date(2026, 5, 13))
    assert r2.skipped_duplicate == 1
    assert r2.files_written == []
    body_after_second = (tenant / "weekly-cp.md").read_text()
    assert body_after_first == body_after_second


def test_account_decision_renumbers_correctly_when_no_existing_decisions(tmp_path: Path) -> None:
    """If weekly-cp.md has no existing numbered decisions, start at #1."""
    week_dir = tmp_path / "sprints" / "2026-W19"
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
    result = execute_plan(plan, tenant_root=tmp_path, today=date(2026, 5, 13))
    assert result.errors == []
    body = (tmp_path / "weekly-cp.md").read_text()
    assert "1. **First decision**" in body


def test_account_decision_validates_required_fields(tmp_path: Path) -> None:
    tenant = _make_tenant(tmp_path, with_weekly_cp=True)
    # Missing 'text'
    r1 = execute_plan(
        {"account_decisions": [{"company": "google", "date": "2026-05-13"}]},
        tenant_root=tenant, today=date(2026, 5, 13),
    )
    assert any("missing 'text'" in e for e in r1.errors)
    # Missing 'company'
    r2 = execute_plan(
        {"account_decisions": [{"text": "x", "date": "2026-05-13"}]},
        tenant_root=tenant, today=date(2026, 5, 13),
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
    week_dir = tmp_path / "sprints" / "2026-W19"
    week_dir.mkdir(parents=True)
    _scaffold_minimal_sprint_file(week_dir / "ggl-5168.md", "ggl-5168")
    (week_dir / "_week.md").write_text("## Themes\n\n")
    # No weekly-cp.md.
    plan = {
        "account_decisions": [
            {"text": "x", "company": "google", "date": "2026-05-13"}
        ]
    }
    result = execute_plan(plan, tenant_root=tmp_path, today=date(2026, 5, 13))
    assert any("weekly-cp.md missing" in e for e in result.errors)
    assert result.files_written == []
