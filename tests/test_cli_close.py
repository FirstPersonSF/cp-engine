"""Tests for `cp close` — the close-out ritual scaffold verb."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from cp_engine.cli import main

_TODAY = date.today().isoformat()


# ── fakes ─────────────────────────────────────────────────────────────


class _Query:
    """Chainable supabase-py query stub — returns canned rows on execute."""

    def __init__(self, data: list[dict], log: list, name: str):
        self._data = data
        self._log = log
        self._name = name

    def select(self, *a, **k):
        return self

    def eq(self, column, value):
        self._log.append((self._name, column, value))
        return self

    def order(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        return SimpleNamespace(data=self._data)


class FakeClient:
    def __init__(self, tables: dict[str, list[dict]]):
        self._tables = tables
        self.eq_log: list[tuple] = []  # (table, column, value)

    def table(self, name: str) -> _Query:
        return _Query(self._tables.get(name, []), self.eq_log, name)


def _tables(*, mc_status: str = "Closed") -> dict[str, list[dict]]:
    return {
        "projects": [{"id": "p1", "number": 5144, "mc_status": mc_status}],
        "spine_substance": [
            {
                "id": "tel-5144/a/v3",
                "est_item_id": "_authored/what-we-made",
                "framing": "What we made and why it landed",
                "layer": "Synthesis",
                "status": "live",
                "version_label": "v3",
                "version_date": "2026-06-01",
                "body": "x" * 900,
                "serves": [],
                "scope": None,
                "archived": False,
            },
            {
                "id": "tel-5144/b/v1",
                "est_item_id": "_authored/thin-note",
                "framing": "Thin note",
                "layer": "Note",
                "status": "live",
                "version_label": "v1",
                "version_date": "2026-05-01",
                "body": "tiny",
                "serves": [],
                "scope": None,
                "archived": False,
            },
        ],
        "commitments": [
            {
                "id": "c1",
                "description": "Send Teleflex the final naming deck",
                "owner_name": "Drew",
                "owner_email": "drew@firstperson.is",
                "direction": "us_to_them",
                "due_date": "2026-08-01",
                "status": "open",
            }
        ],
    }


# ── tenant scaffolding ────────────────────────────────────────────────


def _tenant(tmp_path: Path, *, parked: bool) -> Path:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.18"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )
    rel = (
        "1p/teleflex/inactive/tel-5144-urolift-3-naming"
        if parked
        else "1p/teleflex/tel-5144-urolift-3-naming"
    )
    proj = tmp_path / rel
    proj.mkdir(parents=True)
    (proj / "cp.md").write_text("# tel-5144\n", encoding="utf-8")
    return proj


def _patch_client(monkeypatch, client) -> None:
    import cp_engine.mc2_db as mc2_db

    monkeypatch.setattr(mc2_db, "get_client", lambda *a, **k: client)


# ── tests ─────────────────────────────────────────────────────────────


def test_close_scaffolds_checklist_for_inactive_project(tmp_path, monkeypatch):
    proj = _tenant(tmp_path, parked=True)
    client = FakeClient(_tables(mc_status="Closed"))
    _patch_client(monkeypatch, client)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["close", "tel-5144"])
    assert result.exit_code == 0, result.output

    out = proj / f"close-out-tel-5144-{_TODAY}.md"
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    # Real element data (both selector paths) + real commitment data.
    assert "`_authored/what-we-made`" in text
    assert "What we made and why it landed" in text
    assert "`_authored/thin-note`" in text and "4 chars" in text
    assert "Send Teleflex the final naming deck" in text
    assert "Closed (project)" in text
    assert "Close-out checklist written" in result.output
    assert "1 open commitment(s)" in result.output
    # The spine read keys on the DIR-SLUG project_code (== the dir name),
    # not the short engagement code (the tel-5144 vs tel-5144-… gotcha).
    assert ("spine_substance", "project_code", "tel-5144-urolift-3-naming") in client.eq_log


def test_close_refuses_active_project_without_force(tmp_path, monkeypatch):
    proj = _tenant(tmp_path, parked=False)
    _patch_client(monkeypatch, FakeClient(_tables(mc_status="Open")))
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["close", "tel-5144"])
    assert result.exit_code == 1
    assert "REFUSING" in result.output
    assert "--force" in result.output
    assert not list(proj.glob("close-out-*"))  # nothing written


def test_close_force_overrides_active_refusal(tmp_path, monkeypatch):
    proj = _tenant(tmp_path, parked=False)
    _patch_client(monkeypatch, FakeClient(_tables(mc_status="Open")))
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["close", "tel-5144", "--force"])
    assert result.exit_code == 0, result.output
    assert (proj / f"close-out-tel-5144-{_TODAY}.md").is_file()


def test_close_offline_parked_dir_proceeds_from_mirror(tmp_path, monkeypatch):
    proj = _tenant(tmp_path, parked=True)
    _patch_client(monkeypatch, None)  # MC-2 unreachable
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["close", "tel-5144"])
    assert result.exit_code == 0, result.output
    text = (proj / f"close-out-tel-5144-{_TODAY}.md").read_text(encoding="utf-8")
    assert "unverified — MC-2 unreachable" in text
    assert "commitments unavailable" in text  # never claims a verified-empty sweep


def test_close_offline_live_dir_refuses_without_force(tmp_path, monkeypatch):
    _tenant(tmp_path, parked=False)
    _patch_client(monkeypatch, None)
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["close", "tel-5144"])
    assert result.exit_code == 1
    assert "REFUSING" in result.output


def test_close_unknown_project_exits_nonzero(tmp_path, monkeypatch):
    _tenant(tmp_path, parked=True)
    _patch_client(monkeypatch, FakeClient(_tables()))
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(main, ["close", "nope-9999"])
    assert result.exit_code == 1
    assert "No working dir" in result.output
