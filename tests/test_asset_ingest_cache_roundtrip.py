"""Closed-loop round-trip test for the ingest cache (item 5).

The ingest cache works in two halves that live in two different functions:

  - WRITE (`_stamp_scope`): after a created/versioned ingest, stamp the
    provider content-hash into the active row's `meta.change_token`, located by
    (owner_col, file_path, status='active').
  - READ (`_unchanged_since_last_ingest`): on a LATER run, look up the active
    row by (project_id, source_provider, source_file_id, status='active') and
    skip the file iff its `meta.change_token` equals the freshly-listed token.

Every OTHER test proves only one half:
  - `test_asset_ingest_run.py::test_run_stamps_scope_after_created` pins the
    write SHAPE (asserts the UPDATE payload/filters) but never reads it back.
  - `test_asset_ingest_skip_loop.py::test_unchanged_file_is_skipped...` proves
    the read path skips, but against a HAND-SEEDED `{"change_token": ...}` row —
    the write never produced it.

So a future drift in the `meta.change_token` key path on only ONE side (a
renamed key, a different locate-filter, the stamp writing meta under a different
shape) would be a silent no-op: the cache just stops firing, with no failing
test. This file closes that gap with a STATEFUL in-memory store shared across
runs, so the REAL `_stamp_scope` write and the REAL `_unchanged_since_last_ingest`
read traverse the SAME `meta.change_token` on the SAME stored row.

What's REAL vs stubbed
----------------------
REAL (exercised end-to-end through `ingest_project_assets`):
  - `_unchanged_since_last_ingest` (the skip read) — its actual SELECT chain.
  - `_existing_dup_at_other_path` (the cross-path dedup pre-check) — actual SELECT.
  - `_stamp_scope` (the stamp write) — its actual SELECT(meta) read-modify-write
    + UPDATE chain, including the `meta.change_token` merge.
  - The ingest loop wiring (skip-before-download, count bookkeeping, cleanup).

STUBBED:
  - `resolve_project_folders` / `list_files` / `download_file` — injected, as in
    the sibling run tests (no network).
  - The document-ingest PIPELINE — a stateful fake whose `ingest_file` writes a
    rag_assets row into the SAME store (modeling what the real pipeline does:
    create/upsert the row keyed on (project_id, file_path)). `_stamp_scope` then
    UPDATEs that very row; `_unchanged_since_last_ingest` then reads it back.
  - The Supabase client — a stateful fake `_StatefulStore` backed by one list of
    row dicts, implementing the .table().select()/.update()/.insert()/.eq()/
    .limit().execute() chain the real code uses, so writes and reads share state.
"""

from __future__ import annotations

from pathlib import Path

from cp_engine.asset_ingest import (
    FileRef,
    IngestRunResult,
    ProjectFolders,
    _content_hash,
    _stable_dir_for,
    ingest_project_assets,
)


