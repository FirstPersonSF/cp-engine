"""Initiative asset ingest (mc-2 #192) — the initiatives-table resolve path,
the confirm-gate parity, the owner-column rebinding, and the run loop's
owner-aware reads.

Everything external is faked: a multi-table fake Supabase client (initiatives,
project_integrations, rag_assets), fake connectors, injected pipeline
factories. No network anywhere.

Covered:
  - `resolve_project_folders` slug fallback → initiatives table, bindings
    hydration (Drive id + Dropbox path), `is_initiative=True`.
  - a digit-carrying slug that misses `projects.number` falls through to the
    initiatives table by full code.
  - `resolve_project_folders_by_id` falls back to initiatives on a
    `projects.id` miss.
  - `folders_unconfigured_reason`: an unconfigured initiative gates exactly
    like an unconfigured client project (never a silent pass); a configured
    one passes; non-client ENGAGEMENTS still bypass the gate.
  - `list_files` does NOT kind-skip an initiative despite its self-* company.
  - `_adapt_pipeline_for_initiative` rebinds check_asset/create_asset to the
    `initiative_id` owner column (and never writes `project_id`).
  - the run loop stamps/reads by the initiative owner pair end to end.
  - `fan_out_ingest` hands initiative codes through per-project runs.
"""

from __future__ import annotations

from types import SimpleNamespace

from cp_engine.asset_ingest import (
    FileRef,
    ProjectFolders,
    _adapt_pipeline_for_initiative,
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
    """Multi-table fake: initiatives + projects + project_integrations reads,
    rag_assets reads/updates."""

    def __init__(self, *, initiatives=(), projects=(), bindings=(), rag_rows=()):
        self.initiatives = list(initiatives)
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
            "initiatives": self.initiatives,
            "projects": self.projects,
            "project_integrations": self.bindings,
        }[name]
        rec = self.recorders.setdefault(name, {})
        return _FakeQuery(rows, rec)


def _initiative_row(iid="init-1", code="storyos", company_id="co-canonic"):
    return {
        "id": iid,
        "code": code,
        "company_id": company_id,
        "companies": {"kind": "self-canonic"},
    }


def _folder_bindings(iid="init-1", drive_id=None, dropbox_path=None):
    rows = []
    if drive_id:
        rows.append({
            "project_id": None, "initiative_id": iid,
            "service": "google_drive", "external_ref": {"id": drive_id},
            "label": "",
        })
    if dropbox_path:
        rows.append({
            "project_id": None, "initiative_id": iid,
            "service": "dropbox", "external_ref": {"url": dropbox_path},
            "label": "",
        })
    return rows


def _initiative_folders(**overrides) -> ProjectFolders:
    base = dict(
        project_id="init-1",
        company_id="co-canonic",
        company_kind="self-canonic",
        google_drive_folder_id=None,
        mc_dropbox_folder_id="/Internal/StoryOS",
        enable_google_drive=True,
        enable_dropbox=True,
        is_initiative=True,
    )
    base.update(overrides)
    return ProjectFolders(**base)


# ──────────────────────────────────────────────────────────────────────
#  Resolve
# ──────────────────────────────────────────────────────────────────────


def test_slug_resolves_via_initiatives_with_binding_hydration():
    client = _FakeClient(
        initiatives=[_initiative_row()],
        bindings=_folder_bindings(drive_id="drv-1", dropbox_path="/Internal/StoryOS"),
    )
    folders = resolve_project_folders(client, "storyos")
    assert folders is not None
    assert folders.is_initiative is True
    assert folders.project_id == "init-1"
    assert folders.company_id == "co-canonic"
    assert folders.company_kind == "self-canonic"
    assert folders.google_drive_folder_id == "drv-1"
    assert folders.mc_dropbox_folder_id == "/Internal/StoryOS"
    # Initiatives have no per-source enable columns: both read enabled so the
    # confirm gate speaks "enabled but folder not set" for missing bindings.
    assert folders.enable_google_drive is True
    assert folders.enable_dropbox is True
    assert folders.asset_ingest_folders == ()
    # Explicit columns, never *.
    assert "*" not in client.recorders["initiatives"]["select"]


def test_digit_slug_missing_projects_falls_back_to_initiatives():
    # "web3-lab" parses number 3, which matches no project → the resolver must
    # try the initiatives table by FULL code rather than bail.
    client = _FakeClient(
        initiatives=[_initiative_row(code="web3-lab")],
        projects=[],
        bindings=_folder_bindings(dropbox_path="/Internal/Web3"),
    )
    folders = resolve_project_folders(client, "web3-lab")
    assert folders is not None
    assert folders.is_initiative is True
    assert folders.mc_dropbox_folder_id == "/Internal/Web3"


