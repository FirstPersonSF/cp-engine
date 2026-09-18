"""`cxp sync` raises the tenant pin floor (#296 step 3).

Before this change sync never touched `.cp-engine.toml`; the pin was bumped by
hand for ~20 releases and then not for 80. A control here shows the released
sync.py has no such write, so the pin's four-month freeze was structural, not
a lapse anyone could have noticed from inside the engine.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from cp_engine import health, sync

REPO = Path(__file__).resolve().parents[1]

_TOML = (
    '[tenant]\nname = "t"\n\n'
    '[engine]\nversion = "~= 0.42"  # stale\n\n'
    '[sync]\nbackend = "mc-2"\n'
)


def test_sync_raises_the_pin_in_place_and_reports_the_path(tmp_path: Path, monkeypatch, capsys):
    (tmp_path / ".cp-engine.toml").write_text(_TOML)
    monkeypatch.setattr(health, "installed_cli_version", lambda: "0.120.5")
    written = sync._raise_pin_floor(tmp_path)
    assert written == [tmp_path / ".cp-engine.toml"]
    text = (tmp_path / ".cp-engine.toml").read_text()
    assert 'version = "~= 0.120"  # stale' in text, "value moved, comment kept"
    assert "engine pin raised ~= 0.42 → ~= 0.120" in capsys.readouterr().out


def test_sync_leaves_a_current_or_locked_pin_alone(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(health, "installed_cli_version", lambda: "0.120.5")
    (tmp_path / ".cp-engine.toml").write_text(_TOML.replace("~= 0.42", "~= 0.120"))
    assert sync._raise_pin_floor(tmp_path) == []
    (tmp_path / ".cp-engine.toml").write_text(
        _TOML.replace('"~= 0.42"  # stale', '"~= 0.42"\nversion_lock = true')
    )
    assert sync._raise_pin_floor(tmp_path) == []
    assert capsys.readouterr().out == ""


def test_sync_missing_config_is_a_noop(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(health, "installed_cli_version", lambda: "0.120.5")
    assert sync._raise_pin_floor(tmp_path) == []


def test_control_released_sync_never_wrote_the_pin():
    """v0.120.5's sync.py has no pin write. The pin sat at ~= 0.42 for 80
    releases because nothing in the engine ever moved it."""
    old = subprocess.run(
        ["git", "-C", str(REPO), "show", "v0.120.5:src/cp_engine/sync.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "raise_pin_floor" not in old
