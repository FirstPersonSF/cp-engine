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
    assert not gone.ok and gone.text == "unreadable"
    assert "401" in gone.detail["error"] and "401" not in gone.render()


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
    assert c.detail == {"ok": 1, "partial": 2, "failed": 1, "noop": 1,
                        "error": "ggl-5168: sprint file missing"}
    assert "sprint file missing" not in c.text  # the line carries counts only
    assert c.text == "1 ok · 1 no-op · 2 partial · 1 failed"


def test_ingest_reads_the_pre_migration_warnings_fold():
    client = _Client({"auto_ingest_runs": [
        {"status": "success", "errors": None,
         "plan_summary": {"a": {"record-ask": 1}, "_warnings": ["w"]}},
    ]})
    assert dh.check_ingest(client, NOW).detail["partial"] == 1


def test_ingest_unreadable_is_a_warning_not_zero():
    c = dh.check_ingest(_Client({"auto_ingest_runs": RuntimeError("JWT expired")}), NOW)
    assert not c.ok and c.text == "unreadable" and "JWT expired" in c.detail["error"]
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
    assert "/api/sessions/capture: push failed" in c.detail["error"]
    assert c.text == "2 failed · 1 partial"


def test_unbound_flags_only_floating():
    rows = [
        {"project_id": "p", "est_item_id": "a", "important": False, "serves": []},
        {"project_id": "p", "est_item_id": "a", "important": False, "serves": []},  # 2 versions
        {"project_id": "p", "est_item_id": "b", "important": True, "serves": []},
        {"project_id": "p", "est_item_id": "c", "important": True, "serves": ["x"]},
    ]
    c = dh.check_unbound(_Client({"spine_substance": rows}))
    assert c.detail == {"unbound": 3, "people": 0, "floating": 1,
                        "floating_closed": 0} and not c.ok
    assert dh.check_unbound(_Client({"spine_substance": rows[:1]})).ok


def test_unbound_skips_closed_workstreams_and_counts_people_apart():
    rows = [
        {"project_id": "open", "est_item_id": "a", "important": True, "serves": []},
        {"project_id": "shut", "est_item_id": "b", "important": True, "serves": []},
        {"project_id": "open", "est_item_id": "p1", "important": False, "serves": [],
         "layer": "Stakeholders"},
        {"project_id": "open", "est_item_id": "p2", "important": False, "serves": [],
         "layer": "Stakeholders"},
    ]
    projects = [{"id": "shut", "mc_status": "Closed"}]
    c = dh.check_unbound(_Client({"spine_substance": rows, "projects": projects}))
    assert c.detail == {"unbound": 4, "people": 2, "floating": 1, "floating_closed": 1}
    assert c.text == "2 · 2 people · 1 floating" and not c.ok
    # Only the closed workstream's element floating: nothing to act on.
    only_closed = _Client({"spine_substance": rows[1:], "projects": projects})
    assert dh.check_unbound(only_closed).ok


def test_unbound_status_read_failure_keeps_every_floating_element():
    rows = [{"project_id": "shut", "est_item_id": "b", "important": True, "serves": []}]
    c = dh.check_unbound(_Client({"spine_substance": rows,
                                  "projects": RuntimeError("projects 503")}))
    assert c.detail["floating"] == 1 and not c.ok


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
    assert not c.ok and c.text.endswith("· deps failing")
    assert "ImportError" in c.detail["error"] and "ImportError" not in c.text
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


def test_post_destination_precedence(monkeypatch):
    """--channel beats CP_HEALTH_CHANNEL beats the partners' channel."""
    import cp_engine.daily_health as dh
    from cp_engine import slack

    sent = {}
    monkeypatch.setattr(slack, "load_slack_token", lambda config: "xoxb-test")
    monkeypatch.setattr(slack, "post_channel", lambda web, channel_id, text: sent.setdefault("ch", channel_id) or "ts")
    monkeypatch.setattr(dh, "_partners_channel", lambda client, errors: "CPARTNERS")
    report = dh.HealthReport(when=dh.tenant_now(), checks=[])

    monkeypatch.setenv("CP_HEALTH_CHANNEL", "DENV")
    dh.post(report, config=None, client=object(), channel="DFLAG")
    assert sent.pop("ch") == "DFLAG"
    dh.post(report, config=None, client=object())
    assert sent.pop("ch") == "DENV"
    monkeypatch.delenv("CP_HEALTH_CHANNEL")
    dh.post(report, config=None, client=object())
    assert sent.pop("ch") == "CPARTNERS"


