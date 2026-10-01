"""The hosted `wrap_bundle` fold must not drift from the engine's (#184).

Since architecture plan step 1 the hosted meetings fold IS the engine's
(`wrap_report.summarize_meetings`, serialized); the effort fold is still a
copy (tie order differs — see server.py). These tests drive the real hosted
module against the engine.

The tail-window rule is the specific thing at risk: anchoring on `today`
instead of the last meeting reports a 0% tail share for a project that was
entirely back-loaded, which is the single most useful signal in a wrap report.
"""
from __future__ import annotations

import ast
import datetime as dt
from pathlib import Path

import pytest

_HOSTED = (
    Path(__file__).resolve().parent.parent
    / "prototypes" / "hosted-mcp" / "server.py"
)


@pytest.fixture(scope="module")
def hosted_fold():
    """The real hosted module's fold functions (architecture plan step 1:
    the server imports cp_engine, so there is no AST slice to lift)."""
    import importlib.util
    import os

    pytest.importorskip("jwt")
    pytest.importorskip("mcp")
    os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
    os.environ.setdefault("SUPABASE_ANON_KEY", "anon-key-for-tests")
    spec = importlib.util.spec_from_file_location("hosted_mcp_server_wrap", _HOSTED)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        "_wrap_as_date": module._wrap_as_date,
        "wrap_summarize_meetings": module.wrap_summarize_meetings,
        "wrap_summarize_effort": module.wrap_summarize_effort,
        "module": module,
    }


def _mtg(day: str, minutes: int) -> dict:
    return {"meeting_date": f"{day}T16:00:00+00:00", "duration_minutes": minutes}


def test_hosted_tail_window_anchors_on_last_meeting(hosted_fold) -> None:
    """The rule the hosted docstring says has no test. Now it has one."""
    rows = [_mtg("2026-06-01", 60), _mtg("2026-08-13", 300)]
    out = hosted_fold["wrap_summarize_meetings"](rows, tail_days=14)
    # Hosted emits the PAYLOAD shape (hours), not the engine's intermediate
    # (minutes) — it feeds the JSON directly.
    assert out["tail_hours"] == 5.0, (
        "the closing burst must count even when the run happens later"
    )
    assert out["tail_share"] > 0.8


def test_hosted_meetings_fold_is_the_engines(hosted_fold) -> None:
    """Every key of the payload, including heaviest_days and first/last."""
    from cp_engine.wrap_report import summarize_meetings

    rows = [_mtg("2026-06-25", 68), _mtg("2026-08-06", 310), _mtg("2026-08-06", 40),
            {"meeting_date": None, "duration_minutes": 30}]
    engine = summarize_meetings(rows, tail_days=14)
    host = hosted_fold["wrap_summarize_meetings"](rows, tail_days=14)
    assert host["first"] == engine.first.isoformat()
    assert host["last"] == engine.last.isoformat()
    assert host["heaviest_days"] == [
        {"date": d, "meetings": c, "minutes": m} for d, c, m in engine.heaviest_days
    ]
    from cp_engine import wrap_report

    assert hosted_fold["_wrap_as_date"] is wrap_report._as_date
    assert hosted_fold["module"]._engine_wrap_report is wrap_report
    empty = hosted_fold["wrap_summarize_meetings"]([], tail_days=14)
    assert empty == {"count": 0, "total_hours": 0.0, "first": None, "last": None,
                     "tail_days": 14, "tail_share": 0.0, "tail_hours": 0.0,
                     "head_hours": 0.0, "heaviest_days": []}


def test_hosted_fold_matches_the_engine(hosted_fold) -> None:
    """Byte-for-byte agreement with cp_engine.wrap_report on real shapes."""
    from cp_engine.wrap_report import summarize_meetings

    rows = [
        _mtg("2026-06-25", 68), _mtg("2026-07-20", 72),
        _mtg("2026-08-06", 310), _mtg("2026-08-12", 213),
        {"meeting_date": None, "duration_minutes": 30},        # malformed
        {"meeting_date": "2026-08-02T00:00:00Z", "duration_minutes": "x"},
    ]
    for tail in (7, 14, 30):
        engine = summarize_meetings(rows, tail_days=tail)
        host = hosted_fold["wrap_summarize_meetings"](rows, tail_days=tail)
        assert host["count"] == engine.count
        assert host["total_hours"] == engine.total_hours
        assert host["tail_hours"] == round(engine.tail_minutes / 60.0, 1)
        assert host["head_hours"] == round(engine.head_minutes / 60.0, 1)
        assert host["tail_share"] == round(engine.tail_share, 3)


def test_hosted_effort_matches_the_engine(hosted_fold) -> None:
    from cp_engine.wrap_report import summarize_effort

    names = {"e1": "Geoff Ahmann", "e2": "Drew Fiero"}
    rows = [
        {"entity_id": "e1", "hours": 120, "week_start": "2026-08-03"},
        {"entity_id": "e2", "hours": 45.5, "week_start": "2026-08-10"},
        {"entity_id": "ghost", "hours": 12, "week_start": "2026-08-10"},
        {"entity_id": "e1", "hours": "bad", "week_start": "2026-08-10"},
    ]
    engine = summarize_effort(rows, names)
    host = hosted_fold["wrap_summarize_effort"](rows, names)
    assert host["total_hours"] == engine.total_hours
    assert host["weeks"] == engine.weeks
    assert [(p["name"], p["hours"]) for p in host["by_person"]] == engine.by_person


def test_hosted_never_reaches_for_today_in_the_fold() -> None:
    """Guard the exact regression the docstring warns about, at source level.

    A future edit that "fixes" the window by using today would pass the
    shape tests above only if it also happened to be run on the right day.
    """
    import inspect

    from cp_engine import wrap_report

    src = inspect.getsource(wrap_report.summarize_meetings)
    assert "date.today()" not in src.split('"""')[-1]
    src = _HOSTED.read_text(encoding="utf-8")
    start = src.index("def wrap_summarize_meetings")
    end = src.index("def wrap_summarize_effort")
    fold = src[start:end]
    code = "\n".join(
        ln for ln in fold.split("\n") if not ln.strip().startswith("#")
    )
    # Strip the docstring, which legitimately names date.today().
    body = code.split('"""')[-1]
    assert "date.today()" not in body, (
        "the tail window must anchor on the last meeting, never on today"
    )