# ──────────────────────────────────────────────────────────────────────
#  A genuinely stateful in-memory Supabase fake
# ──────────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    """A select/update/insert query that filters/mutates the shared row list.

    Backs the chain the real code uses:
      .select(cols).eq(...).eq(...)....limit(n).execute()  -> _Resp(rows)
      .update(payload).eq(...)....execute()                -> applies to matches
      .insert(row).execute()                               -> appends a row
    """

    def __init__(self, store: "_StatefulStore"):
        self._store = store
        self._kind: str | None = None
        self._cols: str | None = None
        self._payload: dict | None = None
        self._filters: dict = {}
        self._limit: int | None = None

    def select(self, cols):
        self._kind = "select"
        self._cols = cols
        return self

    def update(self, payload):
        self._kind = "update"
        self._payload = payload
        return self

    def insert(self, row):
        self._kind = "insert"
        self._payload = dict(row)
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _matches(self, row: dict) -> bool:
        return all(row.get(col) == val for col, val in self._filters.items())

    def execute(self):
        if self._kind == "insert":
            self._store.rows.append(self._payload)
            self._store.inserts.append(self._payload)
            return _Resp([self._payload])

        matched = [r for r in self._store.rows if self._matches(r)]

        if self._kind == "select":
            self._store.selects.append(dict(self._filters))
            rows = matched
            if self._limit is not None:
                rows = rows[: self._limit]
            # Project only the requested columns the way PostgREST would, but a
            # superset is harmless for the readers here; return whole dicts so
            # `meta` is present regardless of `cols`.
            return _Resp([dict(r) for r in rows])

        if self._kind == "update":
            self._store.updates.append(
                {"payload": dict(self._payload), "filters": dict(self._filters)}
            )
            for r in matched:
                r.update(self._payload)
            return _Resp([dict(r) for r in matched])

        raise AssertionError(f"unknown query kind {self._kind!r}")


class _StatefulStore:
    """Fake Supabase client whose rag_assets rows PERSIST across calls/runs."""

    def __init__(self):
        self.rows: list[dict] = []  # the one shared rag_assets table
        self.selects: list[dict] = []
        self.updates: list[dict] = []
        self.inserts: list[dict] = []

    def table(self, name):
        assert name == "rag_assets", f"unexpected table {name!r}"
        return _Query(self)


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


class _StatefulPipeline:
    """Fake pipeline that WRITES the rag_assets row into the shared store.

    Models the real document-ingest pipeline's row creation: on `ingest_file`
    it upserts a row keyed on (project_id, file_path) carrying the file_hash,
    source ids, and an empty meta — so that `_stamp_scope` can locate & stamp it
    and `_unchanged_since_last_ingest` can later read it back. Returns 'created'
    for a brand-new (project_id, file_path), 'versioned' for an existing one
    (content changed), mirroring the real create-vs-version distinction enough
    for this test. Records calls so the test can assert re-download/re-ingest.
    """

    def __init__(self, store: _StatefulStore, folders: ProjectFolders, ref_by_path):
        self._store = store
        self._folders = folders
        self._ref_by_path = ref_by_path  # file_path -> FileRef (for source ids/hash)
        self.calls: list[dict] = []

    def ingest_file(self, file_path, title, url=None):
        self.calls.append({"file_path": file_path, "title": title, "url": url})
        ref = self._ref_by_path[file_path]
        file_hash = _content_hash(file_path)
        existing = [
            r
            for r in self._store.rows
            if r.get("project_id") == self._folders.project_id
            and r.get("file_path") == file_path
            and r.get("status", "active") == "active"
        ]
        if existing:
            row = existing[0]
            row["file_hash"] = file_hash
            row["source_provider"] = ref.source
            row["source_file_id"] = ref.id
            return _FakeIngestResult("versioned", file_path=file_path)
        self._store.rows.append(
            {
                "id": f"asset-{len(self._store.rows) + 1}",
                "project_id": self._folders.project_id,
                "file_path": file_path,
                "file_hash": file_hash,
                "status": "active",
                "source_provider": ref.source,
                "source_file_id": ref.id,
                "meta": {},
            }
        )
        return _FakeIngestResult("created", file_path=file_path)


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


def _patch_download(monkeypatch, downloaded, *, body_by_name):
    """download writes per-file bytes (so edited content => new hash)."""

    def _fake_download(file_ref, tmp_dir, drive_connector=None, dropbox_connector=None):
        downloaded.append(file_ref.name)
        p = Path(tmp_dir) / file_ref.name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body_by_name[file_ref.name])
        return p

    monkeypatch.setattr("cp_engine.asset_ingest.download_file", _fake_download)


def _stable_path(tmp_root, file_ref) -> str:
    """The file_path the loop derives — both the dedup key AND the stamp locator."""
    return str(_stable_dir_for(Path(tmp_root), file_ref) / file_ref.name)


def _stored_token(store: _StatefulStore, file_path: str) -> str | None:
    rows = [r for r in store.rows if r.get("file_path") == file_path]
    assert len(rows) == 1, f"expected exactly one row at {file_path}, got {len(rows)}"
    return (rows[0].get("meta") or {}).get("change_token")


# ──────────────────────────────────────────────────────────────────────
#  The closed-loop test
# ──────────────────────────────────────────────────────────────────────


def test_run1_stamps_then_run2_skips_via_real_meta(monkeypatch, tmp_path):
    """Run 1's REAL `_stamp_scope` write → Run 2's REAL
    `_unchanged_since_last_ingest` read, on the SAME stored `meta.change_token`.

    1. Run once (token hash-v1): the file is ingested (created) and the stamp
       writes meta.change_token == "hash-v1" onto the active row.
    2. Run again, same token: the skip read finds that very token → skipped;
       NO re-download, NO re-ingest.
    3. Run a third time with the file edited (token hash-v2): the skip read sees
       a token MISMATCH → re-ingested, and the stamp updates meta.change_token
       to "hash-v2".
    """
    store = _StatefulStore()
    work_root = tmp_path / "work"
    body = {"a.docx": b"content-v1"}
    file_path = _stable_path(work_root, _ref("a.docx", token="x"))
    ref_by_path = {}  # file_path -> current FileRef, refreshed per run

    def _run(token):
        ref = _ref("a.docx", token=token)
        ref_by_path[file_path] = ref
        _patch_resolve_and_list(monkeypatch, [ref])
        downloaded: list[str] = []
        _patch_download(monkeypatch, downloaded, body_by_name=body)
        pipeline = _StatefulPipeline(store, _FOLDERS, ref_by_path)
        result = ingest_project_assets(
            "acme-1", client=store, pipeline=pipeline, tmp_root=work_root
        )
        return result, downloaded, pipeline

    # ── Run 1: stamps meta.change_token via the REAL _stamp_scope ──────────
    r1, dl1, p1 = _run("hash-v1")
    assert isinstance(r1, IngestRunResult)
    assert r1.created == 1
    assert r1.skipped_unchanged == 0
    assert dl1 == ["a.docx"]  # downloaded
    assert [c["title"] for c in p1.calls] == ["a.docx"]  # ingested
    # The REAL stamp wrote the token onto the SAME stored row.
    assert _stored_token(store, file_path) == "hash-v1"

    # ── Run 2: same token → the REAL skip read finds it → skip ────────────
    r2, dl2, p2 = _run("hash-v1")
    assert r2.skipped_unchanged == 1
    assert r2.created == 0
    assert r2.versioned == 0
    # Closed loop: NOT re-downloaded, NOT re-ingested — the cache fired off the
    # token that run 1's stamp wrote into this same store.
    assert dl2 == []
    assert p2.calls == []
    # still exactly one row, token unchanged
    assert _stored_token(store, file_path) == "hash-v1"

    # ── Run 3: edited file (new token) → mismatch → re-ingest + re-stamp ───
    body["a.docx"] = b"content-v2-edited"
    r3, dl3, p3 = _run("hash-v2")
    assert r3.skipped_unchanged == 0
    # re-ingested (the existing-row branch reports 'versioned')
    assert (r3.created, r3.versioned) == (0, 1)
    assert dl3 == ["a.docx"]  # re-downloaded
    assert [c["title"] for c in p3.calls] == ["a.docx"]  # re-ingested
    # the stamp updated the token in place — one row, new token
    assert _stored_token(store, file_path) == "hash-v2"
