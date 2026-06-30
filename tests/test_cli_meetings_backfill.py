"""Tests for the `meetings-backfill` CLI verb.

Monkeypatches the backfill driver + cred loaders + client builder so nothing
touches MC-2 / Voyage. Exercises the CLI surface only: argument validation,
summary echoing (incl. unresolved listing), and exit codes.
"""
from __future__ import annotations

import cp_engine.cli as cli_mod
import cp_engine.meetings as meetings
from click.testing import CliRunner

from cp_engine.cli import main


def _stub_creds_and_client(monkeypatch):
    """Make the creds loaders + client builder harmless sentinels."""
    monkeypatch.setattr(cli_mod, "build_mc2_client", lambda: object())
    monkeypatch.setattr(cli_mod, "_load_config_or_die", lambda: object())
    # Both cred loaders are imported from sync_mc2; patch at the cli module's
    # call sites (they're referenced via the sync_mc2 module).
    import cp_engine.sync_mc2 as sync_mc2
    monkeypatch.setattr(sync_mc2, "_load_supabase_creds", lambda config: ("u", "k"))
    monkeypatch.setattr(sync_mc2, "_load_ingest_creds", lambda config: None)


def test_meetings_backfill_both_code_and_all_exit_2(monkeypatch):
    _stub_creds_and_client(monkeypatch)
    result = CliRunner().invoke(main, ["meetings-backfill", "ibx-5167", "--all"])
    assert result.exit_code == 2, result.output


def test_meetings_backfill_neither_exit_2(monkeypatch):
    _stub_creds_and_client(monkeypatch)
    result = CliRunner().invoke(main, ["meetings-backfill"])
    assert result.exit_code == 2, result.output


def test_meetings_backfill_happy_run_echoes_summary(monkeypatch):
    _stub_creds_and_client(monkeypatch)

    captured = {}

    def fake_backfill(client, *, code=None, supabase_url, supabase_key):
        captured["code"] = code
        captured["url"] = supabase_url
        captured["key"] = supabase_key
        return {"total": 3, "linked": 2, "skipped": 1, "failed": 0,
                "unresolved": []}

    monkeypatch.setattr(meetings, "backfill_meetings", fake_backfill)

    result = CliRunner().invoke(main, ["meetings-backfill", "ibx-5167"])
    assert result.exit_code == 0, result.output
    assert captured["code"] == "ibx-5167"
    assert captured["url"] == "u"
    out = result.output
    assert "total=3" in out
    assert "linked=2" in out
    assert "skipped=1" in out
    assert "failed=0" in out


def test_meetings_backfill_all_passes_code_none(monkeypatch):
    _stub_creds_and_client(monkeypatch)

    captured = {}

    def fake_backfill(client, *, code=None, supabase_url, supabase_key):
        captured["code"] = code
        return {"total": 0, "linked": 0, "skipped": 0, "failed": 0,
                "unresolved": []}

    monkeypatch.setattr(meetings, "backfill_meetings", fake_backfill)

    result = CliRunner().invoke(main, ["meetings-backfill", "--all"])
    assert result.exit_code == 0, result.output
    assert captured["code"] is None


def test_meetings_backfill_lists_unresolved(monkeypatch):
    _stub_creds_and_client(monkeypatch)

    monkeypatch.setattr(
        meetings, "backfill_meetings",
        lambda client, *, code=None, supabase_url, supabase_key: {
            "total": 2, "linked": 1, "skipped": 1, "failed": 0,
            "unresolved": [99, "Meeting Foo"],
        },
    )

    result = CliRunner().invoke(main, ["meetings-backfill", "--all"])
    assert result.exit_code == 0, result.output
    assert "99" in result.output
    assert "Meeting Foo" in result.output


def test_meetings_backfill_failed_exits_nonzero(monkeypatch):
    _stub_creds_and_client(monkeypatch)

    monkeypatch.setattr(
        meetings, "backfill_meetings",
        lambda client, *, code=None, supabase_url, supabase_key: {
            "total": 3, "linked": 2, "skipped": 0, "failed": 1,
            "unresolved": [],
        },
    )

    result = CliRunner().invoke(main, ["meetings-backfill", "--all"])
    assert result.exit_code != 0
    assert "failed=1" in result.output
