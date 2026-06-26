"""Tests for the ingest-cache SKIP wiring in ingest_project_assets (item 5, Task 4).

Task 3 added `_unchanged_since_last_ingest(client, project_id, file_ref) -> bool`
(provider-scoped, fail-open). This task WIRES it into the ingest loop: a listed
file whose `change_token` matches the active rag_assets row's `meta.change_token`
is skipped BEFORE download/hash/embed (no network, no parse), counted as
`skipped_unchanged`. A `use_cache=False` caller (CLI `--no-cache`) bypasses the
check entirely → full re-scan.

These drive the REAL `ingest_project_assets` entry with injected fakes (mirroring
test_asset_ingest_run.py) so the loop wiring is genuinely exercised end-to-end.
"""

from __future__ import annotations

from pathlib import Path

import cp_engine.asset_ingest as asset_ingest
import cp_engine.asset_ingest_cli as asset_ingest_cli
import cp_engine.cli as cli_mod
from click.testing import CliRunner

from cp_engine.asset_ingest import (
    FileRef,
    IngestRunResult,
    ProjectFolders,
    ingest_project_assets,
)
from cp_engine.cli import main


# ──────────────────────────────────────────────────────────────────────
#  Fakes
# ──────────────────────────────────────────────────────────────────────


class _FakeIngestResult:
    def __init__(self, action, *, file_path=""):
        self.success = True
        self.file_path = file_path
        self.action = action
        self.error = None
        self.chunks_processed = 0
        self.chunks_embedded = 0
        self.chunks_reused = 0
        self.warnings = []


class _FakePipeline:
    """Records ingest_file calls; returns 'created' for any file handed to it."""

    def __init__(self):
        self.calls = []

    def ingest_file(self, file_path, title, url=None):
        self.calls.append({"file_path": file_path, "title": title, "url": url})
        return _FakeIngestResult("created", file_path=file_path)


class _Resp:
    def __init__(self, data):
        self.data = data


class _SkipCheckSelectChain:
    """Captures a .select(...).eq(...)....limit(1).execute() read chain.

    Returns the active rag_assets rows seeded for the matching
    (project_id, source_provider, source_file_id) so _unchanged_since_last_ingest
    can compare meta.change_token. Honours status='active'.
    """

    def __init__(self, rows_by_file_id, selects_seen):
        self._rows_by_file_id = rows_by_file_id
        self._selects_seen = selects_seen
        self._filters = {}

    def select(self, cols):
        self._cols = cols
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        self._selects_seen.append(dict(self._filters))
        rows = self._rows_by_file_id.get(self._filters.get("source_file_id"), [])
        if self._filters.get("status") == "active":
            rows = [r for r in rows if r.get("status", "active") == "active"]
        return _Resp(list(rows))


class _UpdateChain:
    def __init__(self, recorder):
        self._recorder = recorder
        self._payload = None
        self._filters = {}

    def update(self, payload):
        self._payload = payload
        return self

    def select(self, cols):
        # The stamp's read-modify-write of meta uses select too; route to a
        # benign empty read so stamping never blows up in these tests.
        return _SkipCheckSelectChain({}, []).select(cols)

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def execute(self):
        self._recorder.append({"payload": self._payload, "filters": dict(self._filters)})
        return None


class _FakeTable:
    def __init__(self, recorder, rows_by_file_id, selects_seen):
        self._recorder = recorder
        self._rows_by_file_id = rows_by_file_id
        self._selects_seen = selects_seen

    def update(self, payload):
        return _UpdateChain(self._recorder).update(payload)

    def select(self, cols):
        return _SkipCheckSelectChain(
            self._rows_by_file_id, self._selects_seen
        ).select(cols)


class _FakeClient:
    """Fake Supabase client. `active_by_file_id` seeds the skip-check:
    source_file_id -> list of active rows (each a dict that may carry `meta`)."""

    def __init__(self, active_by_file_id=None):
        self.updates = []
        self.selects = []
        self._active = active_by_file_id or {}

    def table(self, name):
        assert name == "rag_assets", f"unexpected table {name!r}"
        return _FakeTable(self.updates, self._active, self.selects)


