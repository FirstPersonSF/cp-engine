"""Tests for cp_engine.agenda — v0.8.8 sprint planning agenda renderer."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from cp_engine.agenda import (
    SYNC_STALENESS_THRESHOLD_MINUTES,
    WeeklyDecision,
    _strip_hash_marker,
    decisions_for_project,
    extract_quick_resume,
    is_sync_stale,
    master_cp_last_sync,
    normalize_owner,
    parse_weekly_decisions,
)


# ──────────────────────────────────────────────────────────────────────
#  parse_weekly_decisions
# ──────────────────────────────────────────────────────────────────────


def test_parse_weekly_decisions_extracts_numbered_entries() -> None:
    body = """
## Decisions (cross-cutting, last 4 weeks)

19. **Marcello hours triage for W19** — drop website work this week. (2026-05-11, source: sprint planning)

18. **Begin transition off the Claude team plan** for Brandon and Marcello. (2026-05-11, source: sprint planning)

3. **Drew handles Firebase ownership transfer** for Go Safety. (2026-05-08, source: ggl-5136)
"""
    decisions = parse_weekly_decisions(body)
    assert len(decisions) == 3
    assert decisions[0].number == 19
    assert decisions[0].date == "2026-05-11"
    assert decisions[0].sources == ("sprint planning",)
    assert decisions[2].number == 3
    assert decisions[2].sources == ("ggl-5136",)


def test_parse_weekly_decisions_handles_multi_source() -> None:
    body = """
8. **Infoblox AI-campaign workshop downsized** to 2026-06-08 sessions. (2026-05-07, source: ibx-5167 / ibx-5153)
"""
    decisions = parse_weekly_decisions(body)
    assert len(decisions) == 1
    assert decisions[0].sources == ("ibx-5167", "ibx-5153")


def test_parse_weekly_decisions_stops_at_engine_marker() -> None:
    """Decisions auto-aggregated by the v0.8.5 decisions-strip region
    should NOT be picked up here (they're already surfaced via
    aggregators.aggregate_tenant_strips). Parser truncates at the
    first cp-engine marker so we only consume handwritten content."""
    body = """
3. **Drew handles Firebase** for Go Safety. (2026-05-08, source: ggl-5136)

<!-- cp-engine:start decisions-strip -->
## Decisions (cross-cutting, auto-aggregated)
99. **Auto-decision** body. (2026-05-12, source: ggl-5168)
<!-- cp-engine:end decisions-strip -->
"""
    decisions = parse_weekly_decisions(body)
    # Only #3 (above the marker) — #99 is inside the engine region.
    assert len(decisions) == 1
    assert decisions[0].number == 3


def test_parse_weekly_decisions_returns_empty_for_no_matches() -> None:
    assert parse_weekly_decisions("") == ()
    assert parse_weekly_decisions("just some text\nwith no decisions") == ()


# ──────────────────────────────────────────────────────────────────────
#  decisions_for_project
# ──────────────────────────────────────────────────────────────────────


def test_decisions_for_project_filters_by_source_code() -> None:
    decisions = (
        WeeklyDecision(
            number=1, text="Maria off", date="2026-05-08",
            sources=("weekly account meeting",),
        ),
        WeeklyDecision(
            number=3, text="Firebase transfer", date="2026-05-08",
            sources=("ggl-5136",),
        ),
        WeeklyDecision(
            number=8, text="Workshop downsized", date="2026-05-07",
            sources=("ibx-5167", "ibx-5153"),
        ),
    )
    assert [d.number for d in decisions_for_project("ggl-5136", decisions)] == [3]
    assert [d.number for d in decisions_for_project("ibx-5167", decisions)] == [8]
    assert [d.number for d in decisions_for_project("ibx-5153", decisions)] == [8]
    # Code that nothing references → empty.
    assert decisions_for_project("ggl-5168", decisions) == ()


def test_decisions_for_project_is_case_insensitive() -> None:
    decisions = (
        WeeklyDecision(
            number=3, text="x", date="2026-05-08", sources=("GGL-5136",),
        ),
    )
    assert len(decisions_for_project("ggl-5136", decisions)) == 1
    assert len(decisions_for_project("GGL-5136", decisions)) == 1


# ──────────────────────────────────────────────────────────────────────
#  extract_quick_resume (now reads the exec-summary region)
# ──────────────────────────────────────────────────────────────────────


def test_extract_quick_resume_reads_exec_summary() -> None:
    """Sourced from the exec-summary region: real content returned cleaned,
    with no marker lines and no `_<...>_` placeholder lines."""
    body = """
