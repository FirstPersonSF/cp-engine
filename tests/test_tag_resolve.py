"""Tests for cp_engine.tag_resolve + the webhook /api/resolve-tags endpoint.

This is the single owner of the fathom tag→code heuristic (arch-phase-2);
fathom-meeting-sync's local ``projectTagToCode`` is now only a fallback.
The parse cases mirror fathom's historical test expectations so behavior
stays a superset of the old local parse.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cp_engine.tag_resolve import parse_tag, resolve_tags

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

from fastapi.testclient import TestClient  # noqa: E402

import main as webhook_main  # noqa: E402


# ── parse_tag (the pure heuristic) ──────────────────────────────────────

def test_parse_engagement_display_name():
    parsed = parse_tag("GGL 5136 go/safety website")
    assert parsed == {"prefix": "ggl", "number": 5136, "code": "ggl-5136"}


def test_parse_engagement_variants():
    assert parse_tag("TEL-5163 Brand")["code"] == "tel-5163"
    assert parse_tag("IBX5167 DDI Platform")["code"] == "ibx-5167"
    assert parse_tag("ggl-5136-go-safety-website")["code"] == "ggl-5136"


def test_parse_internal_workstream_code():
    """#301: internal workstreams carry a number and a digit-led company
    code (`1pi`); the old three-letter regex rejected them."""
    assert parse_tag("1pi-9005-mission-control") == {
        "prefix": "1pi", "number": 9005, "code": "1pi-9005",
    }
    assert parse_tag("1PI 9005 Mission Control")["code"] == "1pi-9005"


def test_parse_rejects_numberless_slugs_untagged_and_junk():
    """A bare slug is no longer a tag shape: every workstream has a number
    since the initiatives table retired (#301)."""
    assert parse_tag("mission-control") is None
    assert parse_tag("storyos") is None
    assert parse_tag("untagged") is None
    assert parse_tag("") is None
    assert parse_tag("   ") is None
    assert parse_tag(None) is None
    assert parse_tag("Some Random Meeting Title") is None


# ── resolve_tags (DB-backed) ────────────────────────────────────────────

def _fake_client(project_rows):
    client = MagicMock()

    def table(name):
        assert name == "projects", f"only `projects` is read since #301, got {name!r}"
        t = MagicMock()
        resp = MagicMock()
        resp.data = project_rows
        t.select.return_value.execute.return_value = resp
        return t

    client.table.side_effect = table
    return client


PROJECTS = [
    {"number": 5136, "companies": {"code": "GGL"}},
    {"number": 5153, "companies": {"code": "IBX"}},
    # An internal workstream: a `projects` row like any other (mig 192).
    {"number": 9005, "companies": {"code": "1PI"}, "is_internal": True},
]


def test_resolve_verified_engagement():
    (r,) = resolve_tags(_fake_client(PROJECTS), ["GGL 5136 go/safety"])
    assert r == {"tag": "GGL 5136 go/safety", "code": "ggl-5136",
                 "kind": "project", "matched": True}


def test_resolve_rebuilds_code_from_company_row_not_tag_prefix():
    """A mistyped prefix in the tag still resolves to the true code."""
    (r,) = resolve_tags(_fake_client(PROJECTS), ["GOO 5136 typo tag"])
    assert r["code"] == "ggl-5136"
    assert r["matched"] is True


def test_resolve_unknown_number_falls_back_to_parse():
    """Archived/deleted projects keep routing on the parsed code."""
    (r,) = resolve_tags(_fake_client(PROJECTS), ["HEX 5164 old thing"])
    assert r == {"tag": "HEX 5164 old thing", "code": "hex-5164",
                 "kind": "project", "matched": False}


def test_resolve_internal_workstream_by_number_and_bare_slug_is_null():
    """#301: `1pi-9005-mission-control` is DB-verified by number like any
    job (kind is always "project"); the old bare slug names nothing."""
    rows = resolve_tags(
        _fake_client(PROJECTS), ["1pi-9005-mission-control", "mission-control"],
    )
    assert rows[0] == {"tag": "1pi-9005-mission-control", "code": "1pi-9005",
                       "kind": "project", "matched": True}
    assert rows[1] == {"tag": "mission-control", "code": None,
                       "kind": None, "matched": False}


def test_resolve_untagged_and_junk_are_null():
    rows = resolve_tags(_fake_client(PROJECTS), ["untagged", "???"])
    assert all(r["code"] is None for r in rows)


def test_resolve_without_client_degrades_to_pure_parse():
    (r,) = resolve_tags(None, ["GGL 5136 go/safety"])
    assert r["code"] == "ggl-5136"
    assert r["matched"] is False


def test_resolve_db_error_degrades_to_pure_parse():
    client = MagicMock()
    client.table.side_effect = ConnectionError("db down")
    (r,) = resolve_tags(client, ["GGL 5136 go/safety"])
    assert r["code"] == "ggl-5136"
    assert r["matched"] is False


# ── the webhook endpoint ────────────────────────────────────────────────

@pytest.fixture
def client() -> TestClient:
    return TestClient(webhook_main.app)


def _signed(body: bytes, *, secret: bytes = b"test-secret") -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def test_endpoint_requires_signature(client, monkeypatch):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    body = json.dumps({"tags": ["GGL 5136 x"]}).encode()
    resp = client.post(
        "/api/resolve-tags", content=body,
        headers={"X-Webhook-Signature": "bad", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


def test_endpoint_resolves(client, monkeypatch):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    monkeypatch.setattr(
        webhook_main, "_create_supabase_client",
        lambda: _fake_client(PROJECTS),
    )
    body = json.dumps(
        {"tags": ["GGL 5136 go/safety", "1pi-9005-mission-control", "untagged"]}
    ).encode()
    resp = client.post(
        "/api/resolve-tags", content=body,
        headers={"X-Webhook-Signature": _signed(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    resolutions = resp.json()["resolutions"]
    assert [r["code"] for r in resolutions] == ["ggl-5136", "1pi-9005", None]
    assert [r["kind"] for r in resolutions] == ["project", "project", None]


def test_endpoint_validates_body(client, monkeypatch):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    body = json.dumps({"tags": "not-a-list"}).encode()
    resp = client.post(
        "/api/resolve-tags", content=body,
        headers={"X-Webhook-Signature": _signed(body), "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


# ── internal rows index like any other (#221 retired by #301) ────────

INTERNAL_PROJECTS = [
    {"number": 5136, "companies": {"code": "GGL"}, "is_internal": False},
    # Under the legacy schema this row duplicated the `storyos` initiative
    # and had no working dir, so #221 skipped it. mc-2 mig 192 merged the
    # initiative INTO this row (keeping the initiative uuid) — it is now
    # the one home for StoryOS, with a working dir at `cnc-9004-storyos/`.
    {"number": 9004, "companies": {"code": "CNC"}, "is_internal": True},
]


def test_internal_project_tag_resolves_confidently():
    """The #221 skip is retired: an internal row IS a workstream with a
    working dir, so its tag is DB-verified like a client job's."""
    (r,) = resolve_tags(_fake_client(INTERNAL_PROJECTS), ["CNC 9004 StoryOS"])
    assert r == {"tag": "CNC 9004 StoryOS", "code": "cnc-9004",
                 "kind": "project", "matched": True}


def test_internal_inclusion_does_not_affect_client_projects():
    (r,) = resolve_tags(_fake_client(INTERNAL_PROJECTS), ["GGL 5136 go/safety"])
    assert r == {"tag": "GGL 5136 go/safety", "code": "ggl-5136",
                 "kind": "project", "matched": True}


def test_the_old_initiative_slug_no_longer_resolves():
    """`storyos` was the initiative's slug code; the row now lives under
    `cnc-9004-storyos` and a numberless tag names nothing."""
    (r,) = resolve_tags(_fake_client(INTERNAL_PROJECTS), ["storyos"])
    assert r == {"tag": "storyos", "code": None, "kind": None, "matched": False}
