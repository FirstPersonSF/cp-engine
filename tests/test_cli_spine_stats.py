"""`cxp spine-stats` is retired — it explains itself instead of crashing.

mc-2 migration 072 dropped `spine_elements`; the command queried it and died
with a raw PGRST205 traceback. The tests that asserted its report output went
with the reports (see tests/test_spine_stats.py for why they cannot be
repointed). What remains worth pinning is the user-visible contract.
"""
from pathlib import Path

from click.testing import CliRunner

from cp_engine.cli import main


def _tenant(tmp_path: Path) -> None:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )


def _explode(config=None, **kw):  # pragma: no cover - reaching it IS the failure
    raise AssertionError("spine-stats must not open an MC-2 client while retired")


def test_spine_stats_explains_retirement_and_exits_clean(tmp_path, monkeypatch):
    """Exit 0 with a reason — not a PGRST205 traceback, not a bare '(none)'."""
    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cp_engine.mc2_db.get_client", _explode)

    result = CliRunner().invoke(main, ["spine-stats"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "unavailable" in out
    assert "072" in out, "the message must name the migration that caused this"
    assert "spine_substance" in out, "and what replaced the dropped table"
    # The old failure mode must not reappear.
    assert "PGRST205" not in out
    assert "Traceback" not in out


def test_spine_stats_does_not_reach_mc2_at_all(tmp_path, monkeypatch):
    """The retirement short-circuits BEFORE any client is built.

    This is a deliberate behaviour change: the command used to exit non-zero
    with "cross-project stats need MC-2" when creds were missing. It no longer
    reaches that path, so a tenant with no creds gets the retirement notice
    rather than a credentials error. Pinned so the swap is explicit rather
    than discovered.
    """
    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cp_engine.mc2_db.get_client", _explode)

    for argv in (
        ["spine-stats"],
        ["spine-stats", "--type", "positioning-narrative"],
        ["spine-stats", "--within-days", "60"],
    ):
        result = CliRunner().invoke(main, argv)
        assert result.exit_code == 0, f"{argv}: {result.output}"
        assert "unavailable" in result.output


def test_spine_stats_options_still_parse(tmp_path, monkeypatch):
    """Retired, but the flags must not become parse errors for scripted callers."""
    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("cp_engine.mc2_db.get_client", _explode)

    result = CliRunner().invoke(
        main, ["spine-stats", "--type", "x", "--within-days", "3"]
    )
    assert result.exit_code == 0
    assert "no such option" not in result.output.lower()