def test_github_get_uses_the_repos_own_token_for_its_own_repo(monkeypatch):
    """The tenant's sync runs 404'd with GH_PAT; the job's own token reads them."""
    import cp_engine.daily_health as dh

    seen = []

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    def fake_urlopen(req, timeout=0):
        seen.append((req.full_url, req.headers["Authorization"]))
        return _Resp()

    monkeypatch.setattr(dh.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("GITHUB_REPOSITORY", "FirstPersonSF/cp")
    monkeypatch.setenv("GITHUB_REPO_TOKEN", "own")
    monkeypatch.setenv("GH_TOKEN", "pat")
    dh.github_get("/repos/FirstPersonSF/cp/actions/workflows/sync.yml/runs")
    dh.github_get("/repos/FirstPersonSF/cp-engine/actions/workflows/tests.yml/runs")
    assert seen[0][1] == "Bearer own"
    assert seen[1][1] == "Bearer pat"


# ── Sync line from Railway: git fallback when Actions is unreadable ────────


def _git_repo_with_commits(root: Path, messages_and_times: list[tuple[str, datetime]]):
    import os
    import subprocess

    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for i, (msg, when) in enumerate(messages_and_times):
        (root / f"f{i}").write_text(str(i))
        env = {**os.environ, "GIT_AUTHOR_DATE": when.isoformat(),
               "GIT_COMMITTER_DATE": when.isoformat(),
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
        subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", msg], check=True, env=env)


def test_sync_falls_back_to_the_newest_cp_sync_commit_and_says_so(tmp_path: Path):
    _git_repo_with_commits(tmp_path, [
        ("[cp-sync] 2026-09-30T05:08Z — auto-sync from MC-2",
         NOW - timedelta(hours=1, minutes=22)),
        ("[auto-ingest] ggl-5168: meeting 1", NOW - timedelta(minutes=10)),
    ])
    c = dh.check_sync(_gh({"sync.yml": RuntimeError("HTTP Error 404: Not Found")}),
                      "FirstPersonSF/cp", NOW, tmp_path)
    assert c.ok, c.text
    assert c.text == "05:08 · via git"
    assert "404" in c.detail["error"]  # the Actions reason: --json / logs, not Slack
    assert c.detail["source"] == "git"


def test_sync_git_fallback_with_an_old_commit_is_not_ok(tmp_path: Path):
    _git_repo_with_commits(tmp_path, [("[cp-sync] old", NOW - timedelta(hours=40))])
    c = dh.check_sync(_gh({"sync.yml": RuntimeError("404")}), "o/r", NOW, tmp_path)
    assert not c.ok and c.text.endswith("· via git · 40h old")


def test_sync_with_neither_source_readable_is_a_warning(tmp_path: Path):
    _git_repo_with_commits(tmp_path, [("[auto-ingest] x", NOW)])
    c = dh.check_sync(_gh({"sync.yml": RuntimeError("404")}), "o/r", NOW, tmp_path)
    assert not c.ok and c.text == "unreadable"
    assert "no [cp-sync] commit" in c.detail["error"] and "404" in c.detail["error"]


def test_sync_prefers_actions_when_readable(tmp_path: Path):
    _git_repo_with_commits(tmp_path, [("[cp-sync] x", NOW)])
    c = dh.check_sync(_gh({"sync.yml": {"conclusion": "failure", "updated_at": _iso(NOW)}}),
                      "o/r", NOW, tmp_path)
    assert not c.ok and c.detail["source"] == "actions"  # a git commit never masks a red run


# ── The line carries no error text (house UI rule 1) ──────────────────────

_LEAK = "HTTPError: HTTP Error 404: Not Found while reading the thing"


def _failing_report(tmp_path: Path) -> dh.HealthReport:
    """Every check in a failure mode whose source hands back a long error."""
    def gh_boom(_):
        raise RuntimeError(_LEAK)

    _git_repo_with_commits(tmp_path, [("[cp-sync] x", NOW - timedelta(hours=40))])
    leak_client = _Client({
        "auto_ingest_runs": [{"status": "failed", "errors": [_LEAK]}],
        "webhook_runs": [{"route": "/api/x", "status": "failed", "error": _LEAK}],
        "spine_promote_runs": RuntimeError(_LEAK),
        "asset_ingest_runs": [],
        "spine_substance": RuntimeError(_LEAK),
    })
    checks = [
        dh.check_sync(gh_boom, "o/r", NOW, tmp_path),
        dh.check_sync(gh_boom, "o/r", NOW, None),
        dh.check_ingest(leak_client, NOW),
        dh.check_ingest(_Client({"auto_ingest_runs": RuntimeError(_LEAK)}), NOW),
        dh.check_ingest(None, NOW),
        dh.check_webhook(leak_client, NOW),
        dh.check_webhook(None, NOW),
        dh.check_hosted(None, _LEAK),
        dh.check_hosted({"status": "healthy", "server_version": "h/0.1", "tool_count": 1,
                         "deps_ok": False, "deps": _LEAK}),
        dh.check_hosted({"status": _LEAK, "server_version": "h/0.1", "tool_count": 1}),
        dh.check_ci(gh_boom, NOW),
        dh.check_unbound(leak_client),
        dh.check_unbound(None),
        dh.check_stale_summaries(None),
        dh.check_copies(None, "0.1", "2026-W40"),
        dh.check_copies({"server_version": "h/0.1"}, "0.1", "2026-W40"),
    ]
    return dh.HealthReport(when=NOW, checks=checks)


def test_no_line_carries_error_text_and_every_segment_is_three_words_or_fewer(tmp_path):
    rep = _failing_report(tmp_path)
    text = rep.render()
    assert "404" not in text and "HTTPError" not in text and "Not Found" not in text
    for check in rep.checks:
        for seg in check.text.split(" · "):
            assert len(seg.split()) <= 3, f"{check.label}: {seg!r} (in {check.text!r})"


def test_the_error_the_line_leaves_out_is_kept_in_detail_and_json(tmp_path):
    rep = _failing_report(tmp_path)
    assert all(_LEAK in c.detail.get("error", "") for c in rep.checks
               if c.label in ("CI main", "Hosted") and not c.detail.get("version"))
    errors = rep.errors()
    assert "404" in errors["Sync"] and "404" in errors["CI main"]
    assert any(_LEAK in e for e in errors.values())
    as_json = rep.to_dict()["checks"]
    assert any(_LEAK in (c.get("error") or "") for c in as_json)


@pytest.mark.parametrize("make,expected", [
    (lambda t: dh.check_ci(lambda _: (_ for _ in ()).throw(RuntimeError("x")), NOW),
     "⚠️ CI main · unreadable"),
    (lambda t: dh.check_ingest(None, NOW), "⚠️ Ingest 24h · no client"),
    (lambda t: dh.check_stale_summaries(None), "⚠️ Stale summaries · no checkout"),
    (lambda t: dh.check_hosted(None, "URLError: timed out"), "⚠️ Hosted · unreachable"),
])
def test_unreadable_lines_render_a_short_reason(tmp_path, make, expected):
    assert make(tmp_path).render() == expected


def test_cli_prints_the_errors_the_line_leaves_out_to_stderr(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from cp_engine.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dh, "gather", lambda **kw: dh.HealthReport(
        when=NOW, checks=[dh.Check("Sync", True, "05:08 · via git",
                                   {"error": "Actions: HTTPError: 404"})]))
    monkeypatch.setattr("cp_engine.mc2_db.get_client", lambda *a, **k: None)
    res = CliRunner().invoke(main, ["health"])
    assert "✅ Sync · 05:08 · via git" in res.stdout and "404" not in res.stdout
    assert "Sync: Actions: HTTPError: 404" in res.stderr


# ── Sync line once the schedule runs on the Railway cron (2026-10) ────────


def _cron_row(status, outcome, hours_ago, error=None):
    return {"status": status, "error": error, "created_at": _iso(NOW - timedelta(hours=hours_ago)),
            "detail": {"job": "sync", "outcome": outcome}}


def _actions_never(path):
    raise AssertionError(f"Actions read although the cron row answers: {path}")


@pytest.mark.parametrize("row,ok,text", [
    (_cron_row("ok", "pushed", 1.5), True, "05:00 ok · via cron"),
    (_cron_row("ok", "no_changes", 1.5), True, "05:00 ok · via cron"),
    (_cron_row("failed", "failed", 1.5, "CalledProcessError: push"), False, "05:00 failed · via cron"),
    (_cron_row("partial", "pushed", 1.5, "1 managed-region edit"), False, "05:00 partial · via cron"),
    (_cron_row("ok", "pushed", 40), False, "09-28 14:30 ok · via cron · 40h old"),
])
def test_sync_reads_the_cron_run_row_first(row, ok, text):
    """FAILS on the Actions-only check: after `schedule:` leaves sync.yml its
    newest run is days old, and the git fallback cannot tell failed from
    no-changes."""
    c = dh.check_sync(_actions_never, "o/r", NOW, None, _Client({"webhook_runs": [row]}))
    assert (c.ok, c.text) == (ok, text)
    assert c.detail["source"] == "cron"
    if row["error"]:
        assert row["error"] in c.detail["error"] and row["error"] not in c.text


def test_sync_skips_cron_dry_runs_and_falls_back_without_a_row():
    dry = {"webhook_runs": [_cron_row("ok", "dry_run", 1)]}
    c = dh.check_sync(_gh({"sync.yml": {"conclusion": "success",
                                        "updated_at": _iso(NOW - timedelta(hours=2))}}),
                      "o/r", NOW, None, _Client(dry))
    assert c.detail["source"] == "actions" and c.ok


def test_sync_unreadable_ledger_is_named_in_the_fallback_error(tmp_path: Path):
    _git_repo_with_commits(tmp_path, [("[cp-sync] x", NOW - timedelta(hours=1))])
    c = dh.check_sync(_gh({"sync.yml": RuntimeError("404")}), "o/r", NOW, tmp_path,
                      _Client({"webhook_runs": RuntimeError("JWT expired")}))
    assert c.detail["source"] == "git" and "JWT expired" in c.detail["error"]


def test_json_status_is_not_overwritten_by_detail_keys():
    """The ingest check's detail carries an "ok" RUN COUNT; merged after the
    status it turned `"ok": true` into `"ok": 1` in --json output."""
    import json as _json

    import cp_engine.daily_health as dh

    report = dh.HealthReport(when=dh.tenant_now(), checks=[
        dh.Check("Ingest 24h", True, "1 ok · 0 partial · 0 failed", {"ok": 1, "partial": 0}),
    ])
    row = _json.loads(_json.dumps(report.to_dict()))["checks"][0]
    assert row["ok"] is True
    assert row["partial"] == 0


# ── moved from the retired dates loop's tests (step 5a) ──


def test_partners_channel_from_app_config() -> None:
    from cp_engine.daily_health import _partners_channel

    from unittest.mock import MagicMock

    client = MagicMock()
    chain = client.table.return_value.select.return_value.eq.return_value
    # Bare-string value
    chain.execute.return_value.data = [
        {"key": "dates_loop_partners_channel", "value": "C0PARTNERS"}
    ]
    assert _partners_channel(client) == "C0PARTNERS"
    # Object value
    chain.execute.return_value.data = [
        {"key": "dates_loop_partners_channel", "value": {"channel": "C0X"}}
    ]
    assert _partners_channel(client) == "C0X"
    # Absent key → None (rollup skipped)
    chain.execute.return_value.data = []
    assert _partners_channel(client) is None
    # Lookup failure → None, never raises
    chain.execute.side_effect = RuntimeError("boom")
    assert _partners_channel(client) is None


def test_post_channel_surfaces_slack_error_code() -> None:
    """SlackApiError's str() is generic; the actionable code lives in
    exc.response and must reach the SlackError message (the first live
    dates-loop failure was undiagnosable without it)."""
    import pytest

    from cp_engine import slack as slack_mod

    class FakeApiError(Exception):
        def __init__(self):
            super().__init__("The request to the Slack API failed.")
            self.response = {"ok": False, "error": "channel_not_found"}

    class FakeClient:
        def chat_postMessage(self, **kwargs):
            raise FakeApiError()

    with pytest.raises(slack_mod.SlackError, match=r"\[slack error: channel_not_found\]"):
        slack_mod.post_channel(FakeClient(), channel_id="C123", text="hi")


def test_unbound_canon_member_is_not_floating():
    rows = [{"project_id": "p", "est_item_id": "rule", "important": True, "serves": []}]
    canon = [{"project_id": "p", "from_item_id": "rule", "kind": "canon_of"}]
    c = dh.check_unbound(_Client({"spine_substance": rows, "spine_relations": canon}))
    assert c.ok and c.detail["floating"] == 0


def test_unbound_deliverable_is_not_floating():
    """A deliverable is the work others serve (2026-10-06): an important one
    serving nothing is its normal state, not a floating finding."""
    rows = [{"project_id": "p", "est_item_id": "story-flow", "important": True,
             "serves": [], "layer": "Deliverables"},
            {"project_id": "p", "est_item_id": "note", "important": True,
             "serves": [], "layer": "Synthesis"}]
    c = dh.check_unbound(_Client({"spine_substance": rows}))
    assert c.detail["floating"] == 1 and c.text == "2 · 1 floating"
    assert dh.check_unbound(_Client({"spine_substance": rows[:1]})).ok
