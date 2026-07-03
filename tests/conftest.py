"""Shared fixtures. Only golden-test plumbing lives here so far — the
`golden_clock` fixture is opt-in (not autouse) and has no effect on the
rest of the suite.
"""

from __future__ import annotations

import pytest

from tests.golden_utils import GOLDEN_ENGINE_VERSION, FrozenDate


@pytest.fixture
def golden_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze every clock/version a full-file renderer reads.

    - render._today_iso → GOLDEN_TODAY (frontmatter Provenance dates)
    - render.ENGINE_VERSION → GOLDEN_ENGINE_VERSION (sprints.py reads
      the same name via `_render.ENGINE_VERSION`, so this covers both,
      and goldens stop churning on every release bump)
    - sprints.date → FrozenDate (render_sprint_scaffold calls
      `date.today()` directly)
    """
    monkeypatch.setattr(
        "cp_engine.render._today_iso", lambda: FrozenDate.today().isoformat()
    )
    monkeypatch.setattr("cp_engine.render.ENGINE_VERSION", GOLDEN_ENGINE_VERSION)
    monkeypatch.setattr("cp_engine.sprints.date", FrozenDate)
