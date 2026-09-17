"""VENDORED from `cp_engine.dates_loop` — the commitment TTL clock only.

`commitments_sweep` needs exactly two symbols from `dates_loop`, but the real
module imports `cp_engine.config.TenantConfig`, which pulls `packaging` and the
CLI's wider dependency tree into a container that installs six packages.

The TTL is a POLICY number, not an implementation detail: an undated
meeting-ingest commitment expires at 14 days, and the sweep is the surface that
catches it while it can still be acted on. A second spelling of that number
would mean the CLI and the hosted server disagree about when work lapses — so a
drift test asserts both symbols match the source module exactly.
"""

from __future__ import annotations

from datetime import date, datetime


_EXPIRE_AFTER_DAYS = 14
_EXPIRE_WARN_AFTER_DAYS = 7


def _ttl_bucket(c: dict, today: date) -> str | None:
    """'expire' | 'warn' | None for one open commitment row.

    Age counts from created_at. warn = would expire by the next weekly
    run; expire = past the TTL now.
    """
    if (
        c.get("due_date")
        or c.get("source_kind") != "meeting_ingest"
        or (c.get("date_status") or "proposed") != "proposed"
    ):
        return None
    raw = c.get("created_at") or ""
    try:
        created = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        return None
    age = (today - created).days
    if age >= _EXPIRE_AFTER_DAYS:
        return "expire"
    if age >= _EXPIRE_WARN_AFTER_DAYS:
        return "warn"
    return None
