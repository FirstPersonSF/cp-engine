"""CLI test for `cp spine-recover`: thin wiring + dry-run writes nothing.

The orchestration logic is covered in test_spine_recover.py; here we assert the
command wiring — resolution, dry-run vs --apply boundary, readable report, and
the ANTHROPIC_API_KEY-missing failure when an element needs re-distilling.
"""

from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from cp_engine.cli import main

CANON = "ibx-5153"
PID = "0b27ca26-a6cd-46a0-a7ab-816549f9a1d2"
CID = "company-uuid-1"


def _tenant(tmp_path: Path) -> Path:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.30"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "stub"\n',
        encoding="utf-8",
    )
    proj = tmp_path / "1p" / "infoblox" / "ibx-5153-ai-campaign"
    spine = proj / "spine"
    # One synthesis Decisions element (carries verbatim — no asset, no LLM).
    dec = spine / "Decisions" / "two-track.md"
    dec.parent.mkdir(parents=True, exist_ok=True)
    dec.write_text(
        "---\n"
        "id: ibx-5153/decisions/two-track\n"
        "project: ibx-5153\n"
        "layer: Decisions\n"
        'title: "Two-track AI story"\n'
        "status: active\n"
        "last_touched: 2026-05-27\n"
        "---\n"
        "the durable decision body\n",
        encoding="utf-8",
    )
    return proj


def _add_source_backed(proj: Path) -> None:
    sm = proj / "spine" / "SourceMaterial" / "v6-deck.md"
    sm.parent.mkdir(parents=True, exist_ok=True)
    sm.write_text(
        "---\n"
        "id: ibx-5153/sourcematerial/v6-deck\n"
        "project: ibx-5153\n"
        "layer: SourceMaterial\n"
        'title: "Infoblox Platform v6 deck"\n'
        "status: active\n"
        "last_touched: 2026-05-27\n"
        "source:\n"
        "  - Reference Materials/Infoblox_Platform_v6.pptx\n"
        "---\n"
        "stale deck summary\n",
        encoding="utf-8",
    )


class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._op = None
        self._filters = []

    def select(self, cols):
        assert "*" not in cols
        self._op = "select"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def order(self, *a, **k):
        return self

    def upsert(self, rows, on_conflict=None):
        self.store.setdefault("_upserts", []).append((self.name, list(rows)))
        self._op = "upsert"
        return self

    def execute(self):
        if self._op == "upsert":
            return type("R", (), {"data": []})()
        rows = self.store.get(self.name, [])
        sel = [r for r in rows if all(r.get(c) == v for c, v in self._filters)]
        return type("R", (), {"data": sel})()


class _FakeClient:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _FakeTable(self.store, name)


def _patch(monkeypatch, client, assets=None):
    monkeypatch.setattr(
        "cp_engine.mc2_db.get_client", lambda config=None, **kw: client
    )
    monkeypatch.setattr(
        "cp_engine.mc2_db._resolve_project_id", lambda c, code: PID
    )
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders_by_id",
        lambda c, pid: SimpleNamespace(project_id=PID, company_id=CID),
    )
    client.store["rag_assets"] = [
        {"project_id": PID, "status": "active", **a} for a in (assets or [])
    ]


def test_dry_run_prints_report_and_writes_nothing(tmp_path, monkeypatch):
    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = {}
    client = _FakeClient(store)
    _patch(monkeypatch, client)

    res = CliRunner().invoke(main, ["spine-recover", CANON])
    assert res.exit_code == 0, res.output
    assert "DRY RUN" in res.output
    assert "Two-track AI story" in res.output
    assert "carry" in res.output
    assert "_upserts" not in store  # nothing written


def test_apply_writes_rows(tmp_path, monkeypatch):
    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    store = {}
    client = _FakeClient(store)
    _patch(monkeypatch, client)

    res = CliRunner().invoke(main, ["spine-recover", CANON, "--apply"])
    assert res.exit_code == 0, res.output
    assert "Wrote 1 rows under ibx-5153" in res.output
    upserts = store.get("_upserts", [])
    assert len(upserts) == 1
    name, rows = upserts[0]
    assert name == "spine_substance"
    assert all(r["project_code"] == CANON and r["origin"] == "authored" for r in rows)


def test_missing_api_key_when_redistill_needed_errors(tmp_path, monkeypatch):
    proj = _tenant(tmp_path)
    _add_source_backed(proj)  # source-backed element → needs re-distill
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    store = {}
    client = _FakeClient(store)
    # Seed a matching asset so plan_element marks the element redistill.
    _patch(monkeypatch, client, assets=[
        {"id": "a1", "title": "Infoblox_Platform_v6.pptx",
         "source_type": "deck", "created_at": "2026-05-01"},
    ])

    res = CliRunner().invoke(main, ["spine-recover", CANON])
    assert res.exit_code == 1, res.output
    assert "ANTHROPIC_API_KEY" in res.output


def test_unknown_project_errors(tmp_path, monkeypatch):
    _tenant(tmp_path)
    monkeypatch.chdir(tmp_path)
    client = _FakeClient({})
    monkeypatch.setattr(
        "cp_engine.mc2_db.get_client", lambda config=None, **kw: client
    )
    monkeypatch.setattr(
        "cp_engine.mc2_db._resolve_project_id", lambda c, code: None
    )
    res = CliRunner().invoke(main, ["spine-recover", CANON])
    assert res.exit_code == 1, res.output
    assert "No project_id" in res.output