def test_resolve_by_id_falls_back_to_initiatives():
    client = _FakeClient(
        initiatives=[_initiative_row()],
        projects=[],
        bindings=_folder_bindings(drive_id="drv-1"),
    )
    folders = resolve_project_folders_by_id(client, "init-1")
    assert folders is not None
    assert folders.is_initiative is True
    assert folders.google_drive_folder_id == "drv-1"


def test_resolve_by_id_none_when_neither_table_matches(capsys):
    client = _FakeClient()
    assert resolve_project_folders_by_id(client, "nope") is None
    assert "no MC-2 project or initiative" in capsys.readouterr().err


def test_unknown_slug_resolves_none(capsys):
    client = _FakeClient()
    assert resolve_project_folders(client, "mission-control") is None
    assert "no MC-2 initiative with code" in capsys.readouterr().err


# ──────────────────────────────────────────────────────────────────────
#  Confirm gate (#59) parity
# ──────────────────────────────────────────────────────────────────────


def test_unconfigured_initiative_gates_like_a_client_project():
    folders = _initiative_folders(mc_dropbox_folder_id=None)
    reason = folders_unconfigured_reason(folders)
    assert reason is not None
    assert "enabled but folder not set" in reason


def test_configured_initiative_passes_the_gate():
    assert folders_unconfigured_reason(_initiative_folders()) is None


def test_non_client_engagement_still_bypasses_the_gate():
    folders = _initiative_folders(is_initiative=False)  # self-* ENGAGEMENT
    assert folders_unconfigured_reason(folders) is None


# ──────────────────────────────────────────────────────────────────────
#  list_files kind guard
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


def test_list_files_does_not_kind_skip_an_initiative():
    _clear_listing_cache()
    connector = _FakeDropboxConnector(
        [_FakeDropboxEntry("brief.pdf", "/Internal/StoryOS/brief.pdf")]
    )
    refs, notes = list_files(
        _initiative_folders(), dropbox_connector=connector, use_cache=False
    )
    assert [r.name for r in refs] == ["brief.pdf"]
    # No kind-skip. The only note is the drive side reporting its unset
    # folder (initiatives read both sources as enabled), never a skip of the
    # whole item.
    assert [n["source"] for n in notes] == ["drive"]


def test_list_files_still_skips_non_client_engagement():
    _clear_listing_cache()
    refs, notes = list_files(
        _initiative_folders(is_initiative=False), use_cache=False
    )
    assert refs == [] and notes == []


# ──────────────────────────────────────────────────────────────────────
#  Pipeline owner-column rebinding
# ──────────────────────────────────────────────────────────────────────


class _InsertRecordingTable:
    def __init__(self, client):
        self._client = client
        self._filters = {}
        self._mode = None

    def insert(self, payload):
        self._mode = ("insert", payload)
        return self

    def update(self, payload):
        self._mode = ("update", payload)
        return self

    def select(self, cols):
        self._mode = ("select", cols)
        return self

    def eq(self, col, val):
        self._filters[col] = val
        return self

    def in_(self, col, vals):
        self._filters[col] = tuple(vals)
        return self

    def order(self, col, desc=False):
        return self

    def limit(self, n):
        return self

    def execute(self):
        kind, payload = self._mode
        if kind == "insert":
            self._client.inserts.append(payload)
            return _Resp([{"id": "new-asset-1"}])
        if kind == "update":
            self._client.updates.append(
                {"payload": payload, "filters": dict(self._filters)}
            )
            return _Resp([])
        # select — the dedup lookup
        self._client.selects.append(dict(self._filters))
        rows = [
            r
            for r in self._client.rows
            if all(
                (r.get(c) in v if isinstance(v, tuple) else r.get(c) == v)
                for c, v in self._filters.items()
                if c in r
            )
        ]
        return _Resp(rows)


class _InsertRecordingClient:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.inserts = []
        self.updates = []
        self.selects = []

    def table(self, name):
        assert name == "rag_assets"
        return _InsertRecordingTable(self)


def _real_shaped_pipeline(client):
    """A pipeline shaped like the real one at the two rebound seams."""
    return SimpleNamespace(
        storage=SimpleNamespace(client=client, create_asset=None),
        deduplication=SimpleNamespace(client=client, check_asset=None),
    )


