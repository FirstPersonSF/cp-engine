"""Task C7 — dedup-scope interaction regression test (spec §3.3).

EXECUTABLE DOCUMENTATION of the (project_id, file_path) dedup-key semantics.

The document-ingest component dedups on `(project_id, file_path)`. So the SAME
source file ingested under TWO DIFFERENT projects produces TWO project rows —
NOT a false skip — because `project_id` differs and the dedup query won't match.
This is CORRECT: two projects each referencing the same client file is fine; the
account-promoted copy is the shared one. The same file under the SAME project a
second time IS a skip (the dedup key matches).

This test guards against someone "fixing" the two-row outcome into a cross-
project dedup and calling it a bug. The real dedup lives in the component + DB,
so we exercise it at the cp glue level with a fake pipeline that mimics the
component's contract: it keys its "already seen" set on (project_id, file_path).
A file under a new project_id is therefore NOT seen → 'created' again (the right
answer), while a repeat under the same project_id → 'skipped'.
"""

from __future__ import annotations

from pathlib import Path

from cp_engine.asset_ingest import FileRef, ProjectFolders, ingest_project_assets


class _FakeIngestResult:
    """Stand-in for IngestResult (only the fields the glue reads)."""

    def __init__(self, action, *, file_path=""):
        self.success = action != "failed"
        self.file_path = file_path
        self.action = action
        self.error = None


class _DedupKeyedPipeline:
    """Fake pipeline that mimics the component's (project_id, file_path) dedup.

    The pipeline is constructed per-project (the real `IngestPipeline` is bound to
    a `project_id`), so we pass project_id in and key the seen-set on
    (project_id, file_path). First sighting of a key → 'created'; repeat → 'skipped'.
    The seen-set is SHARED across pipelines (stands in for the single backing DB)
    so a second project's pipeline can observe that the SAME file_path under a
    DIFFERENT project_id has not been seen for THAT project.
    """

    def __init__(self, project_id, seen):
        self.project_id = project_id
        self._seen = seen  # shared set of (project_id, file_path) keys

    def ingest_file(self, file_path, title, url=None):
        key = (self.project_id, file_path)
        if key in self._seen:
            return _FakeIngestResult("skipped", file_path=file_path)
        self._seen.add(key)
        return _FakeIngestResult("created", file_path=file_path)


def _folders(project_id):
    return ProjectFolders(
        project_id=project_id,
        company_id="co-shared",  # SAME company — both projects belong to it
        company_kind="client",
        google_drive_folder_id=None,
        mc_dropbox_folder_id="/Clients/Acme",
        enable_google_drive=False,
        enable_dropbox=True,
    )


def _ref(name):
    return FileRef(
        source="dropbox",
        id=f"id:{name}",
        name=name,
        mime_type=None,
        size=10,
        modified="2026-06-13T00:00:00Z",
        path=f"/Clients/Acme/{name}",
    )


class _NoopStampClient:
    """Client whose scope-stamp UPDATE is a no-op (dedup semantics are the focus)."""

    class _Chain:
        def update(self, payload):
            return self

        def eq(self, col, val):
            return self

        def execute(self):
            return None

    def table(self, name):
        return self._Chain()


def _patch(monkeypatch, folders, files):
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders",
        lambda client, code: folders,
    )
    monkeypatch.setattr(
        "cp_engine.asset_ingest.list_files",
        lambda f, drive_connector=None, dropbox_connector=None: list(files),
    )

    def _fake_download(file_ref, tmp_dir, drive_connector=None, dropbox_connector=None):
        # Stable per-file path (the real glue derives a stable dedup-key path too);
        # the SAME source file yields the SAME relative name under any project.
        p = Path(tmp_dir) / file_ref.name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"same-bytes")
        return p

    monkeypatch.setattr("cp_engine.asset_ingest.download_file", _fake_download)


def test_same_file_two_projects_yields_two_creates(monkeypatch, tmp_path):
    """SAME source file under project A and project B → two creates, not a skip."""
    seen = set()  # the shared backing "DB" of (project_id, file_path) keys
    file_name = "brief.docx"

    # Project A — first time this file is seen anywhere for project A.
    _patch(monkeypatch, _folders("proj-A"), [_ref(file_name)])
    pipe_a = _DedupKeyedPipeline("proj-A", seen)
    res_a = ingest_project_assets(
        "acme-1",
        client=_NoopStampClient(),
        pipeline=pipe_a,
        tmp_root=tmp_path / "A",
    )
    assert res_a.created == 1
    assert res_a.skipped == 0

    # Project B — SAME file_path, DIFFERENT project_id. The (project_id, file_path)
    # dedup key differs → NOT seen → created again. This is the spec §3.3 contract:
    # two projects referencing the same client file = two rows, not a false skip.
    _patch(monkeypatch, _folders("proj-B"), [_ref(file_name)])
    pipe_b = _DedupKeyedPipeline("proj-B", seen)
    res_b = ingest_project_assets(
        "beta-2",
        client=_NoopStampClient(),
        pipeline=pipe_b,
        tmp_root=tmp_path / "B",
    )
    assert res_b.created == 1, "same file under a different project must NOT skip"
    assert res_b.skipped == 0

    # Two distinct dedup keys now exist (one per project) — same file_path basename
    # under two different project_ids = two conceptual rows, the spec §3.3 outcome.
    project_ids = {pid for (pid, _fp) in seen}
    assert project_ids == {"proj-A", "proj-B"}
    file_paths = {Path(fp).name for (_pid, fp) in seen}
    assert file_paths == {file_name}  # the SAME source file under both


def test_same_file_same_project_twice_yields_skip(monkeypatch, tmp_path):
    """SAME file under the SAME project twice → created then skipped."""
    seen = set()
    file_name = "brief.docx"

    _patch(monkeypatch, _folders("proj-A"), [_ref(file_name)])

    # Same tmp_root across both runs: the real glue derives a STABLE download path
    # (the dedup key) from the source id, so a re-run lands at the identical
    # file_path. Reusing tmp_root here reproduces that stable-path reality.
    run_root = tmp_path / "stable"

    pipe_first = _DedupKeyedPipeline("proj-A", seen)
    res_first = ingest_project_assets(
        "acme-1",
        client=_NoopStampClient(),
        pipeline=pipe_first,
        tmp_root=run_root,
    )
    assert res_first.created == 1
    assert res_first.skipped == 0

    # Second run, same project, same file → dedup key matches → skip.
    pipe_second = _DedupKeyedPipeline("proj-A", seen)
    res_second = ingest_project_assets(
        "acme-1",
        client=_NoopStampClient(),
        pipeline=pipe_second,
        tmp_root=run_root,
    )
    assert res_second.created == 0
    assert res_second.skipped == 1, "same file, same project, second run must skip"
