"""Tests for cp_engine.commitments_sweep (#135) — grouping, filters, render."""
from __future__ import annotations

from datetime import date

from cp_engine.commitments_sweep import SweepRow, _row, render_sweep

_TODAY = date(2026, 8, 6)


def _c(
    id: str,
    desc: str,
    *,
    due: str | None = None,
    created: str = "2026-07-14",
    source_kind: str = "meeting_ingest",
    date_status: str = "proposed",
) -> dict:
    return {
        "id": id,
        "description": desc,
        "owner_email": "marcello@firstperson.is",
        "owner_name": "Marcello Grande",
        "due_date": due,
        "date_status": date_status,
        "status": "open",
        "source_kind": source_kind,
        "project_id": "p1",
        "initiative_id": None,
        "created_at": created + "T12:00:00+00:00",
    }


def test_row_ages_and_flags() -> None:
    r = _row(_c("a", "Old undated", created="2026-07-14"), _TODAY)
    assert r.age_days == 23
    assert r.undated and r.stale
    assert r.ttl == "expire"  # undated meeting_ingest proposed, 23d

    dated = _row(_c("b", "Dated", due="2026-08-04"), _TODAY)
    assert not dated.undated and not dated.stale and dated.ttl is None

    fresh = _row(_c("c", "Fresh", created="2026-08-01"), _TODAY)
    assert fresh.undated and not fresh.stale and fresh.ttl is None

    session = _row(_c("d", "Session", source_kind="session"), _TODAY)
    assert session.stale and session.ttl is None  # stale but TTL-exempt


def test_render_sweep_shapes() -> None:
    rows = [
        _row(_c("a", "Add Fred's fear to Hopes & Fears board"), _TODAY),
        _row(_c("b", "Write up notes from the Jul 29 customer session",
                due="2026-08-04", source_kind="session"), _TODAY),
        _row(_c("c", "Future thing", due="2026-08-20", date_status="agreed"), _TODAY),
    ]
    text = render_sweep({"sap-5174-vision-update-2026": rows}, today=_TODAY)
    assert "sap-5174-vision-update-2026 — 3 open" in text
    assert "⚠ UNDATED · 23d" in text
    assert "[meeting_ingest]  Marcello Grande" in text
    assert "SLIPPED · due 2026-08-04 (2d ago)" in text
    assert "due 2026-08-20 [agreed]" in text
    assert "past TTL, expires next dates loop" in text
    assert "3 open across 1 project(s)" in text
    assert "1 stale" in text


def test_render_sweep_empty() -> None:
    assert render_sweep({}, today=_TODAY) == "No open commitments match."


def test_sweep_row_properties() -> None:
    r = SweepRow(
        id="x", description="d", owner="o", source_kind="manual",
        due_date=None, date_status="proposed", age_days=14, ttl=None,
    )
    assert r.stale
