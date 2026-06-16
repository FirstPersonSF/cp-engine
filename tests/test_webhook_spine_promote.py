"""Tests for POST /api/spine/promote (spine estimate-binding, Task 4.a).

The mc-2 web UI's "Frame & promote" click lands a new live substance version
directly in the cp tenant git repo. mc-2 has the DB but no checkout of the
tenant filesystem, so the markdown write + git push happen HERE (the webhook
clones the tenant per-request). This endpoint is the server-side equivalent of
the `cp spine-frame` CLI: signature-verify → clone → load card → resolve item
→ promote_card (directed re-distill, write markdown) → mirror to spine_substance
→ commit + push → return the HEAD sha + version label + tenant-relative path.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path
from contextlib import contextmanager

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

from fastapi.testclient import TestClient

import main as webhook_main

from cp_engine.estimate import Estimate, EstimateItem, EstimatePhase
from cp_engine.spine_inbox import InboxCard


def _signed(body: bytes, *, secret: bytes = b"test-secret") -> str:
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


@pytest.fixture
def client() -> TestClient:
    return TestClient(webhook_main.app)


def _post(client: TestClient, payload: dict):
    body = json.dumps(payload).encode()
    return client.post(
        "/api/spine/promote",
        content=body,
        headers={"x-webhook-signature": _signed(body)},
    )


def _card(**kw) -> InboxCard:
    base = dict(
        id="ibx-5153/inbox/mtg-1",
        project_id="u-123",
        project_code="ibx-5153",
        source_ref="mtg-1",
        raw_distillation="raw faithful body",
        guessed_est_item_id="d1",
        guessed_type="deliverable",
        status="proposed",
        framing=None,
    )
    base.update(kw)
    return InboxCard(**base)


def _estimate() -> Estimate:
    item = EstimateItem(
        id="d1", phase_id="ph0", kind="deliverable", name="Messaging system",
        short_description=None, position=0, library_item_id=None,
    )
    return Estimate(
        id="e1", mc_project_id="u-123", name="E1",
        phases=(EstimatePhase(id="ph0", name="Phase 0", overview=None,
                              position=0, items=(item,)),),
    )


@contextmanager
def _fake_clone(tmp_path: Path):
    yield tmp_path


def _wire_happy(monkeypatch, tmp_path: Path, *, card=None, estimate=None,
                promote_path=None, mirror=None, sha="deadbeef",
                version_label="v3", recorder=None):
    """Wire every collaborator the endpoint touches; return the recorder dict."""
    rec = recorder if recorder is not None else {}
    card = card if card is not None else _card()
    estimate = estimate if estimate is not None else _estimate()

    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")

    monkeypatch.setattr(webhook_main, "_cloned_tenant",
                        lambda: _fake_clone(tmp_path))

    sb = object()
    monkeypatch.setattr(webhook_main, "_create_supabase_client", lambda: sb)
    rec["client"] = sb

    cfg = type("Cfg", (), {"root": tmp_path})()
    monkeypatch.setattr(webhook_main, "_load_tenant_config", lambda root: cfg)

    monkeypatch.setattr("cp_engine.spine_inbox.load_card",
                        lambda c, cid: card)
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate",
                        lambda c, pid: estimate)
    monkeypatch.setattr("cp_engine.spine.find_spine_dir",
                        lambda root, code: tmp_path / "1p" / "infoblox" / code)

    if promote_path is None:
        promote_path = (tmp_path / "1p" / "infoblox" / "ibx-5153"
                        / "spine" / "phase-0" / "messaging-system.md")

    def fake_promote(card, **kw):
        rec["promote_kw"] = kw
        rec["promote_card"] = card
        promote_path.parent.mkdir(parents=True, exist_ok=True)
        promote_path.write_text("# stub substance\n")
        return promote_path
    monkeypatch.setattr("cp_engine.spine_inbox.promote_card", fake_promote)

    def fake_mirror(c, **kw):
        rec["mirror_kw"] = kw
        if mirror is not None:
            return mirror(c, **kw)
        return 1
    monkeypatch.setattr("cp_engine.spine_substance_sync.sync_spine_substance",
                        fake_mirror)

    fake_live = type("LV", (), {"label": version_label})()
    fake_item = type("WI", (), {"live_version": lambda self: fake_live})()
    monkeypatch.setattr("cp_engine.substance.parse_substance",
                        lambda p: fake_item)

    def fake_commit(*, tenant_root, project_code, version_label, rel_path):
        rec["commit_kw"] = dict(
            tenant_root=tenant_root, project_code=project_code,
            version_label=version_label, rel_path=rel_path,
        )
        return sha
    monkeypatch.setattr(webhook_main, "_commit_and_push_promote", fake_commit)

    return rec


# ---------------------------------------------------------------- happy path


def test_promote_happy_path(monkeypatch, client, tmp_path):
    rec = _wire_happy(monkeypatch, tmp_path)
    resp = _post(client, {
        "card_id": "ibx-5153/inbox/mtg-1",
        "framing": "the directing brief",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["commit_sha"] == "deadbeef"
    assert data["version_label"] == "v3"
    assert data["rel_path"] == (
        "1p/infoblox/ibx-5153/spine/phase-0/messaging-system.md"
    )
    assert data["mirrored"] is True

    # promote_card got the resolved item name/phase/kind + framing.
    kw = rec["promote_kw"]
    assert kw["framing"] == "the directing brief"
    assert kw["est_item_id"] == "d1"
    assert kw["kind"] == "deliverable"
    assert kw["name"] == "Messaging system"
    assert kw["phase"] == "Phase 0"
    assert kw["model"] == webhook_main.DEFAULT_MODEL
    assert kw["sources"] == ["mtg-1"]  # falls back to card.source_ref
    assert kw["client"] is rec["client"]

    # mirror + commit were both called.
    assert "mirror_kw" in rec
    assert rec["commit_kw"]["project_code"] == "ibx-5153"
    assert rec["commit_kw"]["version_label"] == "v3"


# ---------------------------------------------------------------- 404


def test_promote_404_when_card_missing(monkeypatch, client, tmp_path):
    _wire_happy(monkeypatch, tmp_path)
    monkeypatch.setattr("cp_engine.spine_inbox.load_card", lambda c, cid: None)
    resp = _post(client, {"card_id": "ibx-5153/inbox/nope", "framing": "x"})
    assert resp.status_code == 404
    assert "no inbox card" in resp.text


# ---------------------------------------------------------------- 400s


def test_promote_400_when_framing_empty(monkeypatch, client, tmp_path):
    _wire_happy(monkeypatch, tmp_path)
    resp = _post(client, {"card_id": "ibx-5153/inbox/mtg-1", "framing": "   "})
    assert resp.status_code == 400
    assert "framing" in resp.text.lower()


def test_promote_400_when_no_est_item_id(monkeypatch, client, tmp_path):
    # Card carries no guess and none is passed → nothing to bind to.
    _wire_happy(monkeypatch, tmp_path, card=_card(guessed_est_item_id=None))
    resp = _post(client, {
        "card_id": "ibx-5153/inbox/mtg-1", "framing": "brief",
    })
    assert resp.status_code == 400
    assert "estimate item" in resp.text.lower()


# ---------------------------------------------------------------- 401


def test_promote_401_on_bad_signature(monkeypatch, client, tmp_path):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    body = json.dumps({"card_id": "x", "framing": "y"}).encode()
    resp = client.post(
        "/api/spine/promote",
        content=body,
        headers={"x-webhook-signature": "deadbeef"},
    )
    assert resp.status_code == 401


def test_promote_401_when_signature_missing(monkeypatch, client, tmp_path):
    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    body = json.dumps({"card_id": "x", "framing": "y"}).encode()
    resp = client.post("/api/spine/promote", content=body)
    assert resp.status_code == 401


# ---------------------------------------------------------------- mirror fail


def test_promote_mirror_failure_still_200(monkeypatch, client, tmp_path):
    def boom(c, **kw):
        raise RuntimeError("supabase down")
    _wire_happy(monkeypatch, tmp_path, mirror=boom)
    resp = _post(client, {
        "card_id": "ibx-5153/inbox/mtg-1", "framing": "brief",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["commit_sha"] == "deadbeef"  # push already succeeded
    assert data["mirrored"] is False


# ---------------------------------------------------------------- defaults


def test_promote_defaults_resolution(monkeypatch, client, tmp_path):
    """est_item_id falls back to card guess; sources fall back to
    [card.source_ref]; model defaults to DEFAULT_MODEL; kind defaults to the
    estimate item's kind."""
    rec = _wire_happy(monkeypatch, tmp_path)
    resp = _post(client, {
        "card_id": "ibx-5153/inbox/mtg-1", "framing": "brief",
    })
    assert resp.status_code == 200, resp.text
    kw = rec["promote_kw"]
    assert kw["est_item_id"] == "d1"            # card.guessed_est_item_id
    assert kw["sources"] == ["mtg-1"]           # [card.source_ref]
    assert kw["model"] == "claude-opus-4-7"     # DEFAULT_MODEL
    assert kw["kind"] == "deliverable"          # item.kind


