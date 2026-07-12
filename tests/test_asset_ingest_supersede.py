"""Tests for the same-title supersede on re-ingest (#57).

A doc whose CONTENT changed re-arrives at a fresh temp path, so the pipeline's
path-keyed dedup 'created' a duplicate row under the same title. The supersede
step must chain the new row via prev_asset_id and retire the prior copies
(status='superseded' + chunks deleted). Same-title-same-hash stays a no-op
(the cross-path hash dedup's province).

Fakes mirror the PostgREST chain conventions of test_asset_ingest_run.py:
no real Supabase / pipeline / connectors are touched.
"""

from __future__ import annotations

from pathlib import Path

from cp_engine.asset_ingest import (
    FileRef,
    ProjectFolders,
    _escape_like,
    _supersede_same_title,
    ingest_project_assets,
)

# ──────────────────────────────────────────────────────────────────────
#  Fakes
# ──────────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """One PostgREST chain: select/update/delete + eq/ilike filters."""

    def __init__(self, client, table):
        self._c = client
        self._table = table
        self._mode = None  # ('select', cols) | ('update', payload) | ('delete',)
        self._filters = {}
        self._ilike = None

    def select(self, cols):
        self._mode = ("select", cols)
        return self

    def update(self, payload):
        self._mode = ("update", payload)
        return self

    def delete(self):
        self._mode = ("delete",)
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def ilike(self, col, pattern):
        self._ilike = (col, pattern)
        return self

    def execute(self):
        kind = self._mode[0]
        if kind == "select":
            return _Resp(self._c.on_select(self._table, self._filters, self._ilike))
        if kind == "update":
            self._c.updates.append(
                {
                    "table": self._table,
                    "payload": self._mode[1],
                    "filters": dict(self._filters),
                }
            )
            return _Resp([])
        self._c.deletes.append(
            {"table": self._table, "filters": dict(self._filters)}
        )
        return _Resp([])


class _FakeClient:
    """Seeds the same-title select; records every update/delete.

    `prior_rows` is what the supersede's ilike-titled select returns. Selects
    carrying a `file_hash` filter (the cross-path dedup pre-check) or a
    `source_provider` filter (the ingest cache check) return `hash_rows` /
    nothing respectively.
    """

    def __init__(self, prior_rows=None, hash_rows=None):
        self.prior_rows = list(prior_rows or [])
        self.hash_rows = list(hash_rows or [])
        self.updates = []
        self.deletes = []
        self.select_ilikes = []  # (col, pattern) of every ilike select

    def table(self, name):
        return _FakeQuery(self, name)

    def on_select(self, table, filters, ilike):
        if ilike is not None:
            self.select_ilikes.append(ilike)
            return list(self.prior_rows)
        if "file_hash" in filters:
            return list(self.hash_rows)
        return []  # ingest-cache check / stamp meta read


_FOLDERS = ProjectFolders(
    project_id="proj-1",
    company_id="co-9",
    company_kind="client",
    google_drive_folder_id=None,
    mc_dropbox_folder_id="/Clients/Acme",
    enable_google_drive=False,
    enable_dropbox=True,
)

_INITIATIVE_FOLDERS = ProjectFolders(
    project_id="init-1",
    company_id="co-9",
    company_kind="internal",
    google_drive_folder_id=None,
    mc_dropbox_folder_id="/Internal",
    enable_google_drive=False,
    enable_dropbox=True,
    is_initiative=True,
)


def _prior(id_, created, *, path="/tmp/old/brief.pdf", file_hash="oldhash"):
    return {
        "id": id_,
        "created_at": created,
        "file_hash": file_hash,
        "file_path": path,
    }


# ──────────────────────────────────────────────────────────────────────
#  _escape_like
# ──────────────────────────────────────────────────────────────────────


def test_escape_like_neutralizes_wildcards():
    assert _escape_like("Q1_report 100%.pdf") == "Q1\\_report 100\\%.pdf"
    assert _escape_like("back\\slash") == "back\\\\slash"
    assert _escape_like("plain title.docx") == "plain title.docx"


# ──────────────────────────────────────────────────────────────────────
#  _supersede_same_title (unit)
# ──────────────────────────────────────────────────────────────────────


def test_supersede_chains_new_row_and_retires_priors():
    priors = [
        _prior("a-old", "2026-06-01T00:00:00Z"),
        _prior("a-older", "2026-05-01T00:00:00Z", path="/tmp/older/brief.pdf"),
    ]
    client = _FakeClient(prior_rows=priors)

    n = _supersede_same_title(
        client,
        _FOLDERS,
        title="Brief.pdf",
        file_path="/tmp/new/brief.pdf",
        file_hash="newhash",
    )

    assert n == 2
    # Title matched exactly (escaped ilike pattern), never a substring glob.
    assert client.select_ilikes == [("title", "Brief.pdf")]

    # 1. The NEW row (dedup key: owner + file_path + active) is chained to the
    #    NEWEST prior copy.
    chain = client.updates[0]
    assert chain["table"] == "rag_assets"
    assert chain["payload"] == {"prev_asset_id": "a-old"}
    assert chain["filters"] == {
        "project_id": "proj-1",
        "file_path": "/tmp/new/brief.pdf",
        "status": "active",
    }

    # 2. EVERY prior is retired: status='superseded' + its chunks deleted
    #    (embeddings FK-cascade with the chunks).
    retired = [
        u for u in client.updates if u["payload"] == {"status": "superseded"}
    ]
    assert [u["filters"]["id"] for u in retired] == ["a-old", "a-older"]
    assert [d["filters"]["asset_id"] for d in client.deletes] == [
        "a-old",
        "a-older",
    ]
    assert all(d["table"] == "asset_chunks" for d in client.deletes)


