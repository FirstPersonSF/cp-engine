from pathlib import Path

import pytest

from cp_engine.sprints import parse_sprint_file


def test_parse_sprint_file_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_sprint_file(tmp_path / "missing.md")


def test_parse_sprint_file_extracts_frontmatter(tmp_path: Path) -> None:
    f = tmp_path / "peb.md"
    f.write_text(
        "---\n"
        "Project: peb — Pebble Foods\n"
        "Sprint: 2026-W19\n"
        "PriorSprint: 2026-W18\n"
        "---\n"
        "# peb — Pebble Foods · Sprint W19 (May 11 – May 17, 2026)\n"
    )
    sf = parse_sprint_file(f)
    assert sf.project_code == "peb"
    assert sf.week_iso == "2026-W19"
    assert sf.prior_sprint == "2026-W18"
    assert sf.week_start == "2026-05-11"
    assert sf.week_end == "2026-05-17"


def test_parse_sprint_file_handles_year_boundary_dates(tmp_path: Path) -> None:
    f = tmp_path / "peb.md"
    f.write_text(
        "---\n"
        "Project: peb — Pebble Foods\n"
        "Sprint: 2026-W53\n"
        "---\n"
        "# peb — Pebble Foods · Sprint W53 (Dec 28 – Jan 3, 2027)\n"
    )
    sf = parse_sprint_file(f)
    # The heading anchors on the year following the date range; the test
    # asserts that BOTH dates parse against that year, regardless of when
    # the test runs. Without the fix, the start date would be stamped as
    # the current year rather than the heading's year (2027).
    assert sf.week_start == "2027-12-28"
    assert sf.week_end == "2027-01-03"


def test_parse_sprint_facts_region(tmp_path) -> None:
    f = tmp_path / "peb.md"
    f.write_text(
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W19\n---\n"
        "# peb — Pebble Foods · Sprint W19 (May 11 – May 17, 2026)\n\n"
        "<!-- cp-engine:start sprint-facts -->\n"
        "| | |\n|---|---|\n"
        "| Stage | Negotiation |\n"
        "| Owner | Drew |\n"
        "| Budget | $45,000 |\n"
        "| Last touched | 2 days ago |\n"
        "| Last sprint hours | Drew 6.5h · Tony 2h |\n"
        "| Sessions this week | 3 |\n"
        "| Open issues | 3 |\n"
        "<!-- cp-engine:end sprint-facts -->\n"
    )
    sf = parse_sprint_file(f)
    assert sf.facts.stage == "Negotiation"
    assert sf.facts.owner == "Drew"
    assert sf.facts.budget_short == "$45,000"
    assert sf.facts.sessions_this_week == 3
    assert sf.facts.open_issues == 3


def test_parse_client_section_extracts_outbound_asks_inbound(tmp_path) -> None:
    f = tmp_path / "peb.md"
    f.write_text(
        "---\nProject: peb — Pebble Foods\nSprint: 2026-W19\n---\n"
        "# peb — Pebble Foods · Sprint W19 (May 11 – May 17, 2026)\n"
        "## Client communication\n\n"
        "### Outbound\n"
        "- [sent · 2026-05-09] Counter-proposal pricing draft sent to Maria + Sam\n"
        "- [draft · queued] Schedule contracting call for week of May 18\n"
        "  Send after their pricing response lands\n"
        "\n### Open asks\n"
        "- [open · 2026-05-04 · Maria] Revised volume forecast from ops team\n"
        "  Asked May 4 · blocking pricing finalization\n"
        "\n### Inbound\n"
        "- [2026-05-09 · Maria] \"Tier-2 cap doesn't match our 2H projections.\"\n"
    )
    sf = parse_sprint_file(f)
    assert len(sf.client_outbound) == 2
    assert sf.client_outbound[0].status == "sent"
    assert sf.client_outbound[0].date == "2026-05-09"
    assert sf.client_open_asks[0].who == "Maria"
    assert sf.client_open_asks[0].asked_date == "2026-05-04"
    assert sf.client_inbound[0].who == "Maria"