<!-- cp-engine:start exec-summary -->
## Exec Summary  ·  updated 2026-06-30

**Last session:** 2026-06-28
**Objective:** _<one line>_
**Status:** Round 3 with Rena, awaiting feedback.

**Where it stands:**
- Pop-up Round 3 shared with Rena.

**Next up:**
- _<concrete near-term moves>_

**Blockers:**
- None
<!-- cp-engine:end exec-summary -->

## Current Work

Other content.
"""
    out = extract_quick_resume(body)
    assert out is not None
    assert "Round 3 with Rena" in out
    assert "Pop-up Round 3 shared with Rena." in out
    # No marker lines and no placeholder lines survive.
    assert "cp-engine:start" not in out
    assert "cp-engine:end" not in out
    assert "_<" not in out
    assert "Other content" not in out  # outside the region


def test_extract_quick_resume_none_when_unauthored() -> None:
    """All-placeholder region (incl. only the migration bullet) → None."""
    body = """
<!-- cp-engine:start exec-summary -->
## Exec Summary  ·  updated 2026-06-30

**Last session:** _<date>_
**Objective:** _<one line>_
**Status:** _<current state in a phrase>_

**Where it stands:**
- _<2-4 dense bullets of current reality>_

**Next up:**
- _<concrete near-term moves>_

**Blockers:**
- _<or "None">_

**Updates:**
- 2026-06-30 — migrated from Quick Resume
<!-- cp-engine:end exec-summary -->
"""
    assert extract_quick_resume(body) is None


def test_extract_quick_resume_returns_none_for_missing_section() -> None:
    body = "## Some Other Section\n\ncontent"
    assert extract_quick_resume(body) is None


def test_strip_hash_marker_removes_trailing_idempotency_comment() -> None:
    """The v0.8.6 ingest verbs append `<!-- cp:hash=<sha8> -->` to bullets
    for re-run dedup. The agenda renderer should strip that marker so the
    rendered surface stays clean."""
    assert _strip_hash_marker("Some text <!-- cp:hash=abc12345 -->") == "Some text"
    assert _strip_hash_marker("Multi-word text body  <!-- cp:hash=00000001 -->  ") == "Multi-word text body"
    # No marker → unchanged.
    assert _strip_hash_marker("Plain text") == "Plain text"
    # Marker not at end → unchanged (only trailing markers get stripped).
    assert _strip_hash_marker("<!-- cp:hash=abc -->prefix-marker stays") == "<!-- cp:hash=abc -->prefix-marker stays"


def test_extract_quick_resume_strips_template_lines_when_mixed() -> None:
    """If the exec-summary region has both real content AND placeholder lines,
    keep the real content and drop the placeholders."""
    body = """
<!-- cp-engine:start exec-summary -->
## Exec Summary  ·  updated 2026-06-30

**Status:** Real ongoing work here.
**Last session:** _<date>_
**Blockers:** _<or "None">_
<!-- cp-engine:end exec-summary -->

