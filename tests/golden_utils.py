"""Golden-file test harness (arch-phase-3, issue #26).

`assert_matches_golden(name, rendered)` byte-compares rendered output
against a committed fixture under `tests/fixtures/golden/`. Regenerate
fixtures with:

    UPDATE_GOLDENS=1 uv run pytest tests/test_golden_render.py tests/test_golden_sprints.py

then re-run WITHOUT the env var to confirm stability — a golden that
changes between two consecutive runs means the renderer (or the test's
fixture state) has hidden nondeterminism that must be pinned, not
committed.

Determinism contract for golden tests (enforced by the `golden_clock`
fixture in conftest.py):
- `cp_engine.render._today_iso` frozen to GOLDEN_TODAY
- `cp_engine.render.ENGINE_VERSION` frozen to GOLDEN_ENGINE_VERSION
  (sprints.py renders via `_render.ENGINE_VERSION`, so one patch covers
  both modules)
- `cp_engine.sprints.date` swapped for a fixed-today date subclass
  (render_sprint_scaffold calls `date.today()` directly)
"""

from __future__ import annotations

import difflib
import os
from datetime import date
from pathlib import Path

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"

# One fixed clock for every golden. 2026-05-13 is a Wednesday inside
# ISO week 2026-W20 — the same reference week the existing render/sprints
# tests use, so agenda staleness math and week labels line up.
GOLDEN_TODAY = date(2026, 5, 13)
GOLDEN_ENGINE_VERSION = "0.0.0-golden"


class FrozenDate(date):
    """date subclass whose today() is pinned to GOLDEN_TODAY.

    Swapped in for the `date` name in cp_engine.sprints so
    `date.today()` inside render_sprint_scaffold is deterministic.
    Classmethod constructors (fromisocalendar, …) are inherited and
    return FrozenDate instances, which behave as plain dates.
    """

    @classmethod
    def today(cls) -> "FrozenDate":
        return cls(GOLDEN_TODAY.year, GOLDEN_TODAY.month, GOLDEN_TODAY.day)


def assert_matches_golden(name: str, rendered: str) -> None:
    """Byte-compare `rendered` against `tests/fixtures/golden/<name>`.

    `name` carries its own extension (e.g. "render/master-cp.md",
    "sprints/roundtrip.json") and may include subdirectories.

    With UPDATE_GOLDENS=1 in the environment the fixture is (re)written
    and the assertion passes. Without it, a missing fixture or any byte
    difference fails with a unified diff.
    """
    golden_path = GOLDEN_DIR / name

    if os.environ.get("UPDATE_GOLDENS") == "1":
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(rendered, encoding="utf-8")
        return

    if not golden_path.exists():
        raise AssertionError(
            f"golden fixture missing: {golden_path}\n"
            "Generate it with UPDATE_GOLDENS=1 and commit the result."
        )

    expected = golden_path.read_text(encoding="utf-8")
    if rendered == expected:
        return

    diff = "\n".join(
        difflib.unified_diff(
            expected.splitlines(),
            rendered.splitlines(),
            fromfile=f"golden/{name}",
            tofile="rendered",
            lineterm="",
        )
    )
    raise AssertionError(
        f"rendered output diverges from golden fixture {name!r}.\n"
        "If the change is intentional, regenerate with UPDATE_GOLDENS=1 "
        "and review the fixture diff.\n\n" + diff
    )
