"""Tests for asset_ingest.ingest_project_assets (Task C5 — the ingest run loop).

Everything external is injected/mocked: a fake MC-2 client (records the scope-
stamp UPDATE), a fake pipeline (returns canned IngestResults), fake connectors,
and a stubbed download. No real Supabase / OpenAI / Voyage is touched.

The behaviors under test:

  - After a 'created' (or 'versioned') result, the glue runs the post-insert
    scope stamp: rag_assets UPDATE {scope:'project', company_id} filtered by
    (project_id, file_path, status='active'). We stamp by the dedup key because
    IngestResult carries no asset_id and the migration's unique index on
    (project_id, file_path) WHERE status='active' makes that filter hit exactly
    the just-written row.
  - 'skipped' results are NOT stamped (the row was stamped on a prior run).
  - Failures (a 'failed' result OR a raising download) are collected with their
    (file_name, error) and do NOT abort the loop — every other file still runs.
  - An empty file list short-circuits: the pipeline is never constructed.
  - A missing project resolves to a zero-count result that notes "project not
    found" (no raise).
  - Temp files are cleaned up after the run (bytes never persisted).
"""

from __future__ import annotations

from pathlib import Path

from cp_engine.asset_ingest import (
    FileRef,
    IngestRunResult,
    ProjectFolders,
    ingest_project_assets,
)


# ──────────────────────────────────────────────────────────────────────
#  Fakes
# ──────────────────────────────────────────────────────────────────────


class _FakeIngestResult:
    """Stand-in for ingest.pipeline.IngestResult (only the fields we read)."""

    def __init__(self, action, *, success=True, error=None, file_path="", chunks=0):
        self.success = success
        self.file_path = file_path
        self.action = action
        self.error = error
        self.chunks_processed = chunks
        self.chunks_embedded = chunks
        self.chunks_reused = 0
        self.warnings = []


class _FakePipeline:
    """Records ingest_file calls and returns a canned result per call.

    `results_by_name` maps file basename → action string (or a callable raising
    to simulate a parse blowup). Records (file_path, title, url) per call so the
    test can assert the file_path the glue passed IS the stable dedup key.
    """

    def __init__(self, results_by_name):
        self._results = results_by_name
        self.calls = []  # list of dicts: {file_path, title, url}

    def ingest_file(self, file_path, title, url=None):
        self.calls.append({"file_path": file_path, "title": title, "url": url})
        action = self._results[Path(file_path).name]
        if callable(action):
            action()  # may raise — but the real pipeline catches; we mirror that
        return _FakeIngestResult(action, file_path=file_path)


class _FakeUpdateChain:
    """Captures a .update(...).eq(...).eq(...)...execute() chain."""

    def __init__(self, recorder):
        self._recorder = recorder
        self._payload = None
        self._filters = {}

    def update(self, payload):
        self._payload = payload
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def execute(self):
        self._recorder.append({"payload": self._payload, "filters": dict(self._filters)})
        return None


class _FakeTable:
    def __init__(self, recorder):
        self._recorder = recorder

    def update(self, payload):
        return _FakeUpdateChain(self._recorder).update(payload)


class _FakeClient:
    """Fake Supabase client; only `.table('rag_assets').update(...)` is exercised."""

    def __init__(self):
        self.updates = []  # each: {payload, filters}

    def table(self, name):
        assert name == "rag_assets", f"unexpected table {name!r}"
        return _FakeTable(self.updates)


class _RaisingOnceUpdateChain:
    """An update chain whose .execute() raises the first time, succeeds after."""

    def __init__(self, client):
        self._client = client
        self._payload = None
        self._filters = {}

    def update(self, payload):
        self._payload = payload
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def execute(self):
        self._client.stamp_attempts += 1
        if self._client.stamp_attempts == 1:
            raise RuntimeError("transient PostgREST stamp error")
        self._client.updates.append(
            {"payload": self._payload, "filters": dict(self._filters)}
        )
        return None


class _StampRaisesOnceClient:
    """Fake client whose first stamp UPDATE raises, later ones succeed."""

    def __init__(self):
        self.updates = []  # successful stamps only
        self.stamp_attempts = 0  # total UPDATE execute() calls (incl. the raise)

    def table(self, name):
        assert name == "rag_assets", f"unexpected table {name!r}"
        return _RaisingOnceUpdateChain(self)


# ──────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────


_FOLDERS = ProjectFolders(
    project_id="proj-1",
    company_id="co-9",
    company_kind="client",
    google_drive_folder_id=None,
    mc_dropbox_folder_id="/Clients/Acme",
    enable_google_drive=False,
    enable_dropbox=True,
)


def _ref(name: str) -> FileRef:
    return FileRef(
        source="dropbox",
        id=f"id:{name}",
        name=name,
        mime_type=None,
        size=10,
        modified="2026-06-13T00:00:00Z",
        path=f"/Clients/Acme/{name}",
    )


