"""Suite-wide guard: the code under test is THIS checkout's (#357).

The shared `.venv` installs `cp_engine` editable from the main clone. A
`pytest` run from a git worktree (`../cp-engine-<n>`) then imported MAIN's
`cp_engine`: green runs and failing controls alike said nothing about the
branch. `pythonpath = ["src"]` in pyproject.toml is the fix; this is the
check that the fix is in force, so losing it fails loudly instead of
silently testing the wrong tree.

It lives at the repo root, not in `tests/`, so a run of only
`prototypes/hosted-mcp` is guarded too.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest


def cp_engine_outside_rootdir(rootdir: Path) -> str | None:
    """Where `cp_engine` was imported from, when that is NOT under `rootdir`."""
    import cp_engine

    where = Path(cp_engine.__file__).resolve()
    return None if where.is_relative_to(Path(rootdir).resolve()) else str(where)


def pytest_configure(config):
    stray = cp_engine_outside_rootdir(config.rootpath)
    if stray:
        raise pytest.UsageError(
            f"cp_engine imported from {stray}, outside this checkout "
            f"({config.rootpath}). The suite would test another tree's code. "
            f"Check `pythonpath` under [tool.pytest.ini_options] (#357)."
        )


# ── No real HTTP from the suite (#425) ─────────────────────────────────
#
# ~250 tests run sync / ingest / webhook paths whose MC-2 reads are
# best-effort, against fake hosts (`example.supabase.co` and friends). They
# pass BECAUSE those reads fail and the code carries on. Before this guard the
# failure came from DNS, so each test's speed and outcome were whatever the
# network and the installed postgrest's retry policy said: postgrest 2.34
# added 1+2+4s of backoff per failed GET, and CI hit its 20-minute limit.
#
# Now every non-loopback request fails at once, offline, with an error that
# is a `httpx.TransportError` (what the engine's fail-soft paths and its
# reap-retry catch) but NOT a `NetworkError`, which is the class postgrest
# retries. So no client, whatever its retry setting, sleeps here. Loopback
# stays open for the local servers some tests start. A test that really means
# to reach the network says so with `@pytest.mark.live_network`.


class BlockedNetworkCall(httpx.TransportError):
    """A test tried to reach a non-loopback host (#425)."""


_LOOPBACK = {"localhost", "127.0.0.1", "::1", "testserver"}
_NETWORK_ALLOWED = False
_real_sync = httpx.HTTPTransport.handle_request
_real_async = httpx.AsyncHTTPTransport.handle_async_request


def _check(request) -> None:
    if not _NETWORK_ALLOWED and request.url.host not in _LOOPBACK:
        raise BlockedNetworkCall(
            f"real HTTP to {request.url.host} blocked in tests (#425); mark "
            "the test @pytest.mark.live_network if it is meant to reach it",
            request=request,
        )


def _guarded_sync(self, request):
    _check(request)
    return _real_sync(self, request)


async def _guarded_async(self, request):
    _check(request)
    return await _real_async(self, request)


httpx.HTTPTransport.handle_request = _guarded_sync
httpx.AsyncHTTPTransport.handle_async_request = _guarded_async


@pytest.fixture(autouse=True)
def _network_allowed_only_when_marked(request):
    global _NETWORK_ALLOWED
    _NETWORK_ALLOWED = request.node.get_closest_marker("live_network") is not None
    yield
    _NETWORK_ALLOWED = False
