"""`cxp health` — the daily health line (architecture plan step 3).

The property that matters most: a check whose SOURCE breaks renders ⚠️ with
the reason. It never disappears and never reads ✅ — a health line that goes
quiet when its inputs fail is the silent-failure class it exists to catch.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cp_engine import daily_health as dh
from cp_engine.clock import tenant_timezone

NOW = datetime(2026, 9, 30, 6, 30, tzinfo=tenant_timezone())


class _Q:
    def __init__(self, rows=None, exc=None):
        self.rows, self.exc = rows or [], exc

    def __getattr__(self, name):  # select/gte/eq/in_/order/limit chain
        return lambda *a, **k: self

    def execute(self):
        if self.exc:
            raise self.exc
        return type("R", (), {"data": self.rows})()


class _Client:
    def __init__(self, tables: dict):
        self.tables = tables

    def table(self, name):
        v = self.tables.get(name, [])
        return _Q(exc=v) if isinstance(v, Exception) else _Q(rows=v)


def _iso(dt):
    return dt.astimezone(UTC).isoformat()


def _gh(runs_by_workflow: dict):
    def get(path):
        for wf, run in runs_by_workflow.items():
            if f"/workflows/{wf}/" in path:
                if isinstance(run, Exception):
                    raise run
                return {"workflow_runs": [run] if run else []}
        raise AssertionError(path)
    return get


def test_sync_ok_and_failed_and_unreadable():
    ok = dh.check_sync(_gh({"sync.yml": {"conclusion": "success",
                                         "updated_at": _iso(NOW - timedelta(hours=2))}}),
                       "FirstPersonSF/cp", NOW)
    assert ok.ok and ok.text.startswith("04:30 ok")
    bad = dh.check_sync(_gh({"sync.yml": {"conclusion": "failure",
                                          "updated_at": _iso(NOW)}}), "o/r", NOW)
    assert not bad.ok and "failure" in bad.text
    gone = dh.check_sync(_gh({"sync.yml": RuntimeError("401 Bad credentials")}), "o/r", NOW)
    assert not gone.ok and "unreadable" in gone.text and "401" in gone.text


def test_a_stale_successful_sync_is_not_ok():
    old = dh.check_sync(_gh({"sync.yml": {"conclusion": "success",
                                          "updated_at": _iso(NOW - timedelta(hours=40))}}),
                        "o/r", NOW)
    assert not old.ok and "40h old" in old.text


def test_ingest_counts_partial_from_errors_and_warnings_and_names_the_last_error():
    client = _Client({"auto_ingest_runs": [
        {"status": "failed", "errors": ["ggl-5168: sprint file missing"], "created_at": "x"},
        {"status": "success", "errors": None, "warnings": ["a: transcript persist failed"]},
        {"status": "success", "errors": ["b: plan execution failed"]},  # the #194 shape
        {"status": "success", "errors": None},
        {"status": "skipped_no_op", "errors": None},
    ]})
    c = dh.check_ingest(client, NOW)
    assert not c.ok
    assert c.detail == {"ok": 1, "partial": 2, "failed": 1, "noop": 1}
    assert "sprint file missing" in c.text


def test_ingest_reads_the_pre_migration_warnings_fold():
    client = _Client({"auto_ingest_runs": [
        {"status": "success", "errors": None,
         "plan_summary": {"a": {"record-ask": 1}, "_warnings": ["w"]}},
    ]})
    assert dh.check_ingest(client, NOW).detail["partial"] == 1


def test_ingest_unreadable_is_a_warning_not_zero():
    c = dh.check_ingest(_Client({"auto_ingest_runs": RuntimeError("JWT expired")}), NOW)
    assert not c.ok and "JWT expired" in c.text
    assert not dh.check_ingest(None, NOW).ok


def test_webhook_missing_ledger_is_named():
    client = _Client({
        "webhook_runs": Exception("relation \"public.webhook_runs\" does not exist (42P01)"),
        "spine_promote_runs": [],
        "asset_ingest_runs": [],
    })
    c = dh.check_webhook(client, NOW)
    assert not c.ok and "ledger missing" in c.text


def test_webhook_counts_failures_across_tables():
    client = _Client({
        "webhook_runs": [{"route": "/api/sessions/capture", "status": "failed",
                          "error": "push failed"},
                         {"route": "/slack-action#background", "status": "partial",
                          "error": "410"}],
        "spine_promote_runs": [{"status": "failed", "error": "distill timeout"}],
        "asset_ingest_runs": [],
    })
    c = dh.check_webhook(client, NOW)
    assert c.detail["failed"] == 2 and c.detail["partial"] == 1
    assert "/api/sessions/capture: push failed" in c.text


def test_unbound_flags_only_floating():
    rows = [
        {"project_id": "p", "est_item_id": "a", "important": False, "serves": []},
        {"project_id": "p", "est_item_id": "a", "important": False, "serves": []},  # 2 versions
        {"project_id": "p", "est_item_id": "b", "important": True, "serves": []},
        {"project_id": "p", "est_item_id": "c", "important": True, "serves": ["x"]},
    ]
    c = dh.check_unbound(_Client({"spine_substance": rows}))
    assert c.detail == {"unbound": 3, "floating": 1} and not c.ok
    assert dh.check_unbound(_Client({"spine_substance": rows[:1]})).ok


def test_stale_summaries_counts_what_sync_rendered(tmp_path: Path):
    (tmp_path / "master-cp.md").write_text(
        "| x | ⚠️ _summary 41d stale_ |\n| y | ⚠️ _summary 33d stale_ |\n"
        "| z | ⚠️ _partial refresh_ |\n", encoding="utf-8")
    c = dh.check_stale_summaries(tmp_path)
    assert c.text == "2 (max 41d) · 1 partial" and not c.ok
    assert not dh.check_stale_summaries(tmp_path / "nope").ok


def test_hosted_down_and_deps_broken():
    assert not dh.check_hosted(None, "URLError: timed out").ok
    c = dh.check_hosted({"status": "healthy", "server_version": "hosted-cp/0.127.0",
                         "tool_count": 64, "read_endpoint": {"tool_count": 24},
                         "deps_ok": False, "deps": "spine_steps: ImportError"})
    assert not c.ok and "deps" in c.text
    good = dh.check_hosted({"status": "healthy", "server_version": "hosted-cp/0.127.0",
                            "tool_count": 64, "read_endpoint": {"tool_count": 24}, "deps_ok": True})
    assert good.ok and good.text == "0.127.0 · 64 tools · 24 read"


def test_copies_disagree_on_version_and_week():
    h = {"server_version": "hosted-cp/0.126.3", "sprint_week": "2026-W40"}
    c = dh.check_copies(h, "0.127.0", "2026-W41")
    assert not c.ok and "0.126.3≠0.127.0" in c.text and "W40≠2026-W41" in c.text
    agree = dh.check_copies({"server_version": "hosted-cp/0.127.0"}, "0.127.0", "2026-W41")
    assert agree.ok and "hosted n/a" in agree.text  # absence is said, not implied


def test_gather_never_raises_and_renders_every_line(tmp_path: Path):
    def boom(_):
        raise RuntimeError("no network")

    rep = dh.gather(tenant_root=tmp_path, client=None, github=boom,
                    hosted_fetch=boom, now=NOW)
    text = rep.render()
    assert len(rep.checks) == 8 and not rep.ok
    labels = [c.label for c in rep.checks]
    assert labels == ["Sync", "Ingest 24h", "Webhook 24h", "Hosted", "CI main",
                      "Spine unbound", "Stale summaries", "Copies"]
    assert all(len(lbl.split()) <= 3 for lbl in labels)  # Tony's rule 1
    assert text.count("⚠️") == 8 and "✅" not in text


def test_post_refuses_without_a_partners_channel():
    from cp_engine import slack as slack_mod

    rep = dh.HealthReport(when=NOW, checks=[dh.Check("Sync", True, "ok")])
    with pytest.raises(slack_mod.SlackError, match="partners channel unset"):
        dh.post(rep, config=object(), client=_Client({"app_config": []}))


def test_cli_exits_nonzero_when_a_check_needs_a_look(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from cp_engine.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dh, "gather", lambda **kw: dh.HealthReport(
        when=NOW, checks=[dh.Check("Sync", False, "failure")]))
    monkeypatch.setattr("cp_engine.mc2_db.get_client", lambda *a, **k: None)
    res = CliRunner().invoke(main, ["health"])
    assert res.exit_code == 1 and "⚠️ Sync · failure" in res.output


@pytest.mark.parametrize("when,expected", [
    # PDT (UTC-7): 12:23Z = 05:23 local → the 12:23 slot posts, 13:23 does not.
    (datetime(2026, 9, 30, 12, 40, tzinfo=UTC), {"23 12 * * *": True, "23 13 * * *": False}),
    # PST (UTC-8): 13:23Z = 05:23 local → the 13:23 slot posts.
    (datetime(2026, 12, 2, 13, 30, tzinfo=UTC), {"23 12 * * *": False, "23 13 * * *": True}),
    # Runner lag of 4h must not change which slot posts.
    (datetime(2026, 9, 30, 17, 0, tzinfo=UTC), {"23 12 * * *": True, "23 13 * * *": False}),
])
def test_exactly_one_morning_slot_per_day(when, expected):
    assert {c: dh.is_morning_slot(c, when) for c in expected} == expected
