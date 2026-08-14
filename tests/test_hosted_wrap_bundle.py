"""The hosted `wrap_bundle` fold must not drift from the engine's (#184).

`prototypes/hosted-mcp/server.py` deliberately does NOT import cp_engine — it
copies row shapes. That convention keeps the prototype standalone, and it also
means a fix on one side can silently miss the other. These tests load the
hosted module by AST and check its copied fold against the real one.

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
    """Exec ONLY the copied fold functions, not the whole server module.

    The server imports MCP/Supabase machinery that has no business being a
    test dependency, so we lift the pure functions out by AST instead.
    """
    tree = ast.parse(_HOSTED.read_text(encoding="utf-8"))
    wanted = {
        "_wrap_as_date", "wrap_summarize_meetings", "wrap_summarize_effort",
    }
    # The fold reads module-level constants (WRAP_EFFORT_NOTE et al), so
    # lift those assignments too — otherwise exec raises NameError.
    picked = [
        n for n in tree.body
        if (isinstance(n, ast.FunctionDef) and n.name in wanted)
        or (
            # Both plain and ANNOTATED assignments — the constants are a
            # mix of `X = ...` and `X: tuple[...] = ...`.
            isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id.startswith("WRAP_")
                for t in n.targets
            )
        )
        or (
            isinstance(n, ast.AnnAssign)
            and isinstance(n.target, ast.Name)
            and n.target.id.startswith("WRAP_")
        )
    ]
    got = {n.name for n in picked if isinstance(n, ast.FunctionDef)}
    assert got == wanted, (
        f"hosted server is missing fold functions: {wanted - got}"
    )
    # Constants first, then functions: exec order matters, and the AST
    # slice does not preserve module ordering across the two node types.
    consts = [n for n in picked if not isinstance(n, ast.FunctionDef)]
    funcs = [n for n in picked if isinstance(n, ast.FunctionDef)]
    ns: dict = {
        "datetime": dt.datetime, "date": dt.date, "timedelta": dt.timedelta,
        "Counter": __import__("collections").Counter, "Any": object,
    }
    exec(  # noqa: S102 — executing our own source, by design
        compile(
            ast.Module(body=consts + funcs, type_ignores=[]),
            str(_HOSTED), "exec",
        ),
        ns,   # ONE namespace: the fold reads its constants as globals, so
              # globals and locals must be the same dict.
        ns,
    )
    return ns


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
