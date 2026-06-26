"""In-process TTL listing cache (`_cached_listing`) — unit tests.

The cache is a module-level dict keyed by `(provider, folder_id)`. It memoizes a
folder's recursive tree-walk so back-to-back scans of the SAME folder within one
process don't re-walk it. It's TTL'd and the clock is injectable so expiry is
deterministic in tests.

Because the cache is a MODULE-level dict, it persists across tests in one session
— every test here clears it first (autouse fixture) so state never leaks between
cases or in from other modules' runs.
"""

from __future__ import annotations

import pytest

from cp_engine.asset_ingest import _cached_listing, _clear_listing_cache


@pytest.fixture(autouse=True)
def _reset_cache():
    """The module-level cache survives the whole pytest session — reset it before
    AND after each test so neither this module's cases nor other test modules
    leak cached listings into one another."""
    _clear_listing_cache()
    yield
    _clear_listing_cache()


def _counting_lister(refs):
    """A fake lister returning `refs`, counting how many times it's invoked.

    Returns `(lister, calls)` where `calls` is a single-element list used as a
    mutable counter (closure-friendly)."""
    calls = [0]

    def lister():
        calls[0] += 1
        return refs

    return lister, calls


def test_within_ttl_calls_lister_once():
    clk = [0.0]
    now = lambda: clk[0]  # noqa: E731 — terse controllable clock
    refs = ["a", "b"]
    lister, calls = _counting_lister(refs)

    first = _cached_listing("drive", "F1", lister, ttl=600.0, now=now)
    # Clock unchanged → second call is a cache hit, lister NOT re-invoked.
    second = _cached_listing("drive", "F1", lister, ttl=600.0, now=now)

    assert calls[0] == 1
    assert first == refs
    assert second == refs
    assert second is first  # the cached object is returned, not a recomputation


def test_after_ttl_recomputes():
    clk = [0.0]
    now = lambda: clk[0]  # noqa: E731
    lister, calls = _counting_lister(["x"])

    _cached_listing("drive", "F1", lister, ttl=600.0, now=now)
    assert calls[0] == 1

    # Advance the injected clock PAST the ttl → the entry is stale → recompute.
    clk[0] = 601.0
    _cached_listing("drive", "F1", lister, ttl=600.0, now=now)
    assert calls[0] == 2

    # Exactly AT the ttl boundary is still stale (strict `<` comparison): advance
    # exactly ttl from the last (re)compute and confirm another recompute.
    clk[0] = 601.0 + 600.0
    _cached_listing("drive", "F1", lister, ttl=600.0, now=now)
    assert calls[0] == 3


def test_ttl_zero_never_caches():
    clk = [0.0]
    now = lambda: clk[0]  # noqa: E731
    lister, calls = _counting_lister(["x"])

    _cached_listing("drive", "F1", lister, ttl=0, now=now)
    _cached_listing("drive", "F1", lister, ttl=0, now=now)
    _cached_listing("drive", "F1", lister, ttl=0, now=now)

    # ttl=0 disables the cache entirely → every call recomputes.
    assert calls[0] == 3


def test_different_folder_ids_are_separate_entries():
    clk = [0.0]
    now = lambda: clk[0]  # noqa: E731
    lister1, calls1 = _counting_lister(["one"])
    lister2, calls2 = _counting_lister(["two"])

    a1 = _cached_listing("drive", "F1", lister1, ttl=600.0, now=now)
    b1 = _cached_listing("drive", "F2", lister2, ttl=600.0, now=now)
    # Repeat each key — both should be cache hits independently.
    a2 = _cached_listing("drive", "F1", lister1, ttl=600.0, now=now)
    b2 = _cached_listing("drive", "F2", lister2, ttl=600.0, now=now)

    assert calls1[0] == 1
    assert calls2[0] == 1
    assert a1 == a2 == ["one"]
    assert b1 == b2 == ["two"]


def test_same_folder_id_different_provider_are_separate_entries():
    clk = [0.0]
    now = lambda: clk[0]  # noqa: E731
    lister_d, calls_d = _counting_lister(["drive-refs"])
    lister_x, calls_x = _counting_lister(["dropbox-refs"])

    # Same folder id string under different providers must NOT collide — the key
    # is (provider, folder_id).
    d = _cached_listing("drive", "SHARED", lister_d, ttl=600.0, now=now)
    x = _cached_listing("dropbox", "SHARED", lister_x, ttl=600.0, now=now)

    assert calls_d[0] == 1
    assert calls_x[0] == 1
    assert d == ["drive-refs"]
    assert x == ["dropbox-refs"]


def test_clear_resets_cache():
    clk = [0.0]
    now = lambda: clk[0]  # noqa: E731
    lister, calls = _counting_lister(["x"])

    _cached_listing("drive", "F1", lister, ttl=600.0, now=now)
    assert calls[0] == 1
    # Still a hit before clearing.
    _cached_listing("drive", "F1", lister, ttl=600.0, now=now)
    assert calls[0] == 1

    _clear_listing_cache()

    # After clearing, the next call must recompute (lister invoked again).
    _cached_listing("drive", "F1", lister, ttl=600.0, now=now)
    assert calls[0] == 2
