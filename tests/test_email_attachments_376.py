"""Email attachments into a project (#376).

The cp-email-worker carries attachments as base64; the webhook uploads each
to the project's Dropbox under ``03 Assets/01 Client Assets/Email/<date>
<subject>/`` and queues their ingest. Dropbox, MC-2 and the ingest run are
faked here; every fixture is synthetic (cp-engine is a public repo).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

import pytest

_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import email_attachments as ea  # noqa: E402
import main as webhook_main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from routers import email as email_router  # noqa: E402

from cp_engine import asset_ingest  # noqa: E402
from cp_engine.asset_ingest import ProjectFolders  # noqa: E402

_SECRET = "test-inbound-secret"
_PID = "00000000-0000-4000-8000-000000000376"
_ROOT = "/Clients/Acme/5100 Widget Launch"

PDF = b"%PDF-1.4 synthetic deck " * 50
PNG = b"\x89PNG synthetic screenshot " * 80


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _payload(**over) -> dict:
    body = {
        "to": "cp+acm-5100@thinkermakers.com",
        "from": "Pat Example <pat@example.com>",
        "subject": "Fwd: RE: Deck r02 — feedback!",
        "message_id": "<synthetic-376@example.com>",
        "date": "Thu, 29 Oct 2026 17:30:00 -0700",
        "received_at": "2026-10-30T01:00:00Z",
        "text": "Notes on the deck attached.",
        "attachments": [
            {"filename": "deck.pdf", "mime_type": "application/pdf",
             "size": len(PDF), "inline": False, "content_base64": _b64(PDF)},
            {"filename": "image001.png", "mime_type": "image/png",
             "size": len(PNG), "inline": True, "content_base64": _b64(PNG)},
        ],
        "attachments_skipped": [
            {"filename": "logo.png", "mime_type": "image/png", "size": 4000,
             "reason": "inline image under 40960 bytes (signature logo or icon)"},
        ],
    }
    body.update(over)
    return body


# ── naming ────────────────────────────────────────────────────────────


def test_subject_slug_drops_reply_prefixes():
    assert ea.subject_slug("Fwd: RE: Deck r02 — feedback!") == "deck-r02-feedback"
    assert ea.subject_slug("") == "no-subject"
    assert ea.subject_slug(None) == "no-subject"
    assert len(ea.subject_slug("x" * 300)) == 60


def test_email_date_prefers_the_date_header():
    # 17:30 -0700 on the 29th is 00:30 UTC on the 30th.
    assert ea.email_date({"date": "Thu, 29 Oct 2026 17:30:00 -0700",
                          "received_at": "2026-11-02T00:00:00Z"}) == "2026-10-30"
    assert ea.email_date({"date": "garbage", "received_at": "2026-11-02T00:00:00Z"}) == "2026-11-02"


def test_email_folder():
    assert ea.email_folder(_payload()) == (
        "03 Assets/01 Client Assets/Email/2026-10-30 deck-r02-feedback"
    )


def test_safe_filename():
    assert ea.safe_filename("../../etc/passwd") == "passwd"
    assert ea.safe_filename('a:b*c?.pdf') == "a-b-c-.pdf"
    assert ea.safe_filename("") == "attachment"
    assert ea.safe_filename(".hidden") == "hidden"


def test_numbered():
    assert ea._numbered("deck.pdf", 1) == "deck.pdf"
    assert ea._numbered("deck.pdf", 2) == "deck (2).pdf"
    assert ea._numbered("README", 3) == "README (3)"


def test_dropbox_content_hash_blocks():
    block = 4 * 1024 * 1024
    data = b"a" * (block + 5)
    expected = hashlib.sha256(
        hashlib.sha256(data[:block]).digest() + hashlib.sha256(data[block:]).digest()
    ).hexdigest()
    assert ea.dropbox_content_hash(data) == expected


def test_decode_lists_worker_and_webhook_skips(monkeypatch):
    monkeypatch.setattr(ea, "MAX_FILE_BYTES", 1500)
    payload = _payload()
    payload["attachments"].append(
        {"filename": "broken.pdf", "content_base64": "not base64!!"})
    kept, skipped = ea.decode_attachments(payload)
    assert [a.filename for a in kept] == ["deck.pdf"]  # PDF = 1200 bytes
    statuses = {s["filename"]: s["status"] for s in skipped}
    assert statuses["logo.png"].startswith("skipped: inline image under")
    assert statuses["image001.png"] == "skipped: over the 1500-byte per-file cap"
    assert statuses["broken.pdf"].startswith("skipped: content is not valid base64")


# ── fakes ─────────────────────────────────────────────────────────────


class _NotFound(Exception):
    """Shaped like dropbox.exceptions.ApiError for a missing path."""

    class _Err:
        def is_path(self):
            return True

        def get_path(self):
            return self

        def is_not_found(self):
            return True

    def __init__(self):
        super().__init__("not_found")
        self.error = self._Err()


class _Meta:
    def __init__(self, path, data):
        self.path_display = path
        self.size = len(data)
        self.content_hash = ea.dropbox_content_hash(data)


class _FakeDropbox:
    """Files by path; `upload_file` is the connector method push_to_dropbox calls."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.uploads: list[str] = []
        self.fail_on: set[str] = set()
        self.dbx = self

    def files_get_metadata(self, path):
        if path not in self.files:
            raise _NotFound()
        return _Meta(path, self.files[path])

    def upload_file(self, local, dest, overwrite=False):
        if dest.rsplit("/", 1)[-1] in self.fail_on:
            raise RuntimeError("synthetic upload failure")
        assert overwrite is False
        data = Path(local).read_bytes()
        self.files[dest] = data
        self.uploads.append(dest)
        return _Meta(dest, data)


