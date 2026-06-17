"""Integration test for the `_sources.md` manifest folded into `cp sync`.

The per-project block resolves the project's company via
`resolve_project_folders_by_id` then calls `write_sources_manifest`, both
best-effort: a failure must NEVER abort sync. We drive `sync_tenant` with the
spine-integration fake backend and monkeypatch the two functions at their
sync-import sites (`cp_engine.asset_ingest` / `cp_engine.project_sources`).
"""

from __future__ import annotations

from pathlib import Path

import cp_engine.asset_ingest as asset_ingest
import cp_engine.project_sources as project_sources
from cp_engine import sync_tenant

from tests.test_spine_sync import _FakeClient
from tests.test_sync import make_config
from tests.test_sync_spine_integration import _make_engagement, _SpineBackend


def _stub_folders():
    return asset_ingest.ProjectFolders(
        project_id="rag-proj-uuid",
        company_id="co-uuid",
        company_kind="client",
        google_drive_folder_id=None,
        mc_dropbox_folder_id=None,
        enable_google_drive=False,
        enable_dropbox=False,
    )


def test_sync_writes_sources_manifest(tmp_path: Path, monkeypatch) -> None:
    config = make_config(tmp_path)
    code = "ggl-5168"
    proj_dir = tmp_path / "1p/google" / code

    seen: dict = {}

    def fake_resolve(client, mc_project_id):
        seen["resolve_id"] = mc_project_id
        return _stub_folders()

    def fake_write(client, project_dir, project_id, company_id, *a, **k):
        seen["project_dir"] = Path(project_dir)
        seen["project_id"] = project_id
        seen["company_id"] = company_id
        Path(project_dir).mkdir(parents=True, exist_ok=True)
        (Path(project_dir) / "_sources.md").write_text("stub\n")
        return 1

    monkeypatch.setattr(asset_ingest, "resolve_project_folders_by_id", fake_resolve)
    monkeypatch.setattr(project_sources, "write_sources_manifest", fake_write)

    state = _make_engagement(code, mc2_id="uuid-ggl-5168")
    backend = _SpineBackend((state,), _FakeClient())
    sync_tenant(config, backend_factory=lambda _: backend)

    # Wiring reached write_sources_manifest with the resolved ids + project dir.
    assert seen["resolve_id"] == "uuid-ggl-5168"
    assert seen["project_id"] == "rag-proj-uuid"
    assert seen["company_id"] == "co-uuid"
    assert seen["project_dir"] == proj_dir
    assert (proj_dir / "_sources.md").exists()


def test_sync_swallows_sources_manifest_failure(tmp_path: Path, monkeypatch) -> None:
    """A manifest exception is best-effort: sync completes and does not raise."""
    config = make_config(tmp_path)
    code = "ggl-5168"

    monkeypatch.setattr(
        asset_ingest, "resolve_project_folders_by_id",
        lambda client, mc_project_id: _stub_folders(),
    )

    def boom(*a, **k):
        raise RuntimeError("manifest exploded")

    monkeypatch.setattr(project_sources, "write_sources_manifest", boom)

    state = _make_engagement(code, mc2_id="uuid-ggl-5168")
    backend = _SpineBackend((state,), _FakeClient())

    # Must NOT raise — the best-effort try/except swallows it.
    sync_tenant(config, backend_factory=lambda _: backend)

    # No manifest written, but sync still produced the project dir / cp files.
    proj_dir = tmp_path / "1p/google" / code
    assert not (proj_dir / "_sources.md").exists()
    assert proj_dir.exists()
