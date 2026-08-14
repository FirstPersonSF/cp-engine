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


# --- the scope column: engagements must not fall through to initiatives ----


class _FakeQuery:
    """Records .eq() calls so a test can assert WHICH column was filtered."""

    def __init__(self, rows: list[dict], calls: list[tuple[str, object]]):
        self._rows, self.calls = rows, calls

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self.calls.append((col, val))
        return self

    def execute(self):
        class _R:
            data = self._rows
        return _R()


class _FakeClient:
    def __init__(self, rows: list[dict]):
        self._rows, self.calls = rows, []

    def table(self, _name):
        return _FakeQuery(self._rows, self.calls)


def test_sweep_scopes_an_engagement_by_project_id(monkeypatch) -> None:
    """Regression: `resolve_commitment_owner` emits kind='project', never
    'engagement'.

    The filter previously read `"project_id" if kind == "engagement" else
    "initiative_id"` — so EVERY engagement fell through to `initiative_id`
    and matched nothing. `cp commitments-sweep ibx-5192` reported "No open
    commitments match" against 72 real open rows, and that empty result was
    carried into a close-out retro as fact.
    """
    from cp_engine import commitments_sweep as cs

    monkeypatch.setattr(
        cs, "_owner_codes", lambda _c: {}, raising=False
    )
    import cp_engine.commitments as _cmt
    monkeypatch.setattr(
        _cmt, "resolve_commitment_owner",
        lambda _c, _code: {"id": "proj-uuid", "code": "ibx-5192", "kind": "project"},
    )

    client = _FakeClient([_c("c1", "a real open commitment")])
    groups = cs.sweep(client, code="ibx-5192", today=_TODAY)

    assert ("project_id", "proj-uuid") in client.calls, (
        "an engagement must be scoped by project_id"
    )
    assert ("initiative_id", "proj-uuid") not in client.calls
    assert groups, "the open commitment must survive the filter"


def test_sweep_scopes_an_initiative_by_initiative_id(monkeypatch) -> None:
    """The other side of the branch — guards against over-correcting."""
    from cp_engine import commitments_sweep as cs

    monkeypatch.setattr(cs, "_owner_codes", lambda _c: {}, raising=False)
    import cp_engine.commitments as _cmt
    monkeypatch.setattr(
        _cmt, "resolve_commitment_owner",
        lambda _c, _code: {
            "id": "init-uuid", "code": "mission-control", "kind": "initiative",
        },
    )

    client = _FakeClient([])
    cs.sweep(client, code="mission-control", today=_TODAY)

    assert ("initiative_id", "init-uuid") in client.calls
    assert ("project_id", "init-uuid") not in client.calls
