"""Shared fixtures: golden-test plumbing (opt-in) and the mc2_db
client-cache reset (autouse — `mc2_db.get_client` caches per (url, key),
so without a per-test reset a fake client patched in one test would leak
into the next).
"""

from __future__ import annotations

import pytest

from cp_engine import mc2_db
from tests.golden_utils import GOLDEN_ENGINE_VERSION, FrozenDate


@pytest.fixture(autouse=True)
def _reset_mc2_client_cache() -> None:
    mc2_db.reset_client_cache()
    yield
    mc2_db.reset_client_cache()


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