def _folders(allow=()) -> ProjectFolders:
    return ProjectFolders(
        project_id=_PID, company_id="c1", company_kind="client",
        google_drive_folder_id=None, mc_dropbox_folder_id=_ROOT,
        enable_google_drive=False, enable_dropbox=True,
        asset_ingest_folders=tuple(allow),
    )


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Patch the MC-2 client, the folders lookup, Dropbox and the queue."""
    state = {"dropbox": _FakeDropbox(), "folders": _folders(), "queued": [],
             "queue_error": None}

    project_dir = tmp_path / "acme" / "acm-5100-widget-launch"
    project_dir.mkdir(parents=True)
    (project_dir / "cp.md").write_text(f"---\nMC-id: {_PID}\n---\n\n# cp\n")
    state["project_dir"] = project_dir

    monkeypatch.setattr(ea, "_supabase_client", lambda: object())
    monkeypatch.setattr(ea, "_dropbox_connector", lambda: state["dropbox"])
    monkeypatch.setattr(asset_ingest, "resolve_project_folders_by_id",
                        lambda client, pid: state["folders"] if pid == _PID else None)

    def _queue(client, code, project_id, paths):
        if state["queue_error"]:
            raise state["queue_error"]
        state["queued"].append({"code": code, "project_id": project_id, "paths": list(paths)})
        return {"queued": True, "run_id": f"run-{len(state['queued'])}"}

    monkeypatch.setattr(ea, "_queue_ingest", _queue)
    return state


def _file(env, payload=None):
    return ea.file_attachments(code="acm-5100", project_dir=env["project_dir"],
                               payload=payload or _payload())


_FOLDER = f"{_ROOT}/03 Assets/01 Client Assets/Email/2026-10-30 deck-r02-feedback"


# ── file_attachments ──────────────────────────────────────────────────


def test_no_attachments_is_a_no_op(env):
    p = _payload()
    del p["attachments"], p["attachments_skipped"]
    assert _file(env, p) is None
    assert env["dropbox"].uploads == []


def test_uploads_and_queues_one_run(env):
    out = _file(env)
    assert out["folder"] == _FOLDER
    assert env["dropbox"].uploads == [f"{_FOLDER}/deck.pdf", f"{_FOLDER}/image001.png"]
    assert env["dropbox"].files[f"{_FOLDER}/deck.pdf"] == PDF
    # ONE run for every file, by Dropbox path (the picker's selection key).
    assert env["queued"] == [{"code": "acm-5100", "project_id": _PID,
                              "paths": [f"{_FOLDER}/deck.pdf", f"{_FOLDER}/image001.png"]}]
    assert out["run_id"] == "run-1"
    rows = {a["filename"]: a for a in out["attachments"]}
    assert rows["deck.pdf"]["status"] == "queued" and rows["deck.pdf"]["queued"] is True
    assert rows["deck.pdf"]["dropbox_path"] == f"{_FOLDER}/deck.pdf"
    # The Worker's skip is reported, not dropped.
    assert rows["logo.png"]["status"].startswith("skipped: inline image")
    assert rows["logo.png"]["queued"] is False


def test_redelivery_does_not_upload_twice(env):
    _file(env)
    first_uploads = list(env["dropbox"].uploads)
    # Re-delivered later: received_at moves, the Date header does not.
    out = _file(env, _payload(received_at="2026-11-05T09:00:00Z"))
    assert env["dropbox"].uploads == first_uploads  # nothing new uploaded
    rows = {a["filename"]: a for a in out["attachments"]}
    assert rows["deck.pdf"]["status"] == "already filed, queued"
    assert rows["deck.pdf"]["dropbox_path"] == f"{_FOLDER}/deck.pdf"


def test_different_file_same_name_gets_the_next_name(env):
    env["dropbox"].files[f"{_FOLDER}/deck.pdf"] = b"another email's deck"
    out = _file(env)
    rows = {a["filename"]: a for a in out["attachments"]}
    assert rows["deck.pdf"]["dropbox_path"] == f"{_FOLDER}/deck (2).pdf"
    assert rows["deck.pdf"]["filed_as"] == "deck (2).pdf"
    assert env["dropbox"].files[f"{_FOLDER}/deck.pdf"] == b"another email's deck"


def test_outside_the_ingest_folders_uploads_but_does_not_queue(env):
    env["folders"] = _folders(allow=("Client Decks",))
    out = _file(env)
    assert len(env["dropbox"].uploads) == 2  # uploaded anyway
    assert env["queued"] == []
    rows = {a["filename"]: a for a in out["attachments"]}
    assert rows["deck.pdf"]["status"].startswith("uploaded, not queued: ")
    assert "not in the project's ingest folders (Client Decks)" in rows["deck.pdf"]["status"]
    assert rows["deck.pdf"]["queued"] is False


def test_matching_ingest_folder_queues(env):
    env["folders"] = _folders(allow=("Client Assets",))
    out = _file(env)
    assert out["run_id"] == "run-1"


def test_dropbox_off_uploads_but_does_not_queue(env):
    f = _folders()
    f.enable_dropbox = False
    env["folders"] = f
    out = _file(env)
    rows = {a["filename"]: a for a in out["attachments"]}
    assert rows["deck.pdf"]["status"] == (
        "uploaded, not queued: the project's Dropbox source is not enabled in MC-2"
    )


def test_one_failed_upload_does_not_stop_the_rest(env):
    env["dropbox"].fail_on = {"deck.pdf"}
    out = _file(env)
    rows = {a["filename"]: a for a in out["attachments"]}
    assert rows["deck.pdf"]["status"].startswith("not uploaded: upload failed")
    assert rows["image001.png"]["status"] == "queued"
    assert env["queued"][0]["paths"] == [f"{_FOLDER}/image001.png"]


def test_no_dropbox_folder_reports_every_file(env):
    f = _folders()
    f.mc_dropbox_folder_id = None
    env["folders"] = f
    out = _file(env)
    assert env["dropbox"].uploads == []
    assert "no Dropbox folder in MC-2" in out["error"]
    statuses = [a["status"] for a in out["attachments"] if a["filename"] != "logo.png"]
    assert all(s.startswith("not uploaded: acm-5100 has no Dropbox folder") for s in statuses)


def test_queue_failure_is_reported_per_file(env):
    env["queue_error"] = RuntimeError("supabase down")
    out = _file(env)
    rows = {a["filename"]: a for a in out["attachments"]}
    assert rows["deck.pdf"]["status"] == (
        "uploaded, not queued: the ingest run could not be queued "
        "(RuntimeError: supabase down)"
    )
    assert out["run_id"] is None


def test_project_id_falls_back_to_code_lookup(env, monkeypatch):
    (env["project_dir"] / "cp.md").write_text("# no frontmatter\n")
    monkeypatch.setattr(asset_ingest, "resolve_project_folders",
                        lambda client, code: _folders() if code == "acm-5100" else None)
    out = _file(env)
    assert out["run_id"] == "run-1"


def test_queue_ingest_inserts_a_run_and_spawns_the_scoped_ingest(monkeypatch):
    """The real `_queue_ingest`: what /api/assets/ingest does, in-process."""
    import pipeline
    from routers import assets as assets_router

    inserted, spawned = [], []

    class _Tbl:
        def insert(self, row):
            inserted.append(row)
            return self

        def execute(self):
            return None

    monkeypatch.setattr(assets_router, "_asset_runs_table", lambda client: _Tbl())
    monkeypatch.setattr(assets_router, "_run_asset_ingest",
                        lambda *a: ("ingest", *a))
    monkeypatch.setattr(pipeline, "_spawn_background", spawned.append)

    out = ea._queue_ingest(object(), "acm-5100", _PID, ["/a/b.pdf"])
    assert out["queued"] is True
    assert inserted[0]["id"] == out["run_id"]
    assert inserted[0]["project_id"] == _PID and inserted[0]["status"] == "running"
    assert spawned == [("ingest", out["run_id"], "acm-5100", _PID, None, ["/a/b.pdf"])]


def test_render_section():
    lines = ea.render_section({
        "folder": _FOLDER, "run_id": "r1",
        "attachments": [{"filename": "deck.pdf", "size": 2048, "status": "queued"},
                        {"filename": "x.png", "size": 10, "status": "skipped: tiny",
                         "filed_as": None}],
    })
    text = "\n".join(lines)
    assert text.startswith("## Attachments")
    assert "- deck.pdf (2 KB) — queued" in text
    assert "- x.png (10 B) — skipped: tiny" in text
    assert ea.render_section(None) == []


# ── the route ─────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("INBOUND_EMAIL_SECRET", _SECRET)
    return TestClient(webhook_main.app)


def _signed(body: dict) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    ts = str(int(time.time()))
    sig = hmac.new(_SECRET.encode(), f"{ts}.".encode() + raw, hashlib.sha256).hexdigest()
    return raw, {"x-webhook-signature": sig, "x-webhook-timestamp": ts}


class _FakeClone:
    def __init__(self, root):
        self._root = root

    def __enter__(self):
        return self._root

    def __exit__(self, *exc):
        return False


@pytest.fixture
def wired(monkeypatch, tmp_path, env):
    """The clone, config, resolver, commit and distill patched (as in
    test_webhook_inbound_email), with the attachment fakes from `env`."""
    project_dir = env["project_dir"]
    monkeypatch.setattr(email_router.git_ops, "_cloned_tenant", lambda: _FakeClone(tmp_path))

    class _Cfg:
        root = tmp_path

    monkeypatch.setattr(email_router, "load_config", lambda root: _Cfg())
    monkeypatch.setattr(email_router, "find_spine_dir",
                        lambda root, code, mc2_id=None: project_dir)
    monkeypatch.setattr(email_router.git_ops, "_commit_with_message_and_push",
                        lambda root, msg: "deadbee")
    monkeypatch.setattr(email_router.pipeline, "_ingest_one_project",
                        lambda **kw: {"plan_summary": {}, "files_written": [], "errors": []})
    return env


def test_route_files_attachments_and_notes_them(client, wired):
    raw, headers = _signed(_payload())
    resp = client.post("/api/inbound-email", content=raw, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ingested"
    att = data["attachments"]
    assert att["run_id"] == "run-1"
    assert {a["filename"]: a["status"] for a in att["attachments"]}["deck.pdf"] == "queued"
    # The park note records what was filed — and carries no base64.
    note = (wired["project_dir"].parent.parent / data["parked_path"]).read_text()
    assert "## Attachments" in note
    assert "- deck.pdf (1 KB) — queued" in note
    assert _b64(PDF)[:40] not in note


def test_route_survives_a_crashing_attachment_step(client, wired, monkeypatch):
    def _boom(**kw):
        raise RuntimeError("synthetic crash")

    monkeypatch.setattr(email_router.email_attachments, "file_attachments", _boom)
    raw, headers = _signed(_payload())
    resp = client.post("/api/inbound-email", content=raw, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ingested"  # the email still parked + distilled
    assert "synthetic crash" in data["attachments"]["error"]
    names = [a["filename"] for a in data["attachments"]["attachments"]]
    assert names == ["deck.pdf", "image001.png"]


def test_route_without_attachments_is_unchanged(client, wired):
    p = _payload()
    del p["attachments"], p["attachments_skipped"]
    raw, headers = _signed(p)
    data = client.post("/api/inbound-email", content=raw, headers=headers).json()
    assert data["attachments"] is None
    assert wired["dropbox"].uploads == []


def test_unfiled_shapes_name_their_attachments(client, monkeypatch):
    monkeypatch.setattr(email_router, "_record_unrouted", lambda *a, **k: True)
    for to, why in [
        ("cp@thinkermakers.com", "no project code"),
        ("cp+account+acme@thinkermakers.com", "account addresses are not filed yet"),
    ]:
        raw, headers = _signed(_payload(to=to))
        data = client.post("/api/inbound-email", content=raw, headers=headers).json()
        assert data["attachments_not_filed"]["names"] == ["deck.pdf", "image001.png"]
        assert why in data["attachments_not_filed"]["reason"]
