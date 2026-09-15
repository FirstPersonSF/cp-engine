"""The one unauthenticated surface: can you tell what is running here?

WHY THIS TEST EXISTS. On 2026-09-15 `capture_project_state` was merged,
released in v0.116.3, and the hosted service was up and serving 200s — and
there was no way to tell from outside whether the running build had the verb.
`/mcp` is auth-gated by design, so reading the tool list needs a user token.
Answering the question took comparing a Railway deploy timestamp against a git
log; the running build turned out to predate the verb by six hours.

`tool_count` is the part that matters for this service. `commit` is usually
"unknown" because it deploys by `railway up` from a CLI rather than a
GitHub trigger, and `cp_engine_version` only moves on a release — but a verb
added to the file changes the count either way.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture
def server(monkeypatch):
    for k, v in {
        "MC2_API_BASE": "http://example.invalid",
        "SUPABASE_URL": "http://example.invalid",
        "SUPABASE_ANON_KEY": "x",
    }.items():
        monkeypatch.setenv(k, v)
    import server as mod

    return mod


@pytest.mark.anyio
async def test_health_reports_the_running_code(server):
    """The fields that answer 'is my verb deployed?'."""
    from starlette.requests import Request

    resp = await server.health(None)
    import json

    body = json.loads(resp.body)

    assert body["status"] == "healthy"
    # The version that moves on a release.
    assert body["cp_engine_version"]
    # The structural check a version bump cannot give.
    assert isinstance(body["tool_count"], int) and body["tool_count"] > 0
    assert body["server_version"].startswith("hosted-cp-spike/")


@pytest.mark.anyio
async def test_capture_project_state_is_counted(server):
    """The verb whose absence started this. If it is in the file, it is served."""
    tools = await server.mcp_server.list_tools()
    names = {t.name for t in tools}
    assert "capture_project_state" in names
    assert "capture_session" in names


@pytest.mark.anyio
async def test_commit_is_unknown_rather_than_wrong(server, monkeypatch):
    """A `railway up` deploy injects no commit — say so, never guess.

    Better an explicit unknown than a value that looks authoritative and is
    stale, which is the failure this endpoint exists to prevent.
    """
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    import json

    body = json.loads((await server.health(None)).body)
    assert body["commit"] == "unknown"


@pytest.fixture
def anyio_backend():
    return "asyncio"
