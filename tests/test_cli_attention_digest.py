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


def test_cli_attention_digest_post_to_slack_flag_calls_stub(tmp_path, monkeypatch):
    """`--post-to-slack` invokes the stub; stub currently exits with a clean error message."""
    _fake_tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["attention-digest", "--post-to-slack"])
    # Stub raises NotImplementedError, which the CLI should catch and turn into a clean exit.
    assert result.exit_code != 0
    assert "Task 2.6" in result.output


def test_cli_attention_digest_today_option_overrides_date(tmp_path, monkeypatch):
    """`--today 2026-05-27` lets tests pin the digest's reference date."""
    _fake_tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["attention-digest", "--today", "2026-05-27"])
    assert result.exit_code == 0
    # We're not asserting specific dates render — just that the option parses.