_FOLDERS = ProjectFolders(
    project_id="proj-1",
    company_id="co-9",
    company_kind="client",
    google_drive_folder_id=None,
    mc_dropbox_folder_id="/Clients/Acme",
    enable_google_drive=False,
    enable_dropbox=True,
)


def _ref(name: str, *, token: str | None) -> FileRef:
    return FileRef(
        source="dropbox",
        id=f"id:{name}",
        name=name,
        mime_type=None,
        size=10,
        modified="2026-06-13T00:00:00Z",
        path=f"/Clients/Acme/{name}",
        change_token=token,
    )


def _patch_resolve_and_list(monkeypatch, files):
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders",
        lambda client, code: _FOLDERS,
    )
    monkeypatch.setattr(
        "cp_engine.asset_ingest.list_files",
        lambda f, drive_connector=None, dropbox_connector=None, allowlist=(), **_kw: (
            list(files),
            [],
        ),
    )


def _patch_download_records(monkeypatch, downloaded):
    def _fake_download(file_ref, tmp_dir, drive_connector=None, dropbox_connector=None):
        downloaded.append(file_ref.name)
        p = Path(tmp_dir) / file_ref.name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"content-of-" + file_ref.name.encode())
        return p

    monkeypatch.setattr("cp_engine.asset_ingest.download_file", _fake_download)


# ──────────────────────────────────────────────────────────────────────
#  Skip wiring tests
# ──────────────────────────────────────────────────────────────────────


def test_unchanged_file_is_skipped_no_download_no_ingest(monkeypatch, tmp_path):
    """A listed file whose change_token matches the active row → skipped:
    counted as skipped_unchanged, NO download, NO pipeline.ingest_file."""
    _patch_resolve_and_list(monkeypatch, [_ref("a.docx", token="tok-abc")])
    downloaded: list[str] = []
    _patch_download_records(monkeypatch, downloaded)
    pipeline = _FakePipeline()
    client = _FakeClient(
        active_by_file_id={
            "id:a.docx": [
                {"file_path": "/p", "status": "active",
                 "meta": {"change_token": "tok-abc"}}
            ]
        }
    )

    result = ingest_project_assets(
        "acme-1", client=client, pipeline=pipeline, tmp_root=tmp_path
    )

    assert isinstance(result, IngestRunResult)
    assert result.skipped_unchanged == 1
    assert result.created == 0
    # NO download, NO embed for the skipped file.
    assert downloaded == []
    assert pipeline.calls == []
    # nothing stamped (the file never ingested)
    assert client.updates == []


def test_changed_file_is_not_skipped(monkeypatch, tmp_path):
    """A token MISMATCH → not skipped: downloaded + ingested normally,
    skipped_unchanged stays 0."""
    _patch_resolve_and_list(monkeypatch, [_ref("a.docx", token="tok-NEW")])
    downloaded: list[str] = []
    _patch_download_records(monkeypatch, downloaded)
    pipeline = _FakePipeline()
    client = _FakeClient(
        active_by_file_id={
            "id:a.docx": [
                {"file_path": "/p", "status": "active",
                 "meta": {"change_token": "tok-OLD"}}
            ]
        }
    )

    result = ingest_project_assets(
        "acme-1", client=client, pipeline=pipeline, tmp_root=tmp_path
    )

    assert result.skipped_unchanged == 0
    assert result.created == 1
    assert downloaded == ["a.docx"]
    assert [c["title"] for c in pipeline.calls] == ["a.docx"]