def test_adapt_rebinds_create_asset_to_initiative_owner():
    client = _InsertRecordingClient()
    pipeline = _real_shaped_pipeline(client)
    _adapt_pipeline_for_initiative(pipeline, "init-1")
    asset_id = pipeline.storage.create_asset(
        project_id="IGNORED",
        source_type="pdf",
        title="Brief",
        url=None,
        file_path="/tmp/x/brief.pdf",
        file_hash="h1",
        metadata={"k": "v"},
    )
    assert asset_id == "new-asset-1"
    assert len(client.inserts) == 1
    row = client.inserts[0]
    assert row["initiative_id"] == "init-1"
    # Exactly-one-owner CHECK (migration 081): project_id must be ABSENT.
    assert "project_id" not in row
    assert row["file_hash"] == "h1" and row["meta"] == {"k": "v"}


def test_adapt_create_asset_supersedes_prev_version():
    client = _InsertRecordingClient()
    pipeline = _real_shaped_pipeline(client)
    _adapt_pipeline_for_initiative(pipeline, "init-1")
    pipeline.storage.create_asset(
        project_id="IGNORED",
        source_type="pdf",
        title="Brief",
        url=None,
        file_path="/tmp/x/brief.pdf",
        file_hash="h2",
        metadata={},
        prev_asset_id="old-1",
    )
    assert client.updates == [
        {"payload": {"status": "superseded"}, "filters": {"id": "old-1"}}
    ]


def test_adapt_check_asset_filters_on_initiative_id():
    client = _InsertRecordingClient(
        rows=[
            {
                "id": "a1",
                "initiative_id": "init-1",
                "file_path": "/tmp/x/brief.pdf",
                "file_hash": "same",
                "status": "active",
            }
        ]
    )
    pipeline = _real_shaped_pipeline(client)
    _adapt_pipeline_for_initiative(pipeline, "init-1")
    # Positional call, exactly as ingest.pipeline.ingest_file does.
    decision = pipeline.deduplication.check_asset(
        "IGNORED", "/tmp/x/brief.pdf", "same"
    )
    assert decision.action == "skip"
    assert client.selects[-1]["initiative_id"] == "init-1"
    assert "project_id" not in client.selects[-1]

    changed = pipeline.deduplication.check_asset(
        "IGNORED", "/tmp/x/brief.pdf", "different"
    )
    assert changed.action == "new_version"
    assert changed.existing_asset_id == "a1"

    fresh = pipeline.deduplication.check_asset("IGNORED", "/tmp/new.pdf", "h")
    assert fresh.action == "new"


def test_adapt_noops_on_a_fake_pipeline_without_seams():
    # An injected test-fake pipeline has no storage/deduplication; the adapt
    # must not blow up on it (such fakes never hit the real table anyway).
    _adapt_pipeline_for_initiative(SimpleNamespace(), "init-1")


# ──────────────────────────────────────────────────────────────────────
#  Run loop end-to-end (initiative owner pair on reads + stamp)
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


def test_initiative_run_stamps_by_initiative_owner(tmp_path, monkeypatch):
    _clear_listing_cache()
    client = _FakeClient(
        initiatives=[_initiative_row()],
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
        "storyos",
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
    # The scope stamp landed filtered on the INITIATIVE owner column.
    stamp = client.updates[-1]
    assert stamp["filters"]["initiative_id"] == "init-1"
    assert "project_id" not in stamp["filters"]
    assert stamp["payload"]["company_id"] == "co-canonic"
    # The skip/dedup pre-reads also keyed on the initiative owner column.
    eqs = client.rag_recorder.get("eq", [])
    assert ("initiative_id", "init-1") in eqs
    assert not any(col == "project_id" for col, _ in eqs)


def test_unconfigured_initiative_run_short_circuits_with_reason():
    _clear_listing_cache()
    client = _FakeClient(initiatives=[_initiative_row()], bindings=[])
    run = ingest_project_assets(
        "storyos",
        client=client,
        supabase_url="http://fake",
        supabase_key="fake",
    )
    assert run.project_found is True
    assert run.unconfigured_reason is not None
    assert "enabled but folder not set" in run.unconfigured_reason
    assert run.created == 0 and client.updates == []


def test_fan_out_runs_initiative_codes(monkeypatch):
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
        object(), ["ibx-5153", "storyos", "mission-control"]
    )
    assert seen == ["ibx-5153", "storyos", "mission-control"]
    assert result.total_created == 3
