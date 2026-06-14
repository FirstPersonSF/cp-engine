from pathlib import Path

from click.testing import CliRunner

from cp_engine.cli import main
from cp_engine.sync import BackendUnavailable


def _tenant(tmp_path: Path) -> None:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )


# --- fake MC-2 client (reuses the _FakeTable/_FakeClient pattern) --------------


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows
        self._filter = None

    def select(self, cols):
        return self

    def eq(self, col, val):
        self._filter = (col, val)
        return self

    def execute(self):
        rows = self._rows
        if self._filter:
            col, val = self._filter
            rows = [r for r in rows if r.get(col) == val]
        return type("R", (), {"data": rows})()


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return _FakeTable(self._rows)


# A wide future window so the seeded rows always fall in the default 14-day
# horizon is brittle (depends on date.today()); instead seed dates relative to
# today at call time. We seed absolute-ish rows and pass a big --within-days in
# the window test, and rely on the inventory/distribution sections (date-free)
# for the happy path.
def _seed_rows(today_iso: str, soon_iso: str, far_iso: str):
    return [
        {"layer": "Deliverables", "type": "positioning-narrative",
         "stage": "revised", "target_date": soon_iso,
         "project_code": "ibx-5153", "title": "IBX pos"},
        {"layer": "Deliverables", "type": "message-house",
         "stage": "first", "target_date": far_iso,
         "project_code": "sap-5171", "title": "SAP MH"},
        {"layer": "Deliverables", "type": "message-house",
         "stage": "conception", "target_date": None,
         "project_code": "ggl-5168", "title": "GGL MH (no date)"},
    ]


def _patch_connect(monkeypatch, rows):
    monkeypatch.setattr(
        "cp_engine.sync_mc2.MC2Backend.connect",
        lambda self, cfg: _FakeClient(rows),
    )


def test_shell_stats_happy_path(tmp_path, monkeypatch):
    """All three sections render: type inventory, stage distribution, due-soon."""
    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    # soon date = today (always in-window for default 14d); far date = +60d.
    from datetime import date, timedelta

    today = date.today()
    soon = today.isoformat()
    far = (today + timedelta(days=60)).isoformat()
    _patch_connect(monkeypatch, _seed_rows(today.isoformat(), soon, far))

    result = CliRunner().invoke(main, ["shell-stats"])
    assert result.exit_code == 0, result.output

    # type inventory: type + count
    assert "positioning-narrative" in result.output
    assert "message-house" in result.output
    # stage distribution
    assert "revised" in result.output
    assert "first" in result.output
    # due-soon: the in-window row shows, the +60d one does not
    assert "IBX pos" in result.output
    assert "SAP MH" not in result.output


def test_shell_stats_type_filter(tmp_path, monkeypatch):
    """--type narrows inventory + due-soon to one type only."""
    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    from datetime import date

    today = date.today()
    soon = today.isoformat()
    _patch_connect(monkeypatch, _seed_rows(today.isoformat(), soon, soon))

    result = CliRunner().invoke(
        main, ["shell-stats", "--type", "positioning-narrative"]
    )
    assert result.exit_code == 0, result.output
    assert "positioning-narrative" in result.output
    # message-house is filtered out of inventory AND due-soon
    assert "message-house" not in result.output
    assert "SAP MH" not in result.output
    assert "IBX pos" in result.output
    # stage distribution stays global under --type; the header says so
    assert "(all types)" in result.output


def test_shell_stats_within_days_passthrough(tmp_path, monkeypatch):
    """--within-days widens the due window so a far-future row appears."""
    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    from datetime import date, timedelta

    today = date.today()
    far = (today + timedelta(days=45)).isoformat()
    _patch_connect(monkeypatch, _seed_rows(today.isoformat(), far, far))

    # default 14d → the +45d rows are NOT due-soon
    default_res = CliRunner().invoke(main, ["shell-stats"])
    assert default_res.exit_code == 0, default_res.output
    assert "IBX pos" not in default_res.output

    # --within-days 60 → now they ARE due-soon
    wide_res = CliRunner().invoke(main, ["shell-stats", "--within-days", "60"])
    assert wide_res.exit_code == 0, wide_res.output
    assert "IBX pos" in wide_res.output


def test_shell_stats_offline_errors_no_fallback(tmp_path, monkeypatch):
    """MC-2 unavailable → clear cross-project error + non-zero exit, NO fallback."""
    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)

    def _raise(self, cfg):
        raise BackendUnavailable("no SUPABASE creds")

    monkeypatch.setattr("cp_engine.sync_mc2.MC2Backend.connect", _raise)

    result = CliRunner().invoke(main, ["shell-stats"])
    assert result.exit_code != 0
    assert "cross-project stats need MC-2" in result.output
    # distinct from cp shell's fallback wording
    assert "reading from disk" not in result.output
