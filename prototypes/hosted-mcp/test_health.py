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
    # The structural check, and the ONLY one this container can give.
    assert isinstance(body["tool_count"], int) and body["tool_count"] > 0
    # The PREFIX, not the literal: asserting "hosted-cp-spike/" is what held
    # the stale spike string in place while it drifted nine months.
    assert body["server_version"].startswith("hosted-cp/")


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


@pytest.mark.anyio
async def test_health_does_not_need_cp_engine(server, monkeypatch):
    """The container has no cp-engine. Neither may this endpoint.

    The Dockerfile COPYs `server.py` and `observability.py` and installs six
    packages; cp-engine is not one of them, by design (the module docstring
    says this prototype does not import it). An endpoint that reaches for it
    500s in production while passing locally -- which is exactly what happened
    on 2026-09-15.

    Blocking the import is the point: a test that merely calls health() in a
    venv where cp_engine is importable cannot see the bug.
    """
    import sys

    class _Block:
        def find_module(self, name, path=None):
            return self if name == "cp_engine" or name.startswith("cp_engine.") else None

        def load_module(self, name):
            raise ImportError(f"No module named {name!r}")

    blocker = _Block()
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])
    monkeypatch.delitem(sys.modules, "cp_engine", raising=False)

    import json

    body = json.loads((await server.health(None)).body)
    assert body["status"] == "healthy"
    assert "cp_engine_version" not in body


@pytest.mark.anyio
async def test_whoami_reports_the_build_on_both_paths(server, monkeypatch):
    """The build half of `whoami`, which is why it was added.

    `/health` already answers "what is running here?" — but over HTTP, and a
    hosted-only session (no `cxp`, no shell) cannot curl it. That session
    reaches this server and nothing else, so without these fields its only
    way to ask whether a just-shipped fix is live was to ask a human to run
    curl. Both paths are asserted: an unauthenticated caller still gets to
    know which server turned them away.
    """
    unauth = server.whoami()
    assert unauth["authenticated"] is False
    assert unauth["server_version"].startswith("hosted-cp/")
    assert len(unauth["build"]) == 12

    class _Access:
        subject = "user-123"
        expires_at = 9999999999
        claims = {"email": "drew@firstperson.is", "role": "authenticated"}

    monkeypatch.setattr(server, "get_access_token", lambda: _Access())
    auth = server.whoami()
    assert auth["authenticated"] is True
    assert auth["email"] == "drew@firstperson.is"
    assert auth["server_version"].startswith("hosted-cp/")
    assert len(auth["build"]) == 12


@pytest.mark.anyio
async def test_whoami_and_health_cannot_disagree(server):
    """One source of truth. Two surfaces reporting different builds is worse
    than one surface, because it invites trusting the stale one."""
    import json

    health = json.loads((await server.health(None)).body)
    ident = server.whoami()
    assert ident["server_version"] == health["server_version"]
    assert ident["build"] == health["build"]


def test_server_version_tracks_the_engine_release():
    """`SERVER_VERSION` must match the engine version, and stay matched.

    It did not, for nine months. The constant read `hosted-cp-spike/0.0.6` at
    engine 0.120.1 — harmless while it was an internal label, actively
    misleading the day `whoami` started returning it, because a field named
    `server_version` next to an accurate `build` hash is where a reader looks
    first. A fresh session was asked "what is the version number" and answered
    "0.0.6": confident, precise, and nine months wrong.

    `scripts/release.py` now rewrites it on every release. This asserts the
    result, so the two cannot drift apart again without a red test — a release
    script that silently stops matching is the same failure one level up.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    engine = re.search(
        r'^__version__ = "([^"]+)"',
        (root / "src" / "cp_engine" / "__init__.py").read_text(),
        re.MULTILINE,
    ).group(1)
    served = re.search(
        r'SERVER_VERSION = "([^"]*)"',
        (Path(__file__).resolve().parent / "server.py").read_text(),
    ).group(1)

    assert served == f"hosted-cp/{engine}", (
        f"SERVER_VERSION is {served!r} but the engine is at {engine!r}. "
        "scripts/release.py bumps this — if it drifted, that wiring broke."
    )
