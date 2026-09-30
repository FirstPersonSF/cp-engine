"""The tenant's wall clock (#339).

Sprint weeks roll on Wednesday in the TENANT's timezone, the way MC-2's
`planningWeekMonday()` does in the browser. Before v0.125.1 the engine took
its dates from whatever clock the caller had: `cxp sync` used UTC, so the
week rolled at 17:00 Pacific on Tuesdays; the webhook runs on Railway, whose
local clock is UTC, so `date.today()` there was a UTC date too.

Timestamps stay UTC. Dates and week labels come from here.

A NAIVE datetime is read as tenant wall-clock time (that is what every
`datetime.now()` in the engine meant on a laptop). An AWARE one is converted
to the tenant timezone before its date is taken.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "America/Los_Angeles"

_tz = ZoneInfo(DEFAULT_TIMEZONE)


def set_tenant_timezone(name: str) -> None:
    """Set the process's tenant timezone. `config.load` calls this with
    `[tenant].timezone`; raises `ZoneInfoNotFoundError` on a bad name."""
    global _tz
    _tz = ZoneInfo(name)


def tenant_timezone() -> ZoneInfo:
    return _tz


def tenant_now() -> datetime:
    """Tenant wall-clock time, NAIVE — a drop-in for `datetime.now()` that
    gives the same answer on a laptop and on a UTC server."""
    return datetime.now(_tz).replace(tzinfo=None)


def tenant_today() -> date:
    """The tenant's calendar date — a drop-in for `date.today()`."""
    return datetime.now(_tz).date()


def local_date(value: datetime | date) -> date:
    """The tenant calendar date of `value` (see the module note on naive
    vs aware)."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(_tz)
        return value.date()
    return value
