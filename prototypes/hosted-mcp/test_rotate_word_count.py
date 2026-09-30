"""`rotate_word_count` — the hosted verb that performs word-count rotation (#280).

The verb holds no write key: it hands the call to mc-2 under the caller's own
JWT, and mc-2 names the commit from the verified token. What these pin is
that hop — the URL, the token, a body that CANNOT carry an author — and that
the webhook's answers (landed, no-op, refused) reach the caller as what they
are. The move itself is tested in `tests/test_cp_rotation.py` and the route
in `tests/test_webhook_word_count_rotate.py`.

    python -m pytest prototypes/hosted-mcp/test_rotate_word_count.py -v
"""
from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

CODE = "1pi-9005-mission-control"


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "http://example.invalid")
    mp.setenv("SUPABASE_ANON_KEY", "x")
    import server as mod

    yield mod
    mp.undo()


@pytest.fixture
def audited(server, monkeypatch):
    rows = []
    monkeypatch.setattr(server, "user_client", lambda: object())
    monkeypatch.setattr(
        server, "resolve_write_scope",
        lambda c, code: {"id": "p", "kind": "project", "project_code": code},
    )
    monkeypatch.setattr(server, "audit", lambda *a, **k: rows.append(a))
    monkeypatch.setattr(
        server, "_level_for",
        lambda code: {"code": code, "label": "initiative", "parent": None, "indexed": True},
    )
    return rows


class _Resp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body) if not isinstance(body, str) else body

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


def _fake_httpx(server, monkeypatch, status=200, body=None):
    box = {}

    def _post(url, headers=None, json=None, timeout=None):
        box.update(url=url, headers=headers, payload=json)
        return _Resp(status, body)

    monkeypatch.setattr(server.httpx, "post", _post)
    monkeypatch.setattr(server, "caller_jwt", lambda: "jwt-of-the-caller")
    return box


LANDED = {
    "ok": True, "changed": True, "commit": "deadbeef",
    "moved": [{"date": "2026-08-20", "headline": "old"}, {"date": "2026-08-18", "headline": "older"}],
    "archive_path": f"firstpersonsf/{CODE}/cp-archive-2026-09.md",
    "cp_md_path": f"firstpersonsf/{CODE}/cp.md",
    "words_before": 3659, "words_after": 3533,
    "over_audit_threshold": True, "over_rotation_threshold": True,
}


def test_the_rotation_is_carried_to_mc2_under_the_callers_token(server, audited, monkeypatch):
    box = _fake_httpx(server, monkeypatch, 200, LANDED)
    out = server.rotate_word_count(CODE)
    assert box["url"].endswith("/api/word-count/rotate")
    assert box["headers"] == {"Authorization": "Bearer jwt-of-the-caller"}
    # THE CONTROL: the body names the project and nothing else — no author.
    assert box["payload"] == {"project_code": CODE}
    assert out["ok"] is True and out["backend"]["commit"] == "deadbeef"
    assert out["level"]["code"] == CODE
    # Audited with the moved count, so `wrap_status` could see it ran.
    assert audited[-1][1] == "rotate_word_count" and audited[-1][3] == 2


def test_the_no_op_passes_through_as_success(server, audited, monkeypatch):
    body = {**LANDED, "changed": False, "commit": None, "moved": []}
    _fake_httpx(server, monkeypatch, 200, body)
    out = server.rotate_word_count(CODE)
    assert out["ok"] is True and out["backend"]["changed"] is False
    assert out["backend"]["commit"] is None


def test_a_loss_refusal_is_reported_as_a_refusal(server, audited, monkeypatch):
    """409 = the no-loss check refused, nothing written. It must not read as
    an outage (which invites a blind retry) or as success."""
    _fake_httpx(server, monkeypatch, 409, {"detail": "Rotation refused, nothing pushed: lost 1 line(s)"})
    out = server.rotate_word_count(CODE)
    assert out["ok"] is False and out["refused"] is True
    assert "nothing was written" in out["reason"] and "lost 1 line" in out["reason"]
    assert audited[-1][3] == 0


def test_an_unknown_code_sends_nothing(server, audited, monkeypatch):
    box = _fake_httpx(server, monkeypatch, 200, LANDED)
    monkeypatch.setattr(server, "resolve_write_scope", lambda c, code: None)
    out = server.rotate_word_count("zzz-0000")
    assert "error" in out and not box


def test_unconfigured_mc2_degrades_visibly(server, audited, monkeypatch):
    monkeypatch.setattr(server, "MC2_API_BASE", "")
    out = server.rotate_word_count(CODE)
    assert out["ok"] is False and out["degraded"] is True


def test_the_verb_takes_only_a_project_code(server):
    """No `user`, no `author`, no knobs on what moves: the rule is the
    engine's, and the name is the token's."""
    params = list(inspect.signature(server.rotate_word_count).parameters)
    assert params == ["project_code"]
    tools = {t.name: t for t in server.mcp_server._tool_manager.list_tools()}
    assert "rotate_word_count" in tools
    props = tools["rotate_word_count"].parameters.get("properties", {})
    assert set(props) == {"project_code"}
