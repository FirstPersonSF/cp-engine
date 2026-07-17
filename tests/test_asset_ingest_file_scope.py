"""Tests for the file-scoped ingest path (the picker's 'ingest exactly these').

Two units, both pure / mock-only:
  - `file_selection_key(ref)` — Dropbox → path, Drive → id (the picker's key).
  - `ingest_project_assets(..., only_file_ids=...)` — narrows the listed files
    to the selected keys AFTER list_files, provably a subset (never widens).

The narrowing tests stub `list_files` to canned files and intercept
`download_file` to record which files reached the per-file loop — that set IS
the post-filter survivors.
"""

from __future__ import annotations

from cp_engine.asset_ingest import (
    FileRef,
    IngestRunResult,
    ProjectFolders,
    _folder_segments,
    file_selection_key,
    ingest_project_assets,
)


def _drive(id_: str, name: str) -> FileRef:
    return FileRef(
        source="drive", id=id_, name=name, mime_type="application/pdf",
        size=100, modified="2026-07-17",
    )


def _dropbox(path: str) -> FileRef:
    return FileRef(
        source="dropbox", id="id:xyz", name=path.rsplit("/", 1)[-1],
        mime_type=None, size=100, modified="2026-07-17", path=path,
    )


def _client_folders() -> ProjectFolders:
    return ProjectFolders(
        project_id="p", company_id="c", company_kind="client",
        google_drive_folder_id="drive-root", mc_dropbox_folder_id="/proj",
        enable_google_drive=True, enable_dropbox=True,
    )


def _stub_list(monkeypatch, files):
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders",
        lambda client, code: _client_folders(),
    )
    monkeypatch.setattr(
        "cp_engine.asset_ingest.list_files",
        lambda f, drive_connector=None, dropbox_connector=None, allowlist=(),
        **_kw: (list(files), []),
    )


def _record_reaching_download(monkeypatch, seen):
    """Intercept download_file to record each file that reached the loop, then
    abort THAT file (RuntimeError is caught per-file → recorded as a failure,
    loop continues). Also stub the pipeline factory so no Supabase is touched."""
    monkeypatch.setattr(
        "cp_engine.asset_ingest._build_pipeline", lambda *a, **k: object()
    )

    def _rec(file_ref, *a, **k):
        seen.setdefault("names", []).append(file_ref.name)
        raise RuntimeError("stop after recording")

    monkeypatch.setattr("cp_engine.asset_ingest.download_file", _rec)


# ── file_selection_key ──────────────────────────────────────────────────


def test_selection_key_drive_is_id() -> None:
    assert file_selection_key(_drive("abc123", "brief.pdf")) == "abc123"


def test_selection_key_dropbox_is_path() -> None:
    assert file_selection_key(_dropbox("/proj/Deck.pdf")) == "/proj/Deck.pdf"


def test_selection_key_dropbox_without_path_falls_back_to_id() -> None:
    ref = FileRef(source="dropbox", id="id:only", name="x", mime_type=None,
                  size=1, modified=None, path=None)
    assert file_selection_key(ref) == "id:only"


# ── folder grouping (picker "(root)" bug) ───────────────────────────────


def test_folder_segments_dropbox_derives_ancestry_from_path():
    # The picker groups by folder_path. Dropbox FileRefs carry their folder
    # ONLY in path_display (folder_path is empty), so the annotated listing must
    # derive segments from the path — else every file collapses into "(root)".
    ref = FileRef(
        source="dropbox", id="id:1", name="Deck.pdf", mime_type=None, size=1,
        modified=None, path="/SAP 5174/Creative Assets/Decks/Deck.pdf",
    )
    assert _folder_segments(ref) == ["SAP 5174", "Creative Assets", "Decks"]


def test_folder_segments_dropbox_root_file_is_empty():
    ref = FileRef(
        source="dropbox", id="id:2", name="Top.pdf", mime_type=None, size=1,
        modified=None, path="/Top.pdf",
    )
    assert _folder_segments(ref) == []


# ── only_file_ids narrowing ─────────────────────────────────────────────


def test_only_file_ids_filters_to_selection(monkeypatch) -> None:
    _stub_list(monkeypatch, [_drive("keep", "a.pdf"), _drive("drop", "b.pdf")])
    seen: dict = {}
    _record_reaching_download(monkeypatch, seen)

    ingest_project_assets(
        "acme-1", client=object(), supabase_url="u", supabase_key="k",
        only_file_ids={"keep"},
    )
    # Only the selected file reached the download stage.
    assert seen.get("names") == ["a.pdf"]


def test_only_file_ids_selects_dropbox_by_path(monkeypatch) -> None:
    _stub_list(monkeypatch, [_dropbox("/proj/Keep.pdf"), _dropbox("/proj/Drop.pdf")])
    seen: dict = {}
    _record_reaching_download(monkeypatch, seen)

    ingest_project_assets(
        "acme-1", client=object(), supabase_url="u", supabase_key="k",
        only_file_ids={"/proj/Keep.pdf"},
    )
    assert seen.get("names") == ["Keep.pdf"]


def test_only_file_ids_empty_set_scans_nothing(monkeypatch) -> None:
    _stub_list(monkeypatch, [_drive("a", "a.pdf"), _drive("b", "b.pdf")])
    result = ingest_project_assets(
        "acme-1", client=object(), only_file_ids=set()
    )
    # Empty selection → nothing survives the filter → empty run, no pipeline.
    assert isinstance(result, IngestRunResult)
    assert result.created == 0 and result.versioned == 0


def test_only_file_ids_none_is_no_filter(monkeypatch) -> None:
    _stub_list(monkeypatch, [_drive("a", "a.pdf"), _drive("b", "b.pdf")])
    seen: dict = {}
    _record_reaching_download(monkeypatch, seen)

    ingest_project_assets(
        "acme-1", client=object(), supabase_url="u", supabase_key="k",
        only_file_ids=None,
    )
    assert set(seen.get("names", [])) == {"a.pdf", "b.pdf"}
