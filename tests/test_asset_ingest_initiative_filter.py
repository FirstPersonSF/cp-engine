"""Task 8 Part A — active initiatives become eligible for asset ingestion.

`active_ingestable_codes` widens the engagement-only ingestable set to also
include ACTIVE initiatives (`source == "initiative"` + active status), while
keeping the active-client-engagement path exactly as it was.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from cp_engine import asset_ingest_cli
from cp_engine.state import ProjectState


def _state(
    code: str,
    *,
    source: str,
    status: str,
    company_kind: str = "client",
    is_internal: bool = False,
) -> ProjectState:
    return ProjectState(
        code=code,
        name=code,
        source=source,  # type: ignore[arg-type]
        company_kind=company_kind,  # type: ignore[arg-type]
        company_code="GGL",
        company_name="Google",
        status=status,
        is_internal=is_internal,
        owner="drew",
        last_touched=datetime(2026, 6, 20, tzinfo=timezone.utc),
        deadline=None,
    )


class _FakeBackend:
    def __init__(self, states):
        self._states = states

    def read_projects(self, config):
        return self._states


def _patch_backend(monkeypatch, states):
    monkeypatch.setattr(
        asset_ingest_cli,
        "_default_backend_factory",
        lambda backend: _FakeBackend(states),
    )


def _config():
    return SimpleNamespace(sync=SimpleNamespace(backend="mc-2"))


def test_active_initiative_is_ingestable(monkeypatch):
    _patch_backend(
        monkeypatch,
        [_state("mission-control", source="initiative", status="Active")],
    )
    codes = asset_ingest_cli.active_ingestable_codes(_config())
    assert "mission-control" in codes


def test_inactive_initiative_is_not_ingestable(monkeypatch):
    _patch_backend(
        monkeypatch,
        [
            _state("storyos", source="initiative", status="On hold"),
            _state("old-thing", source="initiative", status="Archived"),
            _state("done-thing", source="initiative", status="Done"),
        ],
    )
    assert asset_ingest_cli.active_ingestable_codes(_config()) == []


def test_active_engagement_still_ingestable(monkeypatch):
    _patch_backend(
        monkeypatch,
        [
            _state("ggl-5168", source="engagement", status="Open"),
            _state("ibx-5153", source="engagement", status="Deal"),
            # excluded: closed engagement, internal engagement
            _state("old-9999", source="engagement", status="Closed"),
            _state("int-1", source="engagement", status="Open", is_internal=True),
        ],
    )
    codes = asset_ingest_cli.active_ingestable_codes(_config())
    assert "ggl-5168" in codes
    assert "ibx-5153" in codes
    assert "old-9999" not in codes
    assert "int-1" not in codes


def test_mixed_engagements_and_initiatives(monkeypatch):
    _patch_backend(
        monkeypatch,
        [
            _state("ggl-5168", source="engagement", status="Open"),
            _state("mission-control", source="initiative", status="Active"),
            _state("storyos", source="initiative", status="Done"),
        ],
    )
    codes = asset_ingest_cli.active_ingestable_codes(_config())
    assert set(codes) == {"ggl-5168", "mission-control"}
