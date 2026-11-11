"""Learning phase 4: Close distils a project (final ingest, drafted report,
lesson candidates, freelancer review requests). Drew, 2026-11-11: "Close
should run the report and lessons, and archive is really just closing the
tools."
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from cp_engine import closing as C


class _Q:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self.rows = [dict(r) for r in store.tables.get(name, [])]

    def select(self, _c):
        return self

    def eq(self, c, v):
        self.rows = [r for r in self.rows if r.get(c) == v]
        return self

    def in_(self, c, vals):
        self.rows = [r for r in self.rows if r.get(c) in vals]
        return self

    def insert(self, rows):
        self.store.inserts.append((self.name, rows))
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)


class _Store:
    def __init__(self, tables):
        self.tables, self.inserts = tables, []

    def table(self, name):
        return _Q(self, name)


def _store(reviews=()):
    return _Store({
        "sprint_allocations": [
            {"project_id": "p1", "entity_id": "geoff", "hours": 20},
            {"project_id": "p1", "entity_id": "geoff", "hours": 5},
            {"project_id": "p1", "entity_id": "drew", "hours": 30},
            {"project_id": "p1", "entity_id": "zero", "hours": 0},
            {"project_id": "p1", "entity_id": "vendorco", "hours": 4},
            {"project_id": "other", "entity_id": "zero", "hours": 9},
        ],
        "entities": [
            {"id": "geoff", "is_external": True, "kind": "freelancer"},
            {"id": "drew", "is_external": False, "kind": "staff"},
            {"id": "zero", "is_external": True, "kind": "freelancer"},
            {"id": "vendorco", "is_external": True, "kind": "vendor_contact"},
        ],
        "freelancer_reviews": list(reviews),
    })


def test_reviews_are_requested_for_freelancers_with_hours_only():
    s = _store()
    out = C.request_freelancer_reviews(s, "p1")
    assert out == {"requested": 1, "already": 0}
    assert s.inserts == [("freelancer_reviews",
                          [{"entity_id": "geoff", "project_id": "p1", "status": "requested"}])]


def test_review_requests_are_idempotent():
    s = _store(reviews=[{"entity_id": "geoff", "project_id": "p1"}])
    assert C.request_freelancer_reviews(s, "p1") == {"requested": 0, "already": 1}
    assert s.inserts == []


def test_report_versions_never_overwrite(tmp_path):
    p1 = C.write_report(tmp_path, "ibx-5192", "Summit", "Drew Fiero", "# Report", date(2026, 11, 11), "0.136.0")
    p2 = C.write_report(tmp_path, "ibx-5192", "Summit", "Drew Fiero", "# Report 2", date(2026, 11, 11), "0.136.0")
    assert p1.name.endswith("-v01.md") and p2.name.endswith("-v02.md")
    text = p1.read_text()
    assert "Filename: wrap-report-ibx-5192-2026-11-11-v01.md" in text
    assert "🤖 DRAFTED by cp at Close — Drew Fiero to confirm or rewrite." in text


def _patch_engine(monkeypatch, tmp_path, *, llm_out=None):
    from cp_engine import close_out, lessons, wrap_report

    work = tmp_path / "1p" / "infoblox" / "ibx-5192-summit"
    work.mkdir(parents=True)
    (work / "cp.md").write_text("<!-- cp-engine:start exec-summary -->\nDone.\n"
                                "<!-- cp-engine:end exec-summary -->\n")
    monkeypatch.setattr(close_out, "find_close_workdir", lambda root, code: (work, True))
    bundle = SimpleNamespace(name="Platform Sales Readiness Summit", account_manager="Drew Fiero")
    monkeypatch.setattr(wrap_report, "build_wrap",
                        lambda *a, **k: (bundle, {"code": "ibx-5192", "effort": {}}))
    monkeypatch.setattr(lessons, "known_lists", lambda c: (["event"], ["invoicing"]))
    queued = []
    monkeypatch.setattr(lessons, "record_candidates",
                        lambda c, items, **k: queued.append((items, k)) or {"queued": ["L1"], "skipped": []})
    return work, queued


def test_run_close_writes_the_draft_queues_lessons_and_requests_reviews(tmp_path, monkeypatch):
    work, queued = _patch_engine(monkeypatch, tmp_path)
    prompts = []
    llm = lambda p: prompts.append(p) or json.dumps({  # noqa: E731
        "report_markdown": "## Summary\n66% of meeting time fell in the last two weeks.",
        "lessons": [{"text": "Book decision meetings in week one.", "scope": "client"}]})
    out = C.run_close(_store(), code="ibx-5192", project_id="p1", tenant_root=tmp_path,
                      llm=llm, ingest=lambda: {"created": 3}, today=date(2026, 11, 11))
    assert out["steps"]["ingest"] == {"ok": True, "created": 3}
    assert out["steps"]["report"]["ok"] and out["report_path"].endswith("-v01.md")
    assert (tmp_path / out["report_path"]).read_text().count("66% of meeting time") == 1
    assert out["lesson_ids"] == ["L1"]
    items, kw = queued[0]
    assert kw["source_kind"] == "wrap_report" and kw["project_id"] == "p1"
    assert kw["source_ref"] == "close:p1:2026-11-11"  # a re-run queues nothing twice
    assert out["steps"]["freelancer_reviews"]["requested"] == 1
    assert "invoicing" in prompts[0] and "ALLOCATED" in prompts[0]


def test_a_failed_model_call_still_requests_reviews(tmp_path, monkeypatch):
    _patch_engine(monkeypatch, tmp_path)

    def boom(_p):
        raise RuntimeError("anthropic down")

    out = C.run_close(_store(), code="ibx-5192", project_id="p1", tenant_root=tmp_path,
                      llm=boom, ingest=None, today=date(2026, 11, 11))
    assert out["steps"]["report"]["ok"] is False and "anthropic down" in out["steps"]["report"]["error"]
    assert out["steps"]["lessons"]["ok"] is False
    assert out["steps"]["freelancer_reviews"]["ok"] is True
    assert out["report_path"] is None


# ── the webhook route ─────────────────────────────────────────────────

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))


def _signed(body: bytes) -> str:
    return hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()


def test_close_out_route_is_signed_and_runs_in_the_background(monkeypatch):
    from fastapi.testclient import TestClient

    import main as webhook_main
    import pipeline
    from routers import closing as route

    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "test-secret")
    spawned = []
    monkeypatch.setattr(pipeline, "_spawn_background", lambda coro: (spawned.append(coro), coro.close()))
    client = TestClient(webhook_main.app)
    body = json.dumps({"code": "ibx-5192", "mc_project_id": "p1"}).encode()
    ok = client.post("/api/close-out", content=body, headers={"x-webhook-signature": _signed(body)})
    assert ok.status_code == 202 and len(spawned) == 1
    bad = client.post("/api/close-out", content=body, headers={"x-webhook-signature": "nope"})
    assert bad.status_code in (401, 403)
    empty = json.dumps({"code": "ibx-5192"}).encode()
    assert client.post("/api/close-out", content=empty,
                       headers={"x-webhook-signature": _signed(empty)}).status_code == 400

    rows = []
    monkeypatch.setattr(route.run_ledger, "record_run", lambda **k: rows.append(k))
    monkeypatch.setattr(route, "_close_sync", lambda code, pid: {"steps": {"report": {"ok": False}}})
    asyncio.run(route._run_close("ibx-5192", "p1"))
    assert rows[0]["route"] == "/api/close-out" and rows[0]["status"] == "partial"


def test_publish_uploads_to_the_spine_folder_and_ingests_that_file(tmp_path, monkeypatch):
    from cp_engine import project_sources as ps

    report = tmp_path / "wrap-report-ibx-5192-2026-11-11-v01.md"
    report.write_text("# Report")
    monkeypatch.setattr(C, "project_dropbox_folder", lambda c, pid: "/1P Active Projects/IBX 5192")
    monkeypatch.setattr(ps, "dropbox_destination", lambda conn, folder, name: f"{folder}/{name}")
    pushed = []
    monkeypatch.setattr(ps, "push_to_dropbox", lambda conn, folder, f, dest_name, overwrite:
                        pushed.append(dest_name) or {"dropbox_path": f"{folder}/{dest_name}"})
    missing = SimpleNamespace(dbx=SimpleNamespace(files_get_metadata=lambda p: (_ for _ in ()).throw(
        RuntimeError("not_found"))))
    ingested = []
    out = C.publish_report(None, missing, project_id="p1", code="ibx-5192", report_file=report,
                           ingest=lambda f: ingested.append(f.name) or {"ingested": True})
    assert pushed == ["03 Assets/06 Spine/wrap-report-ibx-5192-2026-11-11-v01.md"]
    assert ingested == ["wrap-report-ibx-5192-2026-11-11-v01.md"]  # the local report, not a folder scan
    assert out["ingested"] is True

    # An initiative's folder has a bare Spine/ — the report goes there.
    has_spine = SimpleNamespace(dbx=SimpleNamespace(files_get_metadata=lambda p: SimpleNamespace(size=None)))
    pushed.clear()
    C.publish_report(None, has_spine, project_id="p1", code="cnc-5237", report_file=report)
    assert pushed == ["Spine/wrap-report-ibx-5192-2026-11-11-v01.md"]

    monkeypatch.setattr(C, "project_dropbox_folder", lambda c, pid: None)
    out = C.publish_report(None, missing, project_id="p1", code="x", report_file=report,
                           ingest=lambda f: {"ingested": True})
    assert out["dropbox_path"] is None and out["ingested"] is True  # still a source in MC-2
