"""#415, webhook side: an unrouted email keeps its attachments' NAMES.

Before: `_record_unrouted` stored text only, so an attachment on an email
sent to the bare cp@ (or a bad +code) left no record anywhere — the sweep
could never say "the brief you are waiting for came in on this email", and
routing the email later filed nothing and said nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

from routers import email as email_router  # noqa: E402

PAYLOAD = {
    "to": "cp@thinkermakers.com",
    "from": '"Haney, Tara" <tara.haney@sap.com>',
    "subject": "Recap + brief",
    "message_id": "<brief-1@sap.com>",
    "text": "Brief attached.",
    "received_at": "2026-10-09T19:02:00Z",
    "attachments": [{"filename": "SAP Concur_2027 Video Ads_Creative Brief V2.docx",
                     "mime_type": "application/vnd.openxmlformats-officedocument."
                                  "wordprocessingml.document",
                     "size": 300123, "content_base64": "UEsDBA=="}],
    "attachments_skipped": [{"filename": "image001.png", "mime_type": "image/png",
                             "size": 812, "reason": "inline signature image"}],
}


class _Upserts:
    def __init__(self, fail_with_attachments=False):
        self.rows: list[dict] = []
        self.fail_with_attachments = fail_with_attachments

    def table(self, name):
        assert name == "unrouted_emails"
        return self

    def upsert(self, row, on_conflict=None):
        self._pending = dict(row)
        return self

    def execute(self):
        if self.fail_with_attachments and "attachments" in self._pending:
            raise RuntimeError("Could not find the 'attachments' column of 'unrouted_emails'")
        self.rows.append(self._pending)
        return SimpleNamespace(data=[self._pending])


def test_unrouted_row_keeps_attachment_names_not_bytes(monkeypatch):
    fake = _Upserts()
    monkeypatch.setattr(email_router.mc2_db, "get_client", lambda required=False: fake)
    assert email_router._record_unrouted(PAYLOAD, reason="no_code", attempted_code=None)
    (row,) = fake.rows
    assert row["attachments"] == [
        {"filename": "SAP Concur_2027 Video Ads_Creative Brief V2.docx",
         "mime_type": PAYLOAD["attachments"][0]["mime_type"], "size": 300123},
        {"filename": "image001.png", "mime_type": "image/png", "size": 812,
         "skipped": "inline signature image"},
    ]
    assert "UEsDBA" not in repr(row)  # never the content


def test_before_migration_207_the_email_is_still_recorded(monkeypatch):
    fake = _Upserts(fail_with_attachments=True)
    monkeypatch.setattr(email_router.mc2_db, "get_client", lambda required=False: fake)
    assert email_router._record_unrouted(PAYLOAD, reason="no_code", attempted_code=None)
    (row,) = fake.rows
    assert "attachments" not in row and row["subject"] == "Recap + brief"


def test_an_email_without_attachments_writes_no_attachments_key(monkeypatch):
    fake = _Upserts()
    monkeypatch.setattr(email_router.mc2_db, "get_client", lambda required=False: fake)
    plain = {k: v for k, v in PAYLOAD.items() if not k.startswith("attachments")}
    assert email_router._record_unrouted(plain, reason="no_code", attempted_code=None)
    assert "attachments" not in fake.rows[0]


# ── /api/route-email says which files it cannot file ─────────────────


class _RouteMC2:
    def __init__(self, row, attachments):
        self.row, self.attachments, self.updated, self._cols = row, attachments, None, ""

    def table(self, name):
        # The run ledger (webhook_runs) shares the client; only this table matters.
        self._table = name
        return self

    def insert(self, *_a, **_k):
        return self

    upsert = insert

    def select(self, cols, *_a, **_k):
        self._cols = cols
        return self

    def eq(self, *_a, **_k):
        return self

    def single(self):
        return self

    def update(self, patch):
        if self._table == "unrouted_emails":
            self.updated = patch
        self._cols = "update"
        return self

    def execute(self):
        if self._cols == "attachments":
            return SimpleNamespace(data=[{"attachments": self.attachments}])
        return SimpleNamespace(data=self.row)


def test_route_email_names_the_attachments_it_cannot_file(monkeypatch, tmp_path):
    import hashlib
    import hmac
    import json
    import time

    from fastapi.testclient import TestClient

    import main as webhook_main

    monkeypatch.setenv("WEBHOOK_HMAC_SECRET", "s3")
    row = {"id": "brief-1@sap.com", "thread_id": None, "from_addr": PAYLOAD["from"],
           "to_addr": "cp@thinkermakers.com", "subject": "Recap + brief",
           "raw_text": "Brief attached.", "received_at": "2026-10-09T19:02:00Z",
           "status": "unrouted"}
    fake = _RouteMC2(row, [{"filename": "Creative Brief V2.docx", "size": 300123}])
    monkeypatch.setattr(email_router.mc2_db, "get_client", lambda required=False: fake)

    class _Clone:
        def __enter__(self):
            return tmp_path

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(email_router.git_ops, "_cloned_tenant", lambda: _Clone())
    project_dir = tmp_path / "1p" / "sap" / "sap-5198-2027-ad-videos"
    project_dir.mkdir(parents=True)
    monkeypatch.setattr(email_router, "load_config", lambda root: SimpleNamespace(root=tmp_path))
    monkeypatch.setattr(email_router, "find_spine_dir", lambda root, code, mc2_id=None: project_dir)
    monkeypatch.setattr(email_router.git_ops, "_commit_with_message_and_push", lambda r, m: "abc")
    monkeypatch.setattr(email_router.pipeline, "_ingest_one_project",
                        lambda **kw: {"plan_summary": None, "files_written": [], "errors": []})

    raw = json.dumps({"message_id": "brief-1@sap.com", "code": "sap-5198"}).encode()
    ts = str(int(time.time()))
    sig = hmac.new(b"s3", f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    resp = TestClient(webhook_main.app).post(
        "/api/route-email", content=raw,
        headers={"x-webhook-signature": sig, "x-webhook-timestamp": ts})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["attachments_not_filed"]["names"] == ["Creative Brief V2.docx"]
    assert any("cp+sap-5198@" in w for w in data["warnings"])
    assert fake.updated == {"status": "routed", "routed_to_code": "sap-5198"}
