"""The master prompt reaches the model — and its absence changes nothing.

Guards the mig-139 injection at `plan_from_transcript._call_claude`. The
property that matters most is the NEGATIVE one: a tenant with no priors
authored, or an unreachable MC-2, must produce a call byte-identical to the
pre-139 behaviour. A judgment-shaping feature that fails loud on an empty
store would be worse than the gap it closes.
"""

from __future__ import annotations

import sys
import types

import pytest

from cp_engine import priors
from cp_engine.plan_from_transcript import _call_claude


class _FakeMessages:
    def __init__(self, sink: dict):
        self._sink = sink

    def create(self, **kwargs):
        self._sink.update(kwargs)
        block = types.SimpleNamespace(text="ok")
        return types.SimpleNamespace(content=[block], stop_reason="end_turn")


class _FakeAnthropic:
    """Stands in for the SDK client, capturing the kwargs of the one call."""

    sink: dict = {}

    def __init__(self, **_kwargs):
        self.messages = _FakeMessages(type(self).sink)


@pytest.fixture(autouse=True)
def _fake_sdk(monkeypatch):
    """Install a fake `anthropic` module and a key, and clear the cache.

    `_call_claude` imports `anthropic` inside the function, so injecting a
    module into sys.modules is enough — no network, no real SDK required.
    """
    _FakeAnthropic.sink = {}
    monkeypatch.setitem(
        sys.modules, "anthropic", types.SimpleNamespace(Anthropic=_FakeAnthropic)
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    priors.clear_cache()
    yield
    priors.clear_cache()


def test_priors_are_sent_as_system(monkeypatch):
    monkeypatch.setattr(priors, "resolve_priors", lambda *a, **k: "BE TERSE.")
    _call_claude("hello", model="m", api_key=None, project_id="p1")
    assert _FakeAnthropic.sink["system"] == "BE TERSE."


def test_explicit_priors_bypass_lookup(monkeypatch):
    def _boom(*_a, **_k):  # pragma: no cover — must never be reached
        raise AssertionError("lookup ran despite explicit priors")

    monkeypatch.setattr(priors, "resolve_priors", _boom)
    _call_claude("hello", model="m", api_key=None, priors="INLINE.")
    assert _FakeAnthropic.sink["system"] == "INLINE."


def test_empty_priors_omit_system_entirely(monkeypatch):
    """The pre-139 shape: no `system` key at all, not an empty string."""
    monkeypatch.setattr(priors, "resolve_priors", lambda *a, **k: "")
    _call_claude("hello", model="m", api_key=None, project_id="p1")
    assert "system" not in _FakeAnthropic.sink


def test_caller_can_force_no_priors(monkeypatch):
    """`priors=""` means deliberately none, and must not trigger a lookup."""

    def _boom(*_a, **_k):  # pragma: no cover
        raise AssertionError("lookup ran despite explicit empty priors")

    monkeypatch.setattr(priors, "resolve_priors", _boom)
    _call_claude("hello", model="m", api_key=None, priors="")
    assert "system" not in _FakeAnthropic.sink


def test_unreachable_mc2_degrades_to_no_priors(monkeypatch):
    """An MC-2 outage must not break ingest — it removes priors, nothing else."""
    import cp_engine.mc2_db as mc2_db

    def _explode(*_a, **_k):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(mc2_db, "get_client", _explode)
    assert priors.resolve_priors("p1") == ""
    _call_claude("hello", model="m", api_key=None, project_id="p1")
    assert "system" not in _FakeAnthropic.sink


def test_resolution_is_cached_per_project(monkeypatch):
    """One lookup per project per process; ingest runs are short-lived."""
    calls = {"n": 0}

    class _Resp:
        data = "PRIORS"

    class _Client:
        def rpc(self, *_a, **_k):
            calls["n"] += 1
            return types.SimpleNamespace(execute=lambda: _Resp())

    import cp_engine.mc2_db as mc2_db

    monkeypatch.setattr(mc2_db, "get_client", lambda *a, **k: _Client())
    assert priors.resolve_priors("p1") == "PRIORS"
    assert priors.resolve_priors("p1") == "PRIORS"
    assert calls["n"] == 1

    # A different project is a separate cache entry.
    assert priors.resolve_priors("p2") == "PRIORS"
    assert calls["n"] == 2


def test_empty_result_is_cached_too(monkeypatch):
    """A negative resolution must not re-query on every call in a long run."""

    class _Resp:
        data = ""

    calls = {"n": 0}

    class _Client:
        def rpc(self, *_a, **_k):
            calls["n"] += 1
            return types.SimpleNamespace(execute=lambda: _Resp())

    import cp_engine.mc2_db as mc2_db

    monkeypatch.setattr(mc2_db, "get_client", lambda *a, **k: _Client())
    assert priors.resolve_priors(None) == ""
    assert priors.resolve_priors(None) == ""
    assert calls["n"] == 1
