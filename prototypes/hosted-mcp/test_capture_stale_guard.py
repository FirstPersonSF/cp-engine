"""`capture_project_state` refuses a partial refresh of an already-stale summary (#280).

WHAT BROKE. ggl-5136's Exec Summary took a hosted `Status`-only write that
advanced its `· updated` stamp while `Next up` still listed three July
deadlines and a collaborator who had left. Every staleness check reads that
stamp, so the refresh converted "obviously old" into "looks current" — worse
than no refresh. The verb's docstring and the skill both SAID to pass every
field; nothing enforced it.

WHAT THESE PIN. When the stamp is already ≥14 days old (the planning bundle's
STALE threshold), omitting any of the five replaced fields refuses the call
BEFORE anything reaches mc-2, naming the omitted fields. `still_current` is the
explicit "I read it, it holds" escape. A summary stamped inside 14 days is
never refused, and an unreadable tree steps the guard aside rather than turn
a clone outage into a write outage.

    python -m pytest prototypes/hosted-mcp/test_capture_stale_guard.py -v
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

TODAY = date(2026, 9, 30)
ALL_FIVE = dict(
    status="In build", objective="Ship the site",
    where_it_stands=["Two migrations delivered"], next_up=["Sitemap v17"],
    blockers=["None"],
)


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
    calls = []
    monkeypatch.setattr(server, "user_client", lambda: object())
    monkeypatch.setattr(server, "resolve_write_scope",
                        lambda c, code: {"id": "p", "kind": "project", "project_code": code})
    monkeypatch.setattr(server, "audit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_project_codes_for_lint", lambda *a: None)
    monkeypatch.setattr(server, "tenant_today", lambda: TODAY)

    def fake_post(code, fields, entry):
        calls.append((code, fields, entry))
        return {"ok": True, "backend": {"changed": list(fields)}}

    monkeypatch.setattr(server, "call_mc2_capture_project_state", fake_post)
    return calls


@pytest.fixture
def tree(server, monkeypatch, tmp_path):
    """A tenant tree whose one project carries a summary stamped `stamp`."""
    proj = tmp_path / "1p" / "google" / "ggl-5136-go-safety-website"
    proj.mkdir(parents=True)
    monkeypatch.setattr(server, "tree_available", lambda: (True, ""))
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(server, "tree_root", lambda: tmp_path)
    monkeypatch.setattr(server, "find_project_dir", lambda root, code: proj)

    def stamp(iso: str | None) -> None:
        heading = f"## Exec Summary  ·  updated {iso}" if iso else "## Exec Summary"
        (proj / "cp.md").write_text(
            "# ggl-5136\n\n<!-- cp-engine:start exec-summary -->\n"
            f"{heading}\n\n**Status:** old\n\n**Next up:**\n- Fri 7/17\n"
            "<!-- cp-engine:end exec-summary -->\n",
            encoding="utf-8",
        )

    return stamp


def test_status_only_on_a_stale_summary_is_refused_and_nothing_is_sent(server, sent, tree):
    """THE CONTROL — the ggl-5136 write, verbatim in shape. Against the
    unguarded verb this POSTs `{"Status": ...}` and the stamp moves."""
    tree("2026-07-15")
    out = server.capture_project_state("ggl-5136", status="In the regional build")
    assert out["ok"] is False, out
    assert not sent
    assert out["stale_fields"] == ["objective", "where_it_stands", "next_up", "blockers"]
    assert out["stamp"] == "2026-07-15" and out["stamp_age_days"] == 77
    for field in out["stale_fields"]:
        assert field in out["error"]
    assert "still_current" in out["error"]


def test_updates_append_alone_on_a_stale_summary_is_refused(server, sent, tree):
    """An Updates-only call also re-stamps the region, so it gets no pass."""
    tree("2026-09-01")
    out = server.capture_project_state("ggl-5136", updates_append="Captured sites")
    assert out["ok"] is False and len(out["stale_fields"]) == 5
    assert not sent


def test_the_threshold_is_fourteen_days_exactly(server, sent, tree):
    tree("2026-09-17")  # 13 days: live
    assert server.capture_project_state("ggl-5136", status="In build").get("ok") is True
    tree("2026-09-16")  # 14 days: stale
    assert server.capture_project_state("ggl-5136", status="In build")["ok"] is False
    assert len(sent) == 1


def test_passing_every_field_on_a_stale_summary_writes(server, sent, tree):
    tree("2026-07-15")
    out = server.capture_project_state("ggl-5136", **ALL_FIVE)
    assert out.get("ok") is True and len(sent[-1][1]) == 5


def test_still_current_confirms_the_omitted_fields(server, sent, tree):
    tree("2026-07-15")
    out = server.capture_project_state(
        "ggl-5136", status="In build", next_up=["Sitemap v17"],
        still_current=["objective", "where_it_stands", "blockers"],
    )
    assert out.get("ok") is True, out
    # Confirmed fields are NOT resent — confirming is not rewriting.
    assert set(sent[-1][1]) == {"Status", "Next up"}


def test_a_partial_confirmation_still_names_what_is_left(server, sent, tree):
    tree("2026-07-15")
    out = server.capture_project_state(
        "ggl-5136", status="In build", still_current=["objective"],
    )
    assert out["stale_fields"] == ["where_it_stands", "next_up", "blockers"]
    assert not sent


def test_an_unknown_still_current_name_is_refused(server, sent, tree):
    """`Next up` (the label) instead of `next_up` must not silently confirm nothing."""
    tree("2026-09-29")
    out = server.capture_project_state("ggl-5136", status="x-ray", still_current=["Next up"])
    assert out["ok"] is False and "Next up" in out["error"]
    assert not sent


@pytest.mark.parametrize("setup", ["no_tree", "unstamped", "not_member"])
def test_the_guard_steps_aside_when_the_stamp_is_unknowable(server, sent, tree, monkeypatch, setup):
    tree("2026-07-15")
    if setup == "no_tree":
        monkeypatch.setattr(server, "tree_available", lambda: (False, "no TENANT_REPO"))
    elif setup == "unstamped":
        tree(None)
    else:
        monkeypatch.setattr(server, "caller_is_team_member", lambda: (False, "denied"))
    assert server.capture_project_state("ggl-5136", status="In build").get("ok") is True


def test_a_tree_error_fails_open(server, sent, tree, monkeypatch):
    tree("2026-07-15")

    def boom():
        raise RuntimeError("clone failed")

    monkeypatch.setattr(server, "tree_root", boom)
    assert server.capture_project_state("ggl-5136", status="In build").get("ok") is True