def _patch_resolve_and_list(monkeypatch, folders, files):
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders",
        lambda client, code: folders,
    )
    monkeypatch.setattr(
        "cp_engine.asset_ingest.list_files",
        lambda f, drive_connector=None, dropbox_connector=None: list(files),
    )


def _patch_download_writes(monkeypatch):
    """download_file writes a real byte file at tmp_dir/<name> and returns it.

    This lets the temp-cleanup test observe real files appearing then vanishing,
    and makes the stable-path assertion meaningful (the returned path is what the
    glue feeds to ingest_file / the stamp).
    """

    def _fake_download(file_ref, tmp_dir, drive_connector=None, dropbox_connector=None):
        p = Path(tmp_dir) / file_ref.name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"content-of-" + file_ref.name.encode())
        return p

    monkeypatch.setattr("cp_engine.asset_ingest.download_file", _fake_download)


# ──────────────────────────────────────────────────────────────────────
#  Tests
# ──────────────────────────────────────────────────────────────────────


def test_run_stamps_scope_after_created(monkeypatch, tmp_path):
    _patch_resolve_and_list(monkeypatch, _FOLDERS, [_ref("a.docx")])
    _patch_download_writes(monkeypatch)
    pipeline = _FakePipeline({"a.docx": "created"})
    client = _FakeClient()

    result = ingest_project_assets(
        "acme-1",
        client=client,
        pipeline=pipeline,
        tmp_root=tmp_path,
    )

    assert isinstance(result, IngestRunResult)
    assert result.created == 1
    # exactly one stamp UPDATE ran
    assert len(client.updates) == 1
    stamp = client.updates[0]
    assert stamp["payload"]["scope"] == "project"
    assert stamp["payload"]["company_id"] == "co-9"
    # stamped by the dedup key: (project_id, file_path, status='active')
    assert stamp["filters"]["project_id"] == "proj-1"
    assert stamp["filters"]["status"] == "active"
    # the file_path stamped MUST equal the file_path the pipeline ingested
    ingested_path = pipeline.calls[0]["file_path"]
    assert stamp["filters"]["file_path"] == ingested_path
    # title carried the file name; dropbox url left None (no public url)
    assert pipeline.calls[0]["title"] == "a.docx"


def test_run_collects_failures_without_aborting(monkeypatch, tmp_path):
    files = [_ref("first.docx"), _ref("boom.docx"), _ref("third.docx")]
    _patch_resolve_and_list(monkeypatch, _FOLDERS, files)
    _patch_download_writes(monkeypatch)
    pipeline = _FakePipeline(
        {"first.docx": "created", "boom.docx": "failed", "third.docx": "created"}
    )
    client = _FakeClient()

    result = ingest_project_assets(
        "acme-1", client=client, pipeline=pipeline, tmp_root=tmp_path
    )

    # the loop did not abort: both good files ran
    assert result.created == 2
    assert result.failed == 1
    assert len(result.failures) == 1
    name, _err = result.failures[0]
    assert name == "boom.docx"
    # the failed file was NOT stamped; both created ones were
    assert len(client.updates) == 2


def test_run_stamp_failure_does_not_abort(monkeypatch, tmp_path):
    """A raising scope-stamp UPDATE on the first file must NOT abort the run.

    The asset WAS ingested ('created'); only the post-insert stamp blew up. The
    run must still return an IngestRunResult, process the second file, and surface
    the stamp failure in `failures` (not silently swallow it).
    """
    files = [_ref("first.docx"), _ref("second.docx")]
    _patch_resolve_and_list(monkeypatch, _FOLDERS, files)
    _patch_download_writes(monkeypatch)
    pipeline = _FakePipeline({"first.docx": "created", "second.docx": "created"})
    client = _StampRaisesOnceClient()  # first stamp raises, second succeeds

    # Must NOT propagate — the call returns normally.
    result = ingest_project_assets(
        "acme-1", client=client, pipeline=pipeline, tmp_root=tmp_path
    )

    assert isinstance(result, IngestRunResult)
    # both files were ingested by the pipeline (the loop didn't abort)
    assert len(pipeline.calls) == 2
    # the second file's stamp succeeded
    assert client.stamp_attempts == 2
    assert len(client.updates) == 1
    # the first file's stamp failure was surfaced, not swallowed
    names = [n for n, _ in result.failures]
    assert "first.docx" in names
    first_err = next(err for n, err in result.failures if n == "first.docx")
    assert "scope-stamp failed" in first_err


