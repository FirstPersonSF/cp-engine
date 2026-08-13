"""A hung-up client must not page us (Sentry CP-ENGINE-WEBHOOK-3, 2026-08-13).

Every route reads `await request.body()` as its first statement, to verify the
HMAC before doing anything. When the sender has already gone away, Starlette
raises ClientDisconnect from that read — and it surfaced as an UNHANDLED
exception, i.e. a Sentry alert that looks like data loss.

It isn't. fathom-meeting-sync retries a delivery up to 3× with backoff
(`_deliverSigned`, because this webhook occasionally 500s on a cold start), but
a real ingest — LLM classification, git commit, push — outlasts the sender's
patience on a large payload. In the observed case a 230KB transcript's FIRST
attempt succeeded and committed at 10:36:17; the retry's socket was already
closed 18 seconds later, and the exception fired on line one of the handler,
before signature verification. The work was done; nothing was half-applied.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import main as webhook_main
from fastapi import Request
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect


@pytest.fixture
def client() -> TestClient:
    # raise_server_exceptions=False so an UNhandled exception surfaces as a
    # 500 response rather than propagating into the test — which is exactly
    # the distinction under test.
    return TestClient(webhook_main.app, raise_server_exceptions=False)


def test_client_disconnect_is_handled_not_raised(client, monkeypatch):
    """The handler is registered and converts the disconnect to a 499."""
    async def _disconnect(request: Request):
        # Fails the same way a real body read does when the sender is gone.
        raise ClientDisconnect()

    # Stand a route on the app that fails the same way a real body read does.
    webhook_main.app.add_api_route(
        "/__test_disconnect", _disconnect, methods=["POST"]
    )
    try:
        res = client.post("/__test_disconnect", content=b"{}")
    finally:
        webhook_main.app.router.routes = [
            r for r in webhook_main.app.router.routes
            if getattr(r, "path", None) != "/__test_disconnect"
        ]

    # 499 (nginx "client closed request"), NOT 500 — a 500 here would mean the
    # exception went unhandled and Sentry got paged.
    assert res.status_code == 499


def test_the_handler_is_registered_for_the_right_exception(client):
    """Registered against ClientDisconnect specifically, not a broad catch.

    A blanket `Exception` handler would swallow real ingest failures — the
    opposite of what this fix is for.
    """
    handlers = webhook_main.app.exception_handlers
    assert ClientDisconnect in handlers
    assert Exception not in handlers


def test_a_normal_request_is_unaffected(client):
    """The health probe still answers — the handler cannot swallow live traffic."""
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "healthy"
