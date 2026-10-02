"""`cxp render --check` — the wrap-up word-count step, report-only.

WHAT BROKE. `cxp render` is a full sync. Run at wrap-up only to read the
word-count warnings (2026-10-01), it rewrote 78 tenant files and expired 3
MC-2 commitments. `--check` runs the advisory passes alone: same warnings, no
file written, no MC-2 call.

The plain render runs first in each test as the POSITIVE control — it must
write files and MC-2 rows through the very fakes `--check` is then held to,
so a clean `--check` means the path is clean, not that the harness is blind.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from click.testing import CliRunner

from cp_engine.cli import main
from tests._spine_fake import FakeClient
from tests.test_sprints import _fixture_project

PID = "00000000-0000-0000-0000-00000000cafe"


def _stale_ask(i: int) -> dict:
    # Undated meeting-ingest ask past the 14-day TTL: sync expires it (#136).
    return {"id": f"c{i}", "description": f"stale ask {i}", "status": "open",
            "due_date": None, "date_status": "proposed",
            "source_kind": "meeting_ingest", "project_id": PID,
            "created_at": "2026-08-01T12:00:00+00:00"}


class _Backend:
    def __init__(self, client):
        self.client, self.reads = client, 0

    def read_projects(self, config):
        self.reads += 1
        return (replace(_fixture_project(code="peb-5100"), mc2_id=PID),)

    def spine_client(self):
        return self.client


def _tenant(tmp_path: Path) -> Path:
    (tmp_path / ".cp-engine.toml").write_text(
        '[tenant]\nname = "test"\n'
        '[engine]\nversion = "~= 0.11"\n'
        '[sync]\nbackend = "mc-2"\n'
        '[sync.mc_2]\nsupabase_project_ref = "fake"\n',
        encoding="utf-8",
    )
    return tmp_path


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def _setup(tmp_path, monkeypatch):
    import cp_engine.mc2_db as mc2_db
    import cp_engine.sync as sync_mod

    root = _tenant(tmp_path)
    monkeypatch.chdir(root)
    client = FakeClient(commitments=[_stale_ask(1)], spine_substance=[],
                        projects=[], companies=[])
    backend = _Backend(client)
    monkeypatch.setattr(sync_mod, "_default_backend_factory", lambda _n: backend)
    # Any MC-2 door outside the backend lands on the same recorder.
    monkeypatch.setattr(mc2_db, "get_client", lambda *a, **k: client)
    return root, client, backend


def test_plain_render_writes_files_and_mc2_rows(tmp_path, monkeypatch):
    """The positive control: the harness sees a full sync's writes."""
    root, client, backend = _setup(tmp_path, monkeypatch)
    before = _snapshot(root)

    result = CliRunner().invoke(main, ["render"])

    assert result.exit_code == 0, result.output
    assert backend.reads == 1
    assert _snapshot(root) != before
    assert client.writes("commitments"), "the stale ask should have expired"


def test_render_check_writes_no_file_and_no_mc2_row(tmp_path, monkeypatch):
    import cp_engine.word_count_lint as wcl

    root, client, backend = _setup(tmp_path, monkeypatch)
    # A synced tree first, so the advisory passes have real files to read.
    assert CliRunner().invoke(main, ["render"]).exit_code == 0
    client.store["commitments"].append(_stale_ask(2))
    client.calls.clear()
    backend.reads = 0
    before = _snapshot(root)

    real = wcl.word_count_warnings
    monkeypatch.setattr(wcl, "word_count_warnings",
                        lambda r: [*real(r), "WORD-COUNT-SENTINEL"])
    result = CliRunner().invoke(main, ["render", "--check"])

    assert result.exit_code == 0, result.output
    assert "WORD-COUNT-SENTINEL" in result.stderr, "the warnings must still print"
    assert "Running sync" not in result.output
    assert _snapshot(root) == before, "no tenant file may change"
    assert client.calls == [], "no MC-2 call, read or write"
    assert backend.reads == 0
    assert {r["id"]: r["status"] for r in client.store["commitments"]}["c2"] == "open"


def test_dry_run_sync_writes_no_file_and_no_mc2_row(tmp_path, monkeypatch):
    """`cxp status`'s dry run: it rendered `exceptions/README.md` for real
    (the one `_write_if_changed` call that did not pass `dry_run`)."""
    from cp_engine import config as cfg_mod
    from cp_engine.sync import sync_tenant

    root, client, backend = _setup(tmp_path, monkeypatch)
    assert CliRunner().invoke(main, ["render"]).exit_code == 0
    (root / "exceptions").mkdir(exist_ok=True)
    (root / "exceptions" / "README.md").unlink(missing_ok=True)
    client.store["commitments"].append(_stale_ask(2))
    client.calls.clear()
    before = _snapshot(root)

    sync_tenant(cfg_mod.load(root), dry_run=True)

    assert _snapshot(root) == before
    assert client.writes() == []
