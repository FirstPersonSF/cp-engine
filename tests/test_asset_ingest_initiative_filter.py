"""Task 8 Part A — internal workstreams are eligible for asset ingestion.

`active_ingestable_codes` covers every active (Deal | Open) client workstream
plus every active internal workstream (self company, no agreement). One
status vocabulary since #301; `is_internal` gates nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from cp_engine import asset_ingest_cli
from cp_engine.state import ProjectState


def _state(
    code: str,
    *,
    has_agreement: bool,
    status: str,
    company_kind: str = "client",
    is_internal: bool = False,
) -> ProjectState:
    return ProjectState(
        code=code,
        name=code,
        has_agreement=has_agreement,
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
        [_state("1pi-9005-mission-control", has_agreement=False, status="Open", company_kind="self-fpsf")],
    )
    codes = asset_ingest_cli.active_ingestable_codes(_config())
    assert "1pi-9005-mission-control" in codes


def test_inactive_initiative_is_not_ingestable(monkeypatch):
    _patch_backend(
        monkeypatch,
        [
            _state("cnc-9004-storyos", has_agreement=False, status="Holding", company_kind="self-canonic"),
            _state("1pi-9007-old-thing", has_agreement=False, status="Archived", company_kind="self-fpsf"),
            _state("1pi-9008-done-thing", has_agreement=False, status="Closed", company_kind="self-fpsf"),
        ],
    )
    assert asset_ingest_cli.active_ingestable_codes(_config()) == []


def test_active_engagement_still_ingestable(monkeypatch):
    _patch_backend(
        monkeypatch,
        [
            _state("ggl-5168", has_agreement=True, status="Open"),
            _state("ibx-5153", has_agreement=True, status="Deal"),
            # excluded: closed engagement
            _state("old-9999", has_agreement=True, status="Closed"),
            # INCLUDED since #301: `is_internal` is MC-2's flag as stored and
            # gates nothing — the internal workstreams carry it and deserve
            # ingest like any other active workstream.
            _state("int-1", has_agreement=True, status="Open", is_internal=True),
        ],
    )
    codes = asset_ingest_cli.active_ingestable_codes(_config())
    assert "ggl-5168" in codes
    assert "ibx-5153" in codes
    assert "old-9999" not in codes
    assert "int-1" in codes


def test_mixed_engagements_and_initiatives(monkeypatch):
    _patch_backend(
        monkeypatch,
        [
            _state("ggl-5168", has_agreement=True, status="Open"),
            _state("1pi-9005-mission-control", has_agreement=False, status="Open", company_kind="self-fpsf"),
            _state("cnc-9004-storyos", has_agreement=False, status="Closed", company_kind="self-canonic"),
            # a self-company row WITH an agreement is house territory: out
            _state("1pi-9009-house-job", has_agreement=True, status="Open", company_kind="self-fpsf"),
        ],
    )
    codes = asset_ingest_cli.active_ingestable_codes(_config())
    assert set(codes) == {"ggl-5168", "1pi-9005-mission-control"}
