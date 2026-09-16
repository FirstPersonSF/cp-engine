"""`spine_stats` is retired — these tests pin the retirement, not the reports.

mc-2 migration 072 dropped `spine_elements`, the table all three reports were
computed from, and the replacement (`spine_substance`) does not carry the
type / stage / target_date columns they keyed on. The previous tests in this
file asserted the report output; they were removed with the reports.

What is worth holding onto is that the module fails SOFT: querying the dropped
table raised PGRST205 and crashed `cxp spine-stats` outright, so the contract
now is "returns empty, never raises, and says so via ELEMENTS_TABLE_RETIRED".
"""
from datetime import date

from cp_engine.spine_stats import (
    ELEMENTS_TABLE_RETIRED,
    due_soon,
    stage_distribution,
    type_inventory,
)


class _ExplodingClient:
    """Any DB access at all is a bug — the reports must not touch MC-2."""

    def table(self, name):  # pragma: no cover - reaching this IS the failure
        raise AssertionError(
            f"spine_stats queried {name!r}; it is retired and must not hit MC-2"
        )

    def schema(self, name):  # pragma: no cover
        raise AssertionError("spine_stats must not hit MC-2")


def test_module_declares_itself_retired():
    assert ELEMENTS_TABLE_RETIRED is True


def test_reports_return_empty_without_touching_the_database():
    """Empty, and — the point of the fix — no PGRST205 on a dropped table."""
    client = _ExplodingClient()
    assert type_inventory(client) == []
    assert stage_distribution(client) == {}
    assert due_soon(client, today=date(2026, 6, 15), within_days=14) == []


def test_due_soon_accepts_its_old_signature():
    """Callers still pass today/within_days; the stub must not break on them."""
    assert due_soon(_ExplodingClient(), today=date(2026, 1, 1)) == []
    assert due_soon(_ExplodingClient(), today=date(2026, 1, 1), within_days=90) == []
