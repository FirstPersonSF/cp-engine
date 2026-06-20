"""Tests for backfill_source_coords.py's migration-077 column preflight guard."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_source_coords.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("backfill_source_coords", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Query:
    def __init__(self, exc: Exception | None):
        self._exc = exc

    def select(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        if self._exc is not None:
            raise self._exc
        return type("R", (), {"data": []})()


class _FakeClient:
    def __init__(self, exc: Exception | None = None):
        self._exc = exc

    def table(self, _name):
        return _Query(self._exc)


def test_preflight_exits_cleanly_when_column_missing():
    mod = _load_script()
    client = _FakeClient(
        Exception("column rag_assets.source_provider does not exist (42703)")
    )
    with pytest.raises(SystemExit) as ei:
        mod._preflight_columns(client)
    assert ei.value.code == 2


def test_preflight_passes_when_column_present():
    mod = _load_script()
    mod._preflight_columns(_FakeClient(None))  # no raise → ok


def test_preflight_reraises_unrelated_error():
    mod = _load_script()
    client = _FakeClient(RuntimeError("network unreachable"))
    with pytest.raises(RuntimeError):
        mod._preflight_columns(client)
