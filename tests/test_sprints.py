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