def test_run_ingest_file_raising_is_collected(monkeypatch, tmp_path):
    """A raising pipeline.ingest_file for one file is collected, not propagated."""
    files = [_ref("first.docx"), _ref("boom.docx"), _ref("third.docx")]
    _patch_resolve_and_list(monkeypatch, _FOLDERS, files)
    _patch_download_writes(monkeypatch)

    def _raise():
        raise RuntimeError("parser exploded")

    pipeline = _FakePipeline(
        {"first.docx": "created", "boom.docx": _raise, "third.docx": "created"}
    )
    client = _FakeClient()

    # Must NOT propagate.
    result = ingest_project_assets(
        "acme-1", client=client, pipeline=pipeline, tmp_root=tmp_path
    )

    assert isinstance(result, IngestRunResult)
    # the two good files still ran (no abort)
    assert result.created == 2
    assert len(client.updates) == 2
    # the raising file is recorded as a failure with its error text
    names = [n for n, _ in result.failures]
    assert "boom.docx" in names
    boom_err = next(err for n, err in result.failures if n == "boom.docx")
    assert "parser exploded" in boom_err


def test_run_download_error_collected_without_aborting(monkeypatch, tmp_path):
    files = [_ref("first.docx"), _ref("raise.docx"), _ref("third.docx")]
    _patch_resolve_and_list(monkeypatch, _FOLDERS, files)

    def _fake_download(file_ref, tmp_dir, drive_connector=None, dropbox_connector=None):
        if file_ref.name == "raise.docx":
            raise RuntimeError("network blip")
        p = Path(tmp_dir) / file_ref.name
        p.write_bytes(b"x")
        return p

    monkeypatch.setattr("cp_engine.asset_ingest.download_file", _fake_download)
    pipeline = _FakePipeline({"first.docx": "created", "third.docx": "created"})
    client = _FakeClient()

    result = ingest_project_assets(
        "acme-1", client=client, pipeline=pipeline, tmp_root=tmp_path
    )

    assert result.created == 2
    assert result.failed == 1
    assert result.failures[0][0] == "raise.docx"
    assert "network blip" in result.failures[0][1]


def test_run_skipped_not_stamped(monkeypatch, tmp_path):
    _patch_resolve_and_list(monkeypatch, _FOLDERS, [_ref("a.docx")])
    _patch_download_writes(monkeypatch)
    pipeline = _FakePipeline({"a.docx": "skipped"})
    client = _FakeClient()

    result = ingest_project_assets(
        "acme-1", client=client, pipeline=pipeline, tmp_root=tmp_path
    )

    assert result.skipped == 1
    assert result.created == 0
    # no stamp UPDATE for a skipped row (it was stamped on a prior run)
    assert client.updates == []


def test_run_versioned_is_stamped(monkeypatch, tmp_path):
    _patch_resolve_and_list(monkeypatch, _FOLDERS, [_ref("a.docx")])
    _patch_download_writes(monkeypatch)
    pipeline = _FakePipeline({"a.docx": "versioned"})
    client = _FakeClient()

    result = ingest_project_assets(
        "acme-1", client=client, pipeline=pipeline, tmp_root=tmp_path
    )

    assert result.versioned == 1
    assert len(client.updates) == 1


def test_run_empty_file_list_returns_zero_counts(monkeypatch, tmp_path):
    _patch_resolve_and_list(monkeypatch, _FOLDERS, [])

    # A pipeline that explodes if touched — proves it is never constructed/called.
    def _boom_factory(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("pipeline must not be built for an empty file list")

    result = ingest_project_assets(
        "acme-1",
        client=object(),
        pipeline_factory=_boom_factory,
        tmp_root=tmp_path,
    )

    assert isinstance(result, IngestRunResult)
    assert (result.created, result.versioned, result.skipped, result.failed) == (0, 0, 0, 0)
    assert result.failures == []


def test_run_project_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders",
        lambda client, code: None,
    )

    result = ingest_project_assets("nope-404", client=object(), tmp_root=tmp_path)

    assert isinstance(result, IngestRunResult)
    assert (result.created, result.versioned, result.skipped, result.failed) == (0, 0, 0, 0)
    assert result.project_found is False


def test_run_cleans_temp_files(monkeypatch, tmp_path):
    files = [_ref("a.docx"), _ref("b.docx")]
    _patch_resolve_and_list(monkeypatch, _FOLDERS, files)

    written = []

    def _fake_download(file_ref, tmp_dir, drive_connector=None, dropbox_connector=None):
        p = Path(tmp_dir) / file_ref.name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"bytes")
        written.append(p)
        return p

    monkeypatch.setattr("cp_engine.asset_ingest.download_file", _fake_download)
    pipeline = _FakePipeline({"a.docx": "created", "b.docx": "created"})
    client = _FakeClient()

    work_root = tmp_path / "work"
    result = ingest_project_assets(
        "acme-1", client=client, pipeline=pipeline, tmp_root=work_root
    )

    assert result.created == 2
    # every downloaded temp file is gone after the run — bytes never persisted
    assert written, "download stub should have been called"
    for p in written:
        assert not p.exists(), f"temp file {p} was not cleaned up"
    # and no stray temp dirs left under the work root
    leftovers = list(work_root.rglob("*")) if work_root.exists() else []
    leftover_files = [p for p in leftovers if p.is_file()]
    assert leftover_files == [], f"unexpected leftover files: {leftover_files}"
