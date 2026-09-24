"""Internal-workstream asset ingest (mc-2 #192, cp-engine #301).

An internal workstream is a self-company `projects` row with `deal_stage`
NULL — StoryOS, Mission Control. Since #301 there is no `initiatives`
table and no second owner column: it resolves by job number like any other
workstream, hydrates its folders from `project_id`-owned bindings, and
writes `project_id`-owned rag_assets rows. What still makes it special is
the SCOPE guard: a self-company row WITH an agreement is house/framework
territory and is skipped; without one it ingests like a client job.

Everything external is faked: a multi-table fake Supabase client (projects,
project_integrations, rag_assets), fake connectors, injected pipeline
factories. No network anywhere.

Covered:
  - `resolve_project_folders` by number (canonical slug code), bindings
    hydration (Drive id + Dropbox path), `has_agreement=False`.
  - `resolve_project_folders_by_id` on the same row.
  - `folders_unconfigured_reason`: an unconfigured internal workstream gates
    exactly like an unconfigured client project (never a silent pass); a
    configured one passes; a self-company row WITH an agreement bypasses.
  - `list_files` does NOT kind-skip an internal workstream despite its
    self-* company, and still skips a self-company row with an agreement.
  - the run loop stamps/reads by `project_id` end to end.
  - `fan_out_ingest` hands internal codes through per-project runs.
"""

from __future__ import annotations

from types import SimpleNamespace

from cp_engine.asset_ingest import (
    FileRef,
    ProjectFolders,
    _clear_listing_cache,
    folders_unconfigured_reason,
    ingest_project_assets,
    list_files,
    resolve_project_folders,
    resolve_project_folders_by_id,
)