def test_use_cache_false_never_skips(monkeypatch, tmp_path):
    """use_cache=False → the skip-check is bypassed entirely: even a MATCHING
    token is downloaded + ingested (full re-scan)."""
    _patch_resolve_and_list(monkeypatch, [_ref("a.docx", token="tok-abc")])
    downloaded: list[str] = []
    _patch_download_records(monkeypatch, downloaded)
    pipeline = _FakePipeline()
    client = _FakeClient(
        active_by_file_id={
            "id:a.docx": [
                {"file_path": "/p", "status": "active",
                 "meta": {"change_token": "tok-abc"}}
            ]
        }
    )

    result = ingest_project_assets(
        "acme-1",
        client=client,
        pipeline=pipeline,
        tmp_root=tmp_path,
        use_cache=False,
    )

    assert result.skipped_unchanged == 0
    assert result.created == 1
    assert downloaded == ["a.docx"]
    assert [c["title"] for c in pipeline.calls] == ["a.docx"]


# ──────────────────────────────────────────────────────────────────────
#  CLI --no-cache flag
# ──────────────────────────────────────────────────────────────────────


def test_cli_no_cache_passes_use_cache_false(monkeypatch):
    """`cp ingest-assets <code> --no-cache` must pass use_cache=False through."""
    monkeypatch.setattr(cli_mod, "build_mc2_client", lambda: object())

    seen = {}

    def fake_ingest(code, **kwargs):
        seen["code"] = code
        seen["use_cache"] = kwargs.get("use_cache")
        return IngestRunResult(created=1)

    monkeypatch.setattr(asset_ingest, "ingest_project_assets", fake_ingest)

    result = CliRunner().invoke(main, ["ingest-assets", "ibx-5153", "--no-cache"])
    assert result.exit_code == 0, result.output
    assert seen["use_cache"] is False


def test_cli_default_uses_cache(monkeypatch):
    """Without --no-cache the CLI leaves caching ON (use_cache True or unset)."""
    monkeypatch.setattr(cli_mod, "build_mc2_client", lambda: object())

    seen = {}

    def fake_ingest(code, **kwargs):
        seen["use_cache"] = kwargs.get("use_cache", True)
        return IngestRunResult(created=1)

    monkeypatch.setattr(asset_ingest, "ingest_project_assets", fake_ingest)

    result = CliRunner().invoke(main, ["ingest-assets", "ibx-5153"])
    assert result.exit_code == 0, result.output
    assert seen["use_cache"] is True


# ──────────────────────────────────────────────────────────────────────
#  Observability: the skip count is actually SHOWN to the user
# ──────────────────────────────────────────────────────────────────────


def test_cli_single_shows_unchanged_count(monkeypatch):
    """`cp ingest-assets <code>` must print `unchanged=N` so a cache-skipped run
    isn't invisible (the headline feature would otherwise look like a no-op)."""
    monkeypatch.setattr(cli_mod, "build_mc2_client", lambda: object())

    def fake_ingest(code, **kwargs):
        return IngestRunResult(created=0, skipped_unchanged=3)

    monkeypatch.setattr(asset_ingest, "ingest_project_assets", fake_ingest)

    result = CliRunner().invoke(main, ["ingest-assets", "ibx-5153"])
    assert result.exit_code == 0, result.output
    assert "unchanged=3" in result.output


def test_cli_all_shows_unchanged_total(monkeypatch):
    """`cp ingest-assets --all` must surface per-project + aggregate `unchanged=`."""
    monkeypatch.setattr(cli_mod, "build_mc2_client", lambda: object())
    monkeypatch.setattr(cli_mod, "_load_config_or_die", lambda: object())
    monkeypatch.setattr(
        asset_ingest_cli,
        "active_ingestable_codes",
        lambda config: ["a-1", "b-2"],
    )

    def fake_ingest(code, **kwargs):
        return IngestRunResult(created=0, skipped_unchanged=2)

    monkeypatch.setattr(asset_ingest, "ingest_project_assets", fake_ingest)

    result = CliRunner().invoke(main, ["ingest-assets", "--all"])
    assert result.exit_code == 0, result.output
    # per-project lines each show unchanged=2; aggregate rolls up to 4
    assert "unchanged=2" in result.output
    assert "unchanged=4" in result.output
