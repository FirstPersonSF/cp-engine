"""#339 — the sprint week rolls on Wednesday in the TENANT's timezone.

`cxp sync` handed the week functions a UTC clock, so W+1 appeared at 17:00
Pacific every Tuesday; the webhook and hosted server run on Railway, whose
local clock is UTC. These pin the roll to the tenant's wall clock.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from cp_engine import clock, config
from cp_engine.sprints import (
    _monday_of,
    _planning_monday,
    current_sprint_week_iso,
    prior_sprint_week_iso,
)

# Tue 2026-09-29 23:11 PDT == Wed 2026-09-30 06:11 UTC — the 09-29 incident.
TUE_NIGHT_UTC = datetime(2026, 9, 30, 6, 11, tzinfo=timezone.utc)
# Wed 2026-09-30 00:05 PDT == 07:05 UTC — the roll is due.
WED_MORNING_UTC = datetime(2026, 9, 30, 7, 5, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pacific():
    clock.set_tenant_timezone(clock.DEFAULT_TIMEZONE)
    yield
    clock.set_tenant_timezone(clock.DEFAULT_TIMEZONE)


def test_tuesday_evening_pacific_is_still_this_week():
    assert current_sprint_week_iso(TUE_NIGHT_UTC) == "2026-W40"
    assert prior_sprint_week_iso(TUE_NIGHT_UTC) == "2026-W39"
    assert _planning_monday(TUE_NIGHT_UTC) == date(2026, 9, 28)


def test_wednesday_in_pacific_rolls_forward():
    assert current_sprint_week_iso(WED_MORNING_UTC) == "2026-W41"


def test_calendar_monday_uses_the_tenant_date():
    # Sun 23:30 PDT is Mon 06:30 UTC — still the week that started 09-21.
    sunday_night = datetime(2026, 9, 28, 6, 30, tzinfo=timezone.utc)
    assert _monday_of(sunday_night) == date(2026, 9, 21)


def test_naive_datetimes_are_tenant_wall_clock():
    # A naive value is read as-is — existing tests and callers pass dates.
    assert current_sprint_week_iso(datetime(2026, 9, 29, 23, 11)) == "2026-W40"
    assert current_sprint_week_iso(datetime(2026, 9, 30, 0, 5)) == "2026-W41"


def test_timezone_is_configurable():
    clock.set_tenant_timezone("UTC")
    assert current_sprint_week_iso(TUE_NIGHT_UTC) == "2026-W41"


def test_tenant_now_is_naive_wall_clock_in_the_zone():
    now = clock.tenant_now()
    assert now.tzinfo is None
    expected = datetime.now(clock.tenant_timezone()).replace(tzinfo=None)
    assert abs((expected - now).total_seconds()) < 5


def _tenant(tmp_path: Path, tenant_extra: str = "") -> Path:
    (tmp_path / ".cp-engine.toml").write_text(
        f'[tenant]\nname = "t"\n{tenant_extra}\n'
        '[engine]\nversion = ">= 0"\n'
        '[sync]\nbackend = "github-issues"\n'
    )
    return tmp_path


def test_config_defaults_to_pacific(tmp_path):
    cfg = config.load(_tenant(tmp_path))
    assert cfg.timezone == "America/Los_Angeles"


def test_config_sets_the_process_clock(tmp_path):
    cfg = config.load(_tenant(tmp_path, 'timezone = "America/New_York"'))
    assert cfg.timezone == "America/New_York"
    assert str(clock.tenant_timezone()) == "America/New_York"


def test_config_rejects_a_bad_zone(tmp_path):
    with pytest.raises(config.CommittedConfigInvalid, match="timezone"):
        config.load(_tenant(tmp_path, 'timezone = "Pacific/Nowhere"'))


def test_a_calendar_date_keeps_its_day_through_to_datetime():
    """v0.126.0 regression: `agenda.to_datetime` built an aware midnight UTC,
    which the tenant clock shifted to the previous Pacific day — Wednesday's
    planning bundle said W40 while the sprint files said W41."""
    from cp_engine.agenda import to_datetime

    assert current_sprint_week_iso(to_datetime(date(2026, 9, 30))) == "2026-W41"
    assert _monday_of(to_datetime(date(2026, 9, 28))) == date(2026, 9, 28)