def test_supersede_excludes_same_hash_and_same_path_rows():
    # Same hash → identical bytes → the cross-path dedup's province (and the
    # issue's "same-title-same-hash stays a no-op"). Same path → the
    # pipeline's skip/new_version province. Neither may be retired here.
    priors = [
        _prior("a-samehash", "2026-06-01T00:00:00Z", file_hash="newhash"),
        _prior(
            "a-samepath", "2026-05-01T00:00:00Z", path="/tmp/new/brief.pdf"
        ),
    ]
    client = _FakeClient(prior_rows=priors)

    n = _supersede_same_title(
        client,
        _FOLDERS,
        title="Brief.pdf",
        file_path="/tmp/new/brief.pdf",
        file_hash="newhash",
    )

    assert n == 0
    assert client.updates == []
    assert client.deletes == []


def test_supersede_noop_without_hash_or_priors():
    # No file_hash (hashing failed upstream) → can't honor the same-hash
    # no-op contract → do nothing.
    client = _FakeClient(prior_rows=[_prior("a-old", "2026-06-01T00:00:00Z")])
    assert (
        _supersede_same_title(
            client,
            _FOLDERS,
            title="Brief.pdf",
            file_path="/tmp/new/brief.pdf",
            file_hash=None,
        )
        == 0
    )
    assert client.updates == []

    # No prior copies → nothing to chain or retire.
    empty = _FakeClient(prior_rows=[])
    assert (
        _supersede_same_title(
            empty,
            _FOLDERS,
            title="Brief.pdf",
            file_path="/tmp/new/brief.pdf",
            file_hash="newhash",
        )
        == 0
    )
    assert empty.updates == []


def test_supersede_uses_initiative_owner_column():
    client = _FakeClient(prior_rows=[_prior("a-old", "2026-06-01T00:00:00Z")])
    _supersede_same_title(
        client,
        _INITIATIVE_FOLDERS,
        title="Brief.pdf",
        file_path="/tmp/new/brief.pdf",
        file_hash="newhash",
    )
    chain = client.updates[0]
    assert chain["filters"]["initiative_id"] == "init-1"
    assert "project_id" not in chain["filters"]


# ──────────────────────────────────────────────────────────────────────
#  Run-loop wiring (a 'created' ingest triggers the supersede)
# ──────────────────────────────────────────────────────────────────────


class _FakeIngestResult:
    def __init__(self, action, file_path=""):
        self.action = action
        self.file_path = file_path
        self.error = None


class _FakePipeline:
    def __init__(self, action):
        self._action = action
        self.calls = []

    def ingest_file(self, file_path, title, url=None):
        self.calls.append({"file_path": file_path, "title": title})
        return _FakeIngestResult(self._action, file_path=file_path)


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


def _patch_run_seams(monkeypatch, files):
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

    def _download(file_ref, dest_dir, *_a, **_kw):
        p = Path(dest_dir) / file_ref.name
        p.write_bytes(b"changed content")
        return p

    monkeypatch.setattr("cp_engine.asset_ingest.download_file", _download)


def test_created_ingest_supersedes_prior_same_title(monkeypatch, tmp_path):
    client = _FakeClient(prior_rows=[_prior("a-old", "2026-06-01T00:00:00Z")])
    _patch_run_seams(monkeypatch, [_ref("brief.pdf")])

    result = ingest_project_assets(
        "acme-1",
        client=client,
        pipeline=_FakePipeline("created"),
        tmp_root=tmp_path,
    )

    assert result.created == 1
    assert result.superseded == 1
    assert result.failures == []
    # Chain + retire landed: prev_asset_id update, superseded flip, chunk delete.
    payloads = [u["payload"] for u in client.updates]
    assert {"prev_asset_id": "a-old"} in payloads
    assert {"status": "superseded"} in payloads
    assert [d["filters"]["asset_id"] for d in client.deletes] == ["a-old"]


def test_versioned_ingest_does_not_run_supersede(monkeypatch, tmp_path):
    # 'versioned' = the pipeline already chained + superseded its same-path
    # predecessor; the same-title supersede must not fire (it would clobber
    # the pipeline-written prev_asset_id).
    client = _FakeClient(prior_rows=[_prior("a-old", "2026-06-01T00:00:00Z")])
    _patch_run_seams(monkeypatch, [_ref("brief.pdf")])

    result = ingest_project_assets(
        "acme-1",
        client=client,
        pipeline=_FakePipeline("versioned"),
        tmp_root=tmp_path,
    )

    assert result.versioned == 1
    assert result.superseded == 0
    assert client.select_ilikes == []  # supersede lookup never ran


def test_supersede_failure_is_collected_not_fatal(monkeypatch, tmp_path):
    class _BoomClient(_FakeClient):
        def on_select(self, table, filters, ilike):
            if ilike is not None:
                raise RuntimeError("transient PostgREST error")
            return super().on_select(table, filters, ilike)

    client = _BoomClient(prior_rows=[_prior("a-old", "2026-06-01T00:00:00Z")])
    _patch_run_seams(monkeypatch, [_ref("brief.pdf")])

    result = ingest_project_assets(
        "acme-1",
        client=client,
        pipeline=_FakePipeline("created"),
        tmp_root=tmp_path,
    )

    # The ingest itself succeeded; the supersede failure is surfaced, not fatal.
    assert result.created == 1
    assert result.superseded == 0
    assert any("supersede failed" in err for _n, err in result.failures)
