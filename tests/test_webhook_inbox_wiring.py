"""Tests for the spine-inbox proposed-card wiring (Phase 3, Task 3.2).

Every single-project auto-ingest writes a PROPOSED `spine_inbox` card — a
raw-faithful distillation + a best-guess estimate item — for a human to
frame+promote. The write is best-effort and MUST NOT break auto-ingest:
no supabase client, no source_ref, an unresolvable project, or any failure
degrades to a status string rather than a raised exception. It writes ONLY to
spine_inbox (never spine_substance).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# webhook/ is a sibling of src/; not on the import path by default.
_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import main as webhook_main  # the module is `main.py`


class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._op = None
        self._filters = []

    def upsert(self, rows, on_conflict=None):
        self._op = ("upsert", rows)
        return self

    def select(self, cols):
        assert "*" not in cols
        self._op = ("select", cols)
        return self

    def update(self, patch):
        self._op = ("update", dict(patch))
        return self

    def eq(self, col, val):
        self._filters.append(lambda r, c=col, v=val: r.get(c) == v)
        return self

    def neq(self, col, val):
        self._filters.append(lambda r, c=col, v=val: r.get(c) != v)
        return self

    def in_(self, col, vals):
        self._filters.append(lambda r, c=col, v=tuple(vals): r.get(c) in v)
        return self

    def execute(self):
        op, payload = self._op
        rows = self.store.setdefault(self.name, [])
        matching = [r for r in rows if all(f(r) for f in self._filters)]
        if op == "upsert":
            for r in payload:
                rows.append(dict(r))
            return type("R", (), {"data": payload})()
        if op == "update":
            for r in matching:
                r.update(payload)
            return type("R", (), {"data": matching})()
        return type("R", (), {"data": matching})()


class _FakeClient:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _FakeTable(self.store, name)


def test_inbox_card_skipped_when_no_source_ref(tmp_path: Path) -> None:
    tp = tmp_path / "t.md"
    tp.write_text("transcript")
    status = webhook_main._append_inbox_card(
        code="ibx-5153", transcript_path=tp, meeting_id=None,
    )
    assert status == "skipped"


def test_inbox_card_skipped_when_no_supabase(tmp_path: Path, monkeypatch) -> None:
    tp = tmp_path / "t.md"
    tp.write_text("transcript")
    monkeypatch.setattr(webhook_main, "_create_supabase_client", lambda: None)
    status = webhook_main._append_inbox_card(
        code="ibx-5153", transcript_path=tp, meeting_id="mtg-1",
    )
    assert status == "skipped"


def test_inbox_card_proposed_happy_path(tmp_path: Path, monkeypatch) -> None:
    from cp_engine import asset_ingest
    from cp_engine.estimate import Estimate, EstimateItem, EstimatePhase
    import cp_engine.plan_from_transcript as pft

    tp = tmp_path / "t.md"
    tp.write_text("We discussed the messaging system.")

    client = _FakeClient()
    monkeypatch.setattr(webhook_main, "_create_supabase_client", lambda: client)

    folders = type("F", (), {"project_id": "u-123"})()
    monkeypatch.setattr(
        asset_ingest, "resolve_project_folders", lambda c, code: folders
    )

    item = EstimateItem(id="d1", phase_id="ph0", kind="deliverable",
                        name="Messaging system", short_description=None,
                        position=0, library_item_id=None)
    est = Estimate(id="e1", mc_project_id="u-123", name="E1",
                   phases=(EstimatePhase(id="ph0", name="Phase 0", overview=None,
                                         position=0, items=(item,)),))
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate", lambda c, pid: est)

    # Stub the Anthropic call — no network.
    def fake_claude(prompt, *, model, api_key=None):
        assert model == webhook_main.DEFAULT_MODEL
        return json.dumps({"distillation": "raw faithful body",
                           "matched_item_name": "Messaging system"})
    monkeypatch.setattr(pft, "_call_claude", fake_claude)

    status = webhook_main._append_inbox_card(
        code="ibx-5153", transcript_path=tp, meeting_id="mtg-42",
    )
    assert status == "proposed"
    assert "spine_inbox" in client.store
    assert "spine_substance" not in client.store  # never writes substance
    row = client.store["spine_inbox"][0]
    assert row["id"] == "ibx-5153/inbox/mtg-42"
    assert row["raw_distillation"] == "raw faithful body"
    assert row["guessed_est_item_id"] == "d1"
    assert row["status"] == "proposed"


def test_inbox_card_never_raises_on_unresolvable_project(
    tmp_path: Path, monkeypatch
) -> None:
    from cp_engine import asset_ingest

    tp = tmp_path / "t.md"
    tp.write_text("transcript")
    monkeypatch.setattr(webhook_main, "_create_supabase_client", lambda: _FakeClient())
    monkeypatch.setattr(
        asset_ingest, "resolve_project_folders", lambda c, code: None
    )
    status = webhook_main._append_inbox_card(
        code="nope-9999", transcript_path=tp, meeting_id="mtg-1",
    )
    assert status == "skipped"


def test_inbox_card_skipped_when_disabled_by_env(
    tmp_path: Path, monkeypatch
) -> None:
    """The SPINE_INBOX_ENABLED kill-switch returns 'skipped' early and never
    touches supabase or the distiller — a single env var disables the live
    path without a redeploy."""
    import cp_engine.plan_from_transcript as pft

    tp = tmp_path / "t.md"
    tp.write_text("transcript")

    # Wire these so that, IF the gate failed to short-circuit, the test would
    # observe a call. They must NOT be reached.
    def must_not_call_client():
        raise AssertionError("kill-switch did not short-circuit: client created")

    def must_not_distill(prompt, *, model, api_key=None):
        raise AssertionError("kill-switch did not short-circuit: distiller called")

    monkeypatch.setattr(webhook_main, "_create_supabase_client", must_not_call_client)
    monkeypatch.setattr(pft, "_call_claude", must_not_distill)

    monkeypatch.setenv("SPINE_INBOX_ENABLED", "0")
    status = webhook_main._append_inbox_card(
        code="ibx-5153", transcript_path=tp, meeting_id="mtg-1",
    )
    assert status == "skipped"


def test_inbox_card_enabled_by_default(tmp_path: Path, monkeypatch) -> None:
    """With SPINE_INBOX_ENABLED unset, the gate is OPEN (prod behavior
    unchanged) — it proceeds past the gate to the supabase check."""
    tp = tmp_path / "t.md"
    tp.write_text("transcript")
    monkeypatch.delenv("SPINE_INBOX_ENABLED", raising=False)
    # No supabase → "skipped" comes from the EXISTING gate, not the kill-switch.
    # The point is that we got past the kill-switch to reach that check.
    called = {"client": False}

    def client_factory():
        called["client"] = True
        return None

    monkeypatch.setattr(webhook_main, "_create_supabase_client", client_factory)
    status = webhook_main._append_inbox_card(
        code="ibx-5153", transcript_path=tp, meeting_id="mtg-1",
    )
    assert status == "skipped"
    assert called["client"] is True  # proceeded past the kill-switch


def test_inbox_card_never_raises_on_distiller_error(
    tmp_path: Path, monkeypatch
) -> None:
    from cp_engine import asset_ingest
    import cp_engine.plan_from_transcript as pft

    tp = tmp_path / "t.md"
    tp.write_text("transcript")
    monkeypatch.setattr(webhook_main, "_create_supabase_client", lambda: _FakeClient())
    monkeypatch.setattr(
        asset_ingest, "resolve_project_folders",
        lambda c, code: type("F", (), {"project_id": "u1"})(),
    )
    monkeypatch.setattr("cp_engine.estimate.fetch_estimate", lambda c, pid: None)

    def boom(prompt, *, model, api_key=None):
        raise RuntimeError("anthropic down")
    monkeypatch.setattr(pft, "_call_claude", boom)

    status = webhook_main._append_inbox_card(
        code="ibx-5153", transcript_path=tp, meeting_id="mtg-1",
    )
    assert status == "error"  # caught, logged, ingest continues
