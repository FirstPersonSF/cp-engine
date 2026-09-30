"""`capture_project_state` refuses placeholder values (improvements.md 2026-09-15).

WHAT BROKE. A probe of this verb wrote the literal string `probe` over
Mission Control's Status — a substantive paragraph about the Service Library —
and the next `cxp render` carried it into `master-cp.md`'s initiatives table
beside eleven real statuses. Found at wrap-up only because a file showed as
modified that nobody had edited. The write path worked perfectly; that was the
problem. There is no sandbox, so the value that proves the path works is the
value that destroys the field.

WHAT THESE PIN. Placeholders are refused, naming the field, BEFORE any read or
write (the body must never reach mc-2). Real short entries — Status is "one
phrase" by design — still pass, so the guard cannot become a length rule that
refuses "Shipped".

    python -m pytest prototypes/hosted-mcp/test_capture_placeholder_guard.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "http://example.invalid")
    mp.setenv("SUPABASE_ANON_KEY", "x")
    import server as mod

    yield mod
    mp.undo()


@pytest.fixture
def sent(server, monkeypatch):
    """Records what would have been POSTed to mc-2."""
    calls = []
    monkeypatch.setattr(server, "user_client", lambda: object())
    monkeypatch.setattr(server, "resolve_write_scope",
                        lambda c, code: {"id": "p", "kind": "project", "project_code": code})
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_project_codes_for_lint", lambda *a: None)
    monkeypatch.setattr(server, "_paths_index", lambda: ({}, "no tree in tests"))

    def fake_post(code, fields, entry):
        calls.append((code, fields, entry))
        return {"ok": True, "backend": {"changed": list(fields)}}

    monkeypatch.setattr(server, "call_mc2_capture_project_state", fake_post)
    return calls


@pytest.mark.parametrize("kwargs,field", [
    ({"status": "probe"}, "status"),                  # the 09-15 value, verbatim
    ({"status": "Test"}, "status"),
    ({"status": " TBD. "}, "status"),
    ({"status": "x"}, "status"),
    ({"objective": "todo"}, "objective"),
    ({"objective": "Lorem ipsum dolor sit amet"}, "objective"),
    ({"where_it_stands": ["Deck shipped 09-20", "placeholder"]}, "where_it_stands"),
    ({"blockers": ["tbd"]}, "blockers"),
    ({"updates_append": "test"}, "updates_append"),
])
def test_a_placeholder_is_refused_naming_the_field_and_nothing_is_sent(server, sent, kwargs, field):
    out = server.capture_project_state("mission-control", **kwargs)
    assert out["ok"] is False and out["field"] == field
    assert f"`{field}" in out["error"] and "placeholder" in out["error"]
    assert not sent


def test_a_real_status_beside_a_placeholder_bullet_is_still_refused(server, sent):
    """One placeholder anywhere refuses the whole call — a partial write would
    leave the summary half-updated with the caller believing it failed."""
    out = server.capture_project_state("mission-control", status="On hold",
                                       next_up=["x"])
    assert out["field"] == "next_up" and "next_up[0]" in out["error"]
    assert not sent


@pytest.mark.parametrize("status", [
    "On hold", "Shipped", "Paused", "Testing", "Live", "Done", "Blocked on legal",
    "SOW signed; kickoff 10-02",
])
def test_real_short_statuses_pass(server, sent, status):
    out = server.capture_project_state("mission-control", status=status)
    assert out.get("ok") is True, out
    assert sent[-1][1] == {"Status": status}


def test_none_is_an_honest_blocker_and_empty_clears(server, sent):
    assert server.capture_project_state("mission-control", blockers=["None"]).get("ok")
    assert server.capture_project_state("mission-control", blockers=[]).get("ok")
    assert [c[1] for c in sent] == [{"Blockers": ["None"]}, {"Blockers": []}]
