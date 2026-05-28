"""Tests for `cp attention-digest` CLI subcommand (Task 2.4)."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from cp_engine.cli import main


def _fake_tenant(tmp_path: Path) -> Path:
    """Drop a minimal .cp-engine.toml + empty sprints dir.

    Uses backend="github-issues" so no [sync.mc_2] block is needed and
    `load()` succeeds without a real Supabase ref.
    """
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.11"\n'
        '[sync]\nbackend = "github-issues"\n',
        encoding="utf-8",
    )
    (tmp_path / "sprints").mkdir()
    return tmp_path


def test_cli_attention_digest_prints_markdown_to_stdout(tmp_path, monkeypatch):
    """Default invocation prints markdown to stdout (no Slack post)."""
    _fake_tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["attention-digest"])
    # Even with no sprint dir, the "all clear" message should print.
    assert result.exit_code == 0, result.output
    assert "all clear" in result.output.lower()


def test_cli_attention_digest_uses_recipient_from_option(tmp_path, monkeypatch):
    """`--recipient Tony` puts Tony's name in the output."""
    _fake_tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["attention-digest", "--recipient", "Tony"])
    assert result.exit_code == 0
    assert "Tony" in result.output


def test_cli_attention_digest_post_to_slack_fails_clean_when_no_recipients(tmp_path, monkeypatch):
    """`--post-to-slack` with empty recipients → SlackError with clean message + non-zero exit."""
    _fake_tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["attention-digest", "--post-to-slack"])
    assert result.exit_code != 0
    assert "no recipients configured" in result.output.lower()


def test_cli_attention_digest_today_option_overrides_date(tmp_path, monkeypatch):
    """`--today 2026-05-27` lets tests pin the digest's reference date."""
    _fake_tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["attention-digest", "--today", "2026-05-27"])
    assert result.exit_code == 0
    # We're not asserting specific dates render — just that the option parses.


# ──────────────────────────────────────────────────────────────────────
#  ClickUp task-id lookup degradation (Task 2.1)
# ──────────────────────────────────────────────────────────────────────


def test_attention_digest_sends_when_clickup_lookup_fails(monkeypatch):
    """If Supabase is unavailable, the digest still sends — ClickUp-linked
    asks just get the standard Resolve buttons instead of an Open-in-ClickUp
    link. NEW NETWORK DEPENDENCY in the digest path: if this fails wrong,
    every daily digest fails."""
    from cp_engine.cli import _fetch_clickup_task_ids_for_hashes
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    result = _fetch_clickup_task_ids_for_hashes(None, ["aaaa1111", "bbbb2222"])
    assert result == {}


def test_attention_digest_sends_when_clickup_query_raises(monkeypatch):
    """Even when create_client raises, the helper returns {} and digest send proceeds."""
    from cp_engine.cli import _fetch_clickup_task_ids_for_hashes
    monkeypatch.setenv("SUPABASE_URL", "https://nonexistent.invalid")
    monkeypatch.setenv("SUPABASE_KEY", "fake")

    def _boom(*a, **k):
        raise RuntimeError("network failure")

    monkeypatch.setattr("supabase.create_client", _boom, raising=False)
    result = _fetch_clickup_task_ids_for_hashes(None, ["aaaa1111"])
    assert result == {}
