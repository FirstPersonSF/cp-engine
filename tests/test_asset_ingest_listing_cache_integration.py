"""Integration tests pinning the listing cache's REASON TO EXIST.

The unit tests in `test_asset_ingest_listing_cache.py` cover `_cached_listing` in
isolation. These tests pin the two end-to-end properties that a per-project clear
(the bug found in review) would silently break:

1. Two scans of the SAME (provider, folder_id) within one CLI run walk the folder
   ONCE total — the real `list_files` → `_cached_listing` → `_list_dropbox` path,
   with an injected counting connector. With `use_cache=False` it walks each time.

2. `fan_out_ingest` clears the cache ONCE before its per-project loop, NOT per
   project — otherwise the cache is wiped between every project and property (1)
   never fires across an `--all` sweep.
"""

from __future__ import annotations

import pytest

import cp_engine.asset_ingest_cli as asset_ingest_cli
from cp_engine.asset_ingest import (
    ProjectFolders,
    _clear_listing_cache,
    list_files,
)


@pytest.fixture(autouse=True)
def _reset_listing_cache():
    """The listing cache is module-level and survives the whole pytest session —
    clear before and after each test so these cases don't leak into one another
    or inherit a cached listing from another module."""
    _clear_listing_cache()
    yield
    _clear_listing_cache()


# ── Fakes (mirroring test_asset_ingest_listing.py's dropbox fake) ──


class _DbxResult:
    def __init__(self, entries, has_more=False, cursor=None):
        self.entries = entries
        self.has_more = has_more
        self.cursor = cursor


class _FakeDropboxEntry:
    """Minimal FileMetadata stand-in (has .size → kept by _list_dropbox)."""

    def __init__(self, name):
        self.name = name
        self.path_display = f"/Clients/Shared/{name}"
        self.size = 10
        self.client_modified = "2026-06-13T00:00:00Z"
        self.id = f"id:{name}"
        self.content_hash = f"hash-{name}"


class _CountingDropboxConnector:
    """Counts how many times the underlying folder walk is actually invoked."""

    class _Dbx:
        def __init__(self, entries):
            self._entries = entries
            self.walk_calls = 0

        def files_list_folder(self, path, recursive=False):
            self.walk_calls += 1
            return _DbxResult(self._entries, has_more=False)

        def files_list_folder_continue(self, cursor):  # pragma: no cover
            raise AssertionError("single-page fake should not paginate")

    def __init__(self, entries):
        self.dbx = self._Dbx(entries)


def _client_folders(folder_id: str) -> ProjectFolders:
    return ProjectFolders(
        project_id="proj-x",
        company_id="co-1",
        company_kind="client",
        google_drive_folder_id=None,
        mc_dropbox_folder_id=folder_id,
        enable_google_drive=False,
        enable_dropbox=True,
    )


# ── Property 1: two scans of the same folder → ONE walk ──


def test_two_list_files_calls_same_folder_walk_once_when_cached() -> None:
    """Simulates two projects in one --all run sharing the same Dropbox parent."""
    conn = _CountingDropboxConnector([_FakeDropboxEntry("brief.pdf")])
    folders = _client_folders("/Clients/Shared")

    out1, _ = list_files(folders, dropbox_connector=conn, use_cache=True)
    # SAME (provider, folder_id) → second call is a cache hit → no second walk.
    out2, _ = list_files(folders, dropbox_connector=conn, use_cache=True)

    assert conn.dbx.walk_calls == 1
    assert {r.name for r in out1} == {"brief.pdf"}
    assert {r.name for r in out2} == {"brief.pdf"}


def test_two_list_files_calls_walk_each_time_when_cache_disabled() -> None:
    conn = _CountingDropboxConnector([_FakeDropboxEntry("brief.pdf")])
    folders = _client_folders("/Clients/Shared")

    list_files(folders, dropbox_connector=conn, use_cache=False)
    list_files(folders, dropbox_connector=conn, use_cache=False)

    # use_cache=False → ttl=0 pass-through → every scan re-walks.
    assert conn.dbx.walk_calls == 2


def test_different_folders_each_walk_once() -> None:
    conn_a = _CountingDropboxConnector([_FakeDropboxEntry("a.pdf")])
    conn_b = _CountingDropboxConnector([_FakeDropboxEntry("b.pdf")])

    list_files(_client_folders("/Clients/A"), dropbox_connector=conn_a, use_cache=True)
    list_files(_client_folders("/Clients/B"), dropbox_connector=conn_b, use_cache=True)
    # Repeat — both should be cache hits, no extra walks.
    list_files(_client_folders("/Clients/A"), dropbox_connector=conn_a, use_cache=True)
    list_files(_client_folders("/Clients/B"), dropbox_connector=conn_b, use_cache=True)

    assert conn_a.dbx.walk_calls == 1
    assert conn_b.dbx.walk_calls == 1


# ── Property 2: fan_out_ingest clears ONCE, not per project ──


def test_fan_out_clears_listing_cache_once_not_per_project(monkeypatch) -> None:
    """The clear must run once before the loop. If it moved back inside
    ingest_project_assets (per project), this would see N clears for N codes and
    the cross-project cache benefit would be gone."""
    clears: list[int] = []
    monkeypatch.setattr(
        "cp_engine.asset_ingest._clear_listing_cache",
        lambda: clears.append(1),
    )

    # Isolate the clear behavior: stub the per-project work to a no-op.
    ran: list[str] = []

    def _fake_ingest(code, *, client=None, use_cache=True):
        ran.append(code)

        class _R:
            created = versioned = skipped = deduped = 0
            skipped_unchanged = skipped_shortcuts = failed = 0
            failures: list = []

        return _R()

    monkeypatch.setattr(
        "cp_engine.asset_ingest.ingest_project_assets", _fake_ingest
    )

    asset_ingest_cli.fan_out_ingest(
        object(), ["acme-1", "acme-2", "acme-3"], use_cache=True
    )

    assert ran == ["acme-1", "acme-2", "acme-3"]  # all projects ran
    assert sum(clears) == 1  # cache cleared exactly ONCE, before the loop
