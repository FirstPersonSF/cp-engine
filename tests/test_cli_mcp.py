"""Tests for the `cp mcp` CLI subcommand (Task 5)."""
from __future__ import annotations

from click.testing import CliRunner

from cp_engine.cli import main


def test_cp_mcp_runs_stdio_server(monkeypatch):
    """`cp mcp` delegates to the stdio server runner.

    We monkeypatch `run_stdio` (the command imports it inside its body) so the
    test never spins a real stdio transport.
    """
    called = {"n": 0}

    def fake_run_stdio() -> None:
        called["n"] += 1

    monkeypatch.setattr("cp_engine.mcp_server.run_stdio", fake_run_stdio)

    result = CliRunner().invoke(main, ["mcp"])

    assert result.exit_code == 0, result.output
    assert called["n"] == 1