## Next Section
"""
    out = extract_quick_resume(body)
    assert out is not None
    assert "Real ongoing work" in out
    assert "_<date>_" not in out


# ──────────────────────────────────────────────────────────────────────
#  v0.8.8.2: normalize_owner
# ──────────────────────────────────────────────────────────────────────


def test_normalize_owner_collapses_drew_variants_to_single_key() -> None:
    """MC-2 has 'Drew Fiero', 'Drew', 'Drew + Tony', 'Drew and Tony',
    'Drew and Marcello' all referring to Drew (sometimes co-owned).
    All should bucket together for the workload summary."""
    assert normalize_owner("Drew Fiero") == "drew"
    assert normalize_owner("Drew") == "drew"
    assert normalize_owner("Drew + Tony") == "drew"
    assert normalize_owner("Drew and Tony") == "drew"
    assert normalize_owner("Drew and Marcello") == "drew"


def test_normalize_owner_keeps_distinct_first_names_distinct() -> None:
    assert normalize_owner("Brandon Grande") == "brandon"
    assert normalize_owner("Tony Welch") == "tony"
    assert normalize_owner("Marcello Grande") == "marcello"


def test_normalize_owner_handles_empty_and_none() -> None:
    assert normalize_owner(None) == "(unowned)"
    assert normalize_owner("") == "(unowned)"
    assert normalize_owner("   ") == "(unowned)"


# NB: defensive normalization for edge-case inputs like "+ Tony" isn't tested
# because no MC-2 owner string starts with punctuation; behavior is undefined
# and doesn't matter for the real workload bucketing.


# ──────────────────────────────────────────────────────────────────────
#  v0.8.8.2: master_cp_last_sync + is_sync_stale
# ──────────────────────────────────────────────────────────────────────


def test_master_cp_last_sync_returns_none_for_missing_file(tmp_path: Path) -> None:
    from types import SimpleNamespace
    config = SimpleNamespace(root=tmp_path)
    assert master_cp_last_sync(config) is None  # type: ignore[arg-type]


def test_master_cp_last_sync_parses_engine_region(tmp_path: Path) -> None:
    from types import SimpleNamespace
    (tmp_path / "master-cp.md").write_text("""
# Master CP

<!-- cp-engine:start last-sync-timestamp -->
**Last sync:** 2026-05-12T01:38:14.975502+00:00
<!-- cp-engine:end last-sync-timestamp -->
""")
    config = SimpleNamespace(root=tmp_path)
    ts = master_cp_last_sync(config)  # type: ignore[arg-type]
    assert ts is not None
    assert ts.year == 2026 and ts.month == 5 and ts.day == 12
    assert ts.hour == 1 and ts.minute == 38


def test_is_sync_stale_returns_true_when_no_master_cp(tmp_path: Path) -> None:
    from types import SimpleNamespace
    config = SimpleNamespace(root=tmp_path)
    # No master-cp.md → conservatively stale (run sync to be safe).
    assert is_sync_stale(config) is True  # type: ignore[arg-type]


def test_is_sync_stale_returns_false_for_recent_sync(tmp_path: Path) -> None:
    from datetime import datetime, timezone
    from types import SimpleNamespace
    now_iso = datetime.now(timezone.utc).isoformat()
    (tmp_path / "master-cp.md").write_text(f"""
<!-- cp-engine:start last-sync-timestamp -->
**Last sync:** {now_iso}
<!-- cp-engine:end last-sync-timestamp -->
""")
    config = SimpleNamespace(root=tmp_path)
    assert is_sync_stale(config) is False  # type: ignore[arg-type]


def test_is_sync_stale_returns_true_for_old_sync(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    old_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    (tmp_path / "master-cp.md").write_text(f"""
<!-- cp-engine:start last-sync-timestamp -->
**Last sync:** {old_iso}
<!-- cp-engine:end last-sync-timestamp -->
""")
    config = SimpleNamespace(root=tmp_path)
    assert is_sync_stale(config) is True  # type: ignore[arg-type]


def test_sync_staleness_threshold_is_10_minutes() -> None:
    """Sanity check: the documented threshold matches the constant."""
    assert SYNC_STALENESS_THRESHOLD_MINUTES == 10
