"""#425: an unreachable MC-2 must fail fast, in production and in the suite.

postgrest 2.34 retries a failed GET three times with 1+2+4s sleeps. Two
guards, each pinned here:

* `mc2_db.get_client` builds its client with postgrest's retry OFF (the
  engine's own one-shot reap retry stays), including the `.schema()`
  sub-clients the cache builds.
* the root conftest fails every non-loopback request at once with an error
  postgrest does not retry, so no test depends on DNS or a retry policy.
"""
from __future__ import annotations

import time

import httpx
import pytest

from cp_engine import mc2_db

FAKE_URL = "https://nonexistent-425.invalid"
FAKE_KEY = "sb_secret_dummy"


def _retry_supported() -> bool:
    try:
        from supabase import ClientOptions

        ClientOptions(postgrest_client_retry=False)
    except TypeError:
        return False
    return True


@pytest.mark.skipif(
    not _retry_supported(), reason="supabase-py < 2.34 has no postgrest retry"
)
def test_get_client_turns_postgrest_retry_off_on_every_sub_client() -> None:
    client = mc2_db.get_client(url=FAKE_URL, key=FAKE_KEY)
    assert client.postgrest.retry_enabled is False
    assert client.schema("estimator").retry_enabled is False


def test_an_unreachable_mc2_read_fails_in_well_under_a_second() -> None:
    client = mc2_db.get_client(url=FAKE_URL, key=FAKE_KEY)
    started = time.monotonic()
    with pytest.raises(httpx.TransportError):
        client.table("projects").select("id").limit(1).execute()
    assert time.monotonic() - started < 1.0


def _blocked_error() -> BaseException:
    with pytest.raises(httpx.TransportError) as caught:
        httpx.get("https://nonexistent-425.invalid/x")
    return caught.value


def test_guard_error_is_not_one_postgrest_retries() -> None:
    # (`import conftest` would resolve to whichever conftest pytest loaded
    # under that name first, so check the error the guard actually raises.)
    err = _blocked_error()
    assert type(err).__name__ == "BlockedNetworkCall"
    assert not isinstance(err, (httpx.NetworkError, httpx.RemoteProtocolError))


def test_guard_blocks_even_a_default_retrying_client() -> None:
    # A client nobody configured (the hosted server builds its own per
    # request) still fails at once under the guard.
    from supabase import create_client

    client = create_client(FAKE_URL, FAKE_KEY)
    started = time.monotonic()
    with pytest.raises(httpx.TransportError) as caught:
        client.table("projects").select("id").limit(1).execute()
    assert type(caught.value).__name__ == "BlockedNetworkCall"
    assert time.monotonic() - started < 1.0


@pytest.mark.live_network
def test_live_network_marker_lifts_the_guard() -> None:
    # `.invalid` never resolves (RFC 6761), so this fails at DNS, not at the
    # guard: the marker really does open the door.
    with pytest.raises(httpx.TransportError) as caught:
        httpx.get("https://nonexistent-425.invalid/x", timeout=5)
    assert type(caught.value).__name__ != "BlockedNetworkCall"