def test_promote_explicit_overrides(monkeypatch, client, tmp_path):
    """Passed est_item_id / kind / sources / model override the defaults."""
    rec = _wire_happy(monkeypatch, tmp_path)
    resp = _post(client, {
        "card_id": "ibx-5153/inbox/mtg-1",
        "framing": "brief",
        "est_item_id": "d1",
        "kind": "activity",
        "sources": ["doc-a", "doc-b"],
        "model": "claude-opus-4-8",
    })
    assert resp.status_code == 200, resp.text
    kw = rec["promote_kw"]
    assert kw["est_item_id"] == "d1"
    assert kw["kind"] == "activity"
    assert kw["sources"] == ["doc-a", "doc-b"]
    assert kw["model"] == "claude-opus-4-8"


def test_promote_estimate_unreachable_degrades(monkeypatch, client, tmp_path):
    """If the estimator schema is unreachable, the promote still proceeds
    unbound-by-name (name/phase None, kind falls back to 'deliverable')."""
    rec = _wire_happy(monkeypatch, tmp_path)

    def boom(c, pid):
        raise RuntimeError("estimator down")
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate", boom)

    resp = _post(client, {
        "card_id": "ibx-5153/inbox/mtg-1", "framing": "brief",
    })
    assert resp.status_code == 200, resp.text
    kw = rec["promote_kw"]
    assert kw["name"] is None
    assert kw["phase"] is None
    assert kw["kind"] == "deliverable"  # resolved_kind fallback