# ──────────────────────────────────────────────────────────────────────
#  Fakes
# ──────────────────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    """Generic chain: select/eq/in_/ilike/order/limit → execute → canned rows,
    filtered by any eq() the caller applied when the seeded rows carry that
    column."""

    def __init__(self, rows, recorder=None):
        self._rows = rows
        self._recorder = recorder if recorder is not None else {}
        self._filters = {}

    def select(self, cols):
        self._recorder["select"] = cols
        return self

    def eq(self, col, val):
        self._filters[col] = val
        self._recorder.setdefault("eq", []).append((col, val))
        return self

    def in_(self, col, vals):
        self._recorder["in_"] = (col, list(vals))
        return self

    def ilike(self, col, pattern):
        self._filters[f"ilike:{col}"] = pattern
        return self

    def order(self, col, desc=False):
        return self

    def limit(self, n):
        return self

    def execute(self):
        rows = [
            r
            for r in self._rows
            if all(
                r.get(col) == val
                for col, val in self._filters.items()
                if not col.startswith("ilike:") and col in r
            )
        ]
        return _Resp(rows)


class _FakeUpdate:
    def __init__(self, sink):
        self._sink = sink
        self._payload = None
        self._filters = {}

    def update(self, payload):
        self._payload = payload
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def execute(self):
        self._sink.append({"payload": self._payload, "filters": dict(self._filters)})
        return _Resp([])


class _FakeRagAssetsTable:
    """rag_assets: select() reads the seeded rows; update() records stamps."""

    def __init__(self, client):
        self._client = client

    def select(self, cols):
        q = _FakeQuery(self._client.rag_rows, self._client.rag_recorder)
        return q.select(cols)

    def update(self, payload):
        return _FakeUpdate(self._client.updates).update(payload)


class _FakeClient:
    """Multi-table fake: projects + project_integrations reads, rag_assets
    reads/updates. One table for every workstream (#301)."""

    def __init__(self, *, projects=(), bindings=(), rag_rows=()):
        self.projects = list(projects)
        self.bindings = list(bindings)
        self.rag_rows = list(rag_rows)
        self.updates = []
        self.recorders = {}
        self.rag_recorder = {}

    def table(self, name):
        if name == "rag_assets":
            return _FakeRagAssetsTable(self)
        rows = {
            "projects": self.projects,
            "project_integrations": self.bindings,
        }[name]
        rec = self.recorders.setdefault(name, {})
        return _FakeQuery(rows, rec)




_CODE = "cnc-9004-storyos"


def _internal_row(pid="init-1", number=9004, company_id="co-canonic", deal_stage=None):
    """A self-company `projects` row. `deal_stage` NULL = internal workstream
    (mig 192 kept the old initiative uuid as `id`)."""
    return {
        "id": pid,
        "number": number,
        "company_id": company_id,
        "deal_stage": deal_stage,
        "enable_google_drive": True,
        "enable_dropbox": True,
        "asset_ingest_folders": None,
        "companies": {"kind": "self-canonic"},
    }


def _folder_bindings(pid="init-1", drive_id=None, dropbox_path=None):
    rows = []
    if drive_id:
        rows.append({
            "project_id": pid,
            "service": "google_drive", "external_ref": {"id": drive_id},
            "label": "",
        })
    if dropbox_path:
        rows.append({
            "project_id": pid,
            "service": "dropbox", "external_ref": {"url": dropbox_path},
            "label": "",
        })
    return rows


def _internal_folders(**overrides) -> ProjectFolders:
    base = dict(
        project_id="init-1",
        company_id="co-canonic",
        company_kind="self-canonic",
        google_drive_folder_id=None,
        mc_dropbox_folder_id="/Internal/StoryOS",
        enable_google_drive=True,
        enable_dropbox=True,
        has_agreement=False,
    )
    base.update(overrides)
    return ProjectFolders(**base)


# ──────────────────────────────────────────────────────────────────────
#  Resolve
# ──────────────────────────────────────────────────────────────────────


def test_internal_code_resolves_by_number_with_binding_hydration():
    client = _FakeClient(
        projects=[_internal_row()],
        bindings=_folder_bindings(drive_id="drv-1", dropbox_path="/Internal/StoryOS"),
    )
    folders = resolve_project_folders(client, _CODE)
    assert folders is not None
    assert folders.has_agreement is False
    assert folders.project_id == "init-1"
    assert folders.company_id == "co-canonic"
    assert folders.company_kind == "self-canonic"
    assert folders.google_drive_folder_id == "drv-1"
    assert folders.mc_dropbox_folder_id == "/Internal/StoryOS"
    assert folders.enable_google_drive is True
    assert folders.enable_dropbox is True
    assert folders.asset_ingest_folders == ()
    # By job number, explicit columns, never *.
    assert ("number", 9004) in client.recorders["projects"]["eq"]
    assert "*" not in client.recorders["projects"]["select"]
    assert "deal_stage" in client.recorders["projects"]["select"]


def test_resolve_by_id_reads_the_same_row():
    client = _FakeClient(
        projects=[_internal_row()],
        bindings=_folder_bindings(drive_id="drv-1"),
    )
    folders = resolve_project_folders_by_id(client, "init-1")
    assert folders is not None
    assert folders.has_agreement is False
    assert folders.google_drive_folder_id == "drv-1"


def test_self_company_row_with_agreement_reads_has_agreement():
    client = _FakeClient(projects=[_internal_row(deal_stage="Won")])
    folders = resolve_project_folders(client, _CODE)
    assert folders is not None and folders.has_agreement is True


def test_resolve_by_id_none_when_no_row(capsys):
    client = _FakeClient()
    assert resolve_project_folders_by_id(client, "nope") is None
    assert "no MC-2 project with id=nope" in capsys.readouterr().err


def test_bare_word_resolves_none(capsys):
    """A code with no job number is not a workstream (#301) — no table read."""
    client = _FakeClient(projects=[_internal_row()])
    assert resolve_project_folders(client, "storyos") is None
    assert "is not a workstream code" in capsys.readouterr().err
    assert "projects" not in client.recorders


# ──────────────────────────────────────────────────────────────────────
#  Confirm gate (#59) parity
# ──────────────────────────────────────────────────────────────────────


def test_unconfigured_internal_workstream_gates_like_a_client_project():
    folders = _internal_folders(mc_dropbox_folder_id=None)
    reason = folders_unconfigured_reason(folders)
    assert reason is not None
    assert "enabled but folder not set" in reason


def test_configured_internal_workstream_passes_the_gate():
    assert folders_unconfigured_reason(_internal_folders()) is None


def test_self_company_row_with_agreement_bypasses_the_gate():
    folders = _internal_folders(has_agreement=True)  # house/framework territory
    assert folders_unconfigured_reason(folders) is None


# ──────────────────────────────────────────────────────────────────────
#  list_files scope guard
# ──────────────────────────────────────────────────────────────────────


class _FakeDropboxEntry:
    def __init__(self, name, path):
        self.id = f"id:{name}"
        self.name = name
        self.size = 10
        self.client_modified = "2026-07-01"
        self.path_display = path
        self.content_hash = f"hash-{name}"


class _FakeDbx:
    def __init__(self, entries):
        self._entries = entries

    def files_list_folder(self, folder, recursive=True):
        return SimpleNamespace(entries=self._entries, has_more=False)


class _FakeDropboxConnector:
    def __init__(self, entries):
        self.dbx = _FakeDbx(entries)




def test_list_files_does_not_kind_skip_an_internal_workstream():
    _clear_listing_cache()
    connector = _FakeDropboxConnector(
        [_FakeDropboxEntry("brief.pdf", "/Internal/StoryOS/brief.pdf")]
    )
    refs, notes = list_files(
        _internal_folders(), dropbox_connector=connector, use_cache=False
    )
    assert [r.name for r in refs] == ["brief.pdf"]
    # No kind-skip. The only note is the drive side reporting its unset
    # folder, never a skip of the whole item.
    assert [n["source"] for n in notes] == ["drive"]


def test_list_files_still_skips_self_company_row_with_agreement(capsys):
    _clear_listing_cache()
    refs, notes = list_files(_internal_folders(has_agreement=True), use_cache=False)
    assert refs == [] and notes == []
    assert "with an agreement" in capsys.readouterr().err


# ──────────────────────────────────────────────────────────────────────
#  Run loop: the owner pair end to end
# ──────────────────────────────────────────────────────────────────────


class _FakeIngestResult:
    def __init__(self, action):
        self.action = action
        self.error = None


class _FakePipeline:
    def __init__(self):
        self.calls = []

    def ingest_file(self, file_path, title, url=None):
        self.calls.append({"file_path": file_path, "title": title})
        return _FakeIngestResult("created")



def test_internal_run_stamps_by_project_id(tmp_path, monkeypatch):
    _clear_listing_cache()
    client = _FakeClient(
        projects=[_internal_row()],
        bindings=_folder_bindings(dropbox_path="/Internal/StoryOS"),
    )
    connector = _FakeDropboxConnector(
        [_FakeDropboxEntry("brief.pdf", "/Internal/StoryOS/brief.pdf")]
    )

    def _fake_download(ref, tmp_dir, *a, **k):
        # NOT `write_bytes(...) or path`: write_bytes returns the byte COUNT
        # (truthy), so that expression returns an int — and downstream
        # `open(<int>)` treats it as a raw file descriptor, closing an fd the
        # test run doesn't own (this killed the whole suite at teardown).
        p = tmp_dir / ref.name
        p.write_bytes(b"pdf-bytes")
        return p

    monkeypatch.setattr("cp_engine.asset_ingest.download_file", _fake_download)
    pipeline = _FakePipeline()
    run = ingest_project_assets(
        _CODE,
        client=client,
        dropbox_connector=connector,
        tmp_root=tmp_path,
        pipeline_factory=lambda pid, url, key: pipeline,
        supabase_url="http://fake",
        supabase_key="fake",
    )
    assert run.created == 1 and run.failed == 0
    assert run.unconfigured_reason is None
    assert len(pipeline.calls) == 1
    # The scope stamp landed filtered on `project_id` — the one owner column.
    stamp = client.updates[-1]
    assert stamp["filters"]["project_id"] == "init-1"
    assert "initiative_id" not in stamp["filters"]
    assert stamp["payload"]["company_id"] == "co-canonic"
    # The skip/dedup pre-reads also keyed on `project_id`.
    eqs = client.rag_recorder.get("eq", [])
    assert ("project_id", "init-1") in eqs
    assert not any(col == "initiative_id" for col, _ in eqs)


def test_unconfigured_internal_run_short_circuits_with_reason():
    _clear_listing_cache()
    client = _FakeClient(projects=[_internal_row()], bindings=[])
    run = ingest_project_assets(
        _CODE,
        client=client,
        supabase_url="http://fake",
        supabase_key="fake",
    )
    assert run.project_found is True
    assert run.unconfigured_reason is not None
    assert "enabled but folder not set" in run.unconfigured_reason
    assert run.created == 0 and client.updates == []


def test_fan_out_runs_internal_codes(monkeypatch):
    from cp_engine import asset_ingest_cli
    from cp_engine.asset_ingest import IngestRunResult

    seen = []

    def _fake_run(code, *, client=None, use_cache=True):
        seen.append(code)
        return IngestRunResult(created=1)

    monkeypatch.setattr(
        "cp_engine.asset_ingest.ingest_project_assets", _fake_run
    )
    result = asset_ingest_cli.fan_out_ingest(
        object(), ["ibx-5153", _CODE, "1pi-9005-mission-control"]
    )
    assert seen == ["ibx-5153", _CODE, "1pi-9005-mission-control"]
    assert result.total_created == 3
