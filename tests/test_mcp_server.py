"""Tests for the local stdio MCP server (Task 4).

The tool layer is WIRING ONLY: resolve code -> ids, call the pure function,
return its result. These tests exercise the tool FUNCTIONS directly (no real
stdio transport, no real Supabase) and assert that:

  - the tools delegate to the pure functions with the resolved ids,
  - a None resolution degrades gracefully (no crash),
  - exactly the two expected tools are registered,
  - importing the module stays light (no config / supabase at import time).
"""
from __future__ import annotations

import sys

import cp_engine.mcp_server as srv


def test_list_project_sources_delegates(monkeypatch):
    """Resolves ids, then returns the pure fn's result unchanged."""
    fake_client = object()
    monkeypatch.setattr(srv, "_resolve", lambda code: (fake_client, "pid", "cid"))

    captured = {}

    def fake_list_sources(client, project_id, company_id):
        captured["args"] = (client, project_id, company_id)
        return [{"id": "a1", "title": "Doc One", "source_type": "gdoc"}]

    monkeypatch.setattr(
        "cp_engine.project_sources.list_sources", fake_list_sources
    )

    out = srv.list_project_sources("IBX-5153")

    assert captured["args"] == (fake_client, "pid", "cid")
    assert out == [{"id": "a1", "title": "Doc One", "source_type": "gdoc"}]


def test_pull_project_source_delegates(monkeypatch):
    """Passes title + query through to the pure fn and returns its result.

    A query-ranked pull first resolves the ingest creds (Voyage) so the embed
    works; that resolution is stubbed here."""
    fake_client = object()
    monkeypatch.setattr(srv, "_resolve", lambda code: (fake_client, "pid", "cid"))
    monkeypatch.setattr(srv, "_tenant_root", lambda: "/tenant")
    monkeypatch.setattr("cp_engine.config.load", lambda root: object())
    creds_loaded = {}
    monkeypatch.setattr(
        "cp_engine.sync_mc2._load_ingest_creds",
        lambda config: creds_loaded.setdefault("called", True),
    )

    captured = {}

    def fake_pull_source(client, project_id, company_id, doc_title, query=None):
        captured["args"] = (client, project_id, company_id, doc_title, query)
        return {"title": doc_title, "chunks": ["c1", "c2"]}

    monkeypatch.setattr("cp_engine.project_sources.pull_source", fake_pull_source)

    out = srv.pull_project_source("IBX-5153", "Storybook", query="risks")

    assert captured["args"] == (fake_client, "pid", "cid", "Storybook", "risks")
    assert out == {"title": "Storybook", "chunks": ["c1", "c2"]}
    # a query-ranked pull loaded the ingest (Voyage) creds first
    assert creds_loaded.get("called") is True


def test_list_project_sources_unresolved_returns_note(monkeypatch):
    """An unresolvable code yields a structured note, not a bare [] (which would
    be indistinguishable from a project that genuinely has no sources)."""
    monkeypatch.setattr(srv, "_resolve", lambda code: None)
    out = srv.list_project_sources("nope")
    assert len(out) == 1 and "nope" in out[0]["note"]


def test_pull_project_source_unresolved_returns_note(monkeypatch):
    """An unresolvable code yields a not-found note, not a crash."""
    monkeypatch.setattr(srv, "_resolve", lambda code: None)
    out = srv.pull_project_source("nope", "SomeDoc")
    assert out["title"] == "SomeDoc"
    assert out["chunks"] == []
    assert "not found" in out["note"]


def test_list_project_sources_resolve_raises_returns_error(monkeypatch):
    """A raising _resolve (e.g. bad config / no creds) yields a structured error."""
    def boom(code):
        raise RuntimeError("supabase creds missing")

    monkeypatch.setattr(srv, "_resolve", boom)

    out = srv.list_project_sources("IBX-5153")
    assert isinstance(out, list) and len(out) == 1
    assert "IBX-5153" in out[0]["error"]
    assert "supabase creds missing" in out[0]["error"]


def test_pull_project_source_resolve_raises_returns_error(monkeypatch):
    """A raising _resolve yields a structured error, not a propagation."""
    def boom(code):
        raise RuntimeError("supabase creds missing")

    monkeypatch.setattr(srv, "_resolve", boom)

    out = srv.pull_project_source("IBX-5153", "Storybook")
    assert out["title"] == "Storybook"
    assert out["chunks"] == []
    assert "Storybook" in out["error"]
    assert "IBX-5153" in out["error"]
    assert "supabase creds missing" in out["error"]


def test_list_project_sources_pure_fn_raises_returns_error(monkeypatch):
    """A raising pure fn (RPC error) is caught and returned as a structured error."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))

    def boom(client, project_id, company_id):
        raise RuntimeError("rpc failed")

    monkeypatch.setattr("cp_engine.project_sources.list_sources", boom)

    out = srv.list_project_sources("IBX-5153")
    assert isinstance(out, list) and len(out) == 1
    assert "IBX-5153" in out[0]["error"]
    assert "rpc failed" in out[0]["error"]


def test_pull_project_source_pure_fn_raises_returns_error(monkeypatch):
    """A raising pure fn (Voyage embedding error) is caught and returned structured."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))
    monkeypatch.setattr(srv, "_tenant_root", lambda: "/tenant")
    monkeypatch.setattr("cp_engine.config.load", lambda root: object())
    monkeypatch.setattr("cp_engine.sync_mc2._load_ingest_creds", lambda config: None)

    def boom(client, project_id, company_id, doc_title, query=None):
        raise RuntimeError("voyage embed failed")

    monkeypatch.setattr("cp_engine.project_sources.pull_source", boom)

    out = srv.pull_project_source("IBX-5153", "Storybook", query="risks")
    assert out["title"] == "Storybook"
    assert out["chunks"] == []
    assert "Storybook" in out["error"]
    assert "voyage embed failed" in out["error"]


def test_tenant_root_walks_up_from_subdir(tmp_path, monkeypatch):
    """The server resolves the tenant root by walking UP from cwd.

    Claude Code launches `cp mcp` with its cwd set to whatever dir the session
    opened in — often a project subdir like `1p/infoblox/ibx-5153-ai-campaign`,
    not the tenant root. The tenant config (`.cp-engine.toml`) lives only at the
    root, so resolving the root from cwd must ascend until it finds the config.
    """
    (tmp_path / ".cp-engine.toml").write_text("# tenant\n")
    subdir = tmp_path / "1p" / "infoblox" / "ibx-5153-ai-campaign"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)

    assert srv._tenant_root() == tmp_path.resolve()


def test_tenant_root_uses_cwd_when_no_config_found(tmp_path, monkeypatch):
    """With no `.cp-engine.toml` in any ancestor, fall back to cwd.

    The downstream config load then raises its own clear NotATenantRepo error;
    `_tenant_root` itself must not crash on a non-tenant cwd.
    """
    monkeypatch.chdir(tmp_path)
    assert srv._tenant_root() == tmp_path.resolve()


def test_exactly_thirty_seven_tools_registered():
    """4 source-read + 1 dropbox-write + 2 spine-read + 13 spine-write +
    2 spine-relation-write + 5 spine-step-write + 1 spine-promote +
    1 meetings-read + 3 framework + 3 commitment + 1 note tool.

    source-read grew 3→4 (#108 pull_document_comments); spine-write grew 8→11
    (#104 add/remove_element_provenance, #105 retire_spine_elements); create_note
    added (#107); spine-step-write added 4 (#119 add/set/reorder/remove_spine_step);
    then +4 (#120): push_to_dropbox (rich-doc write-back), add_spine_document
    (file/source → element), pull_element_from_project (cross-project copy +
    account-tag + lineage), set_element_account_scope (type-agnostic promote);
    then +1 (auto-journey-steps): propose_spine_step (machine-authored, proposed
    review-state)."""
    names = {t.name for t in srv.mcp._tool_manager.list_tools()}
    assert names == {
        "list_project_sources",
        "pull_project_source",
        "fetch_project_source",
        "pull_document_comments",
        "push_to_dropbox",
        "list_spine_elements",
        "pull_spine_element",
        "create_spine_element",
        "add_spine_document",
        "add_spine_version",
        "set_spine_element",
        "set_element_account_scope",
        "pull_element_from_project",
        "add_element_source",
        "remove_element_source",
        "add_element_provenance",
        "remove_element_provenance",
        "retire_spine_element",
        "retire_spine_elements",
        "create_spine_relation",
        "retire_spine_relation",
        "add_spine_step",
        "propose_spine_step",
        "set_spine_step",
        "reorder_spine_step",
        "remove_spine_step",
        "promote_stakeholder",
        "demote_stakeholder",
        "promote_spine_transcript",
        "framework_readiness",
        "framework_decompose",
        "framework_compose",
        "list_project_meetings",
        "create_commitment",
        "list_commitments",
        "resolve_commitment",
        "create_note",
    }


# ---------------------------------------------------------------------------
# Spine read tools (list_spine_elements / pull_spine_element)
# ---------------------------------------------------------------------------


def test_list_spine_elements_delegates(monkeypatch):
    """Resolves ids, delegates to list_spine with the project_id, returns result."""
    fake_client = object()
    monkeypatch.setattr(srv, "_resolve", lambda code: (fake_client, "pid", "cid"))

    captured = {}

    def fake_list_spine(client, project_id, company_id=None, *,
                        layer=None, scope=None, binding=None):
        captured["args"] = (client, project_id)
        captured["filters"] = (layer, scope, binding)
        return [{"est_item_id": "_authored/brief", "framing": "Brief"}]

    monkeypatch.setattr("cp_engine.project_sources.list_spine", fake_list_spine)

    out = srv.list_spine_elements("sap-5171")

    assert captured["args"] == (fake_client, "pid")
    # empty-string filter args normalize to None (no filtering)
    assert captured["filters"] == (None, None, None)
    assert out == [{"est_item_id": "_authored/brief", "framing": "Brief"}]


def test_pull_spine_element_delegates(monkeypatch):
    """Passes the key through to pull_spine and returns its result."""
    fake_client = object()
    monkeypatch.setattr(srv, "_resolve", lambda code: (fake_client, "pid", "cid"))

    captured = {}

    def fake_pull_spine(client, project_id, key, company_id=None):
        captured["args"] = (client, project_id, key)
        return {"est_item_id": key, "body": "full text"}

    monkeypatch.setattr("cp_engine.project_sources.pull_spine", fake_pull_spine)

    out = srv.pull_spine_element("sap-5171", "_authored/email")

    assert captured["args"] == (fake_client, "pid", "_authored/email")
    assert out == {"est_item_id": "_authored/email", "body": "full text"}


def test_list_spine_elements_unresolved_returns_note(monkeypatch):
    """An unresolvable code yields a structured note, NOT a bare [] — the
    v0.39.0 false-negative where an unresolvable code looked like an empty spine.
    """
    monkeypatch.setattr(srv, "_resolve", lambda code: None)
    out = srv.list_spine_elements("nope")
    assert len(out) == 1 and "nope" in out[0]["note"]


def test_pull_spine_element_unresolved_returns_error(monkeypatch):
    """An unresolvable code yields a not-found error, not a crash."""
    monkeypatch.setattr(srv, "_resolve", lambda code: None)
    out = srv.pull_spine_element("nope", "_authored/x")
    assert out["body"] == ""
    assert "not found" in out["error"]


def test_list_spine_elements_resolve_raises_returns_error(monkeypatch):
    """A raising _resolve yields a structured error, not a propagation."""

    def boom(_code):
        raise RuntimeError("no creds")

    monkeypatch.setattr(srv, "_resolve", boom)
    out = srv.list_spine_elements("sap-5171")
    assert "error" in out[0]


def test_pull_spine_element_pure_fn_raises_returns_error(monkeypatch):
    """A raising pure fn is caught and returned as a structured error note."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))

    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr("cp_engine.project_sources.pull_spine", boom)
    out = srv.pull_spine_element("sap-5171", "_authored/x")
    assert "error" in out


# ---------------------------------------------------------------------------
# Meetings read tool (list_project_meetings)
# ---------------------------------------------------------------------------


def test_list_project_meetings_delegates(monkeypatch):
    """Resolves ids, delegates to the helper with the project_id, returns it."""
    fake_client = object()
    monkeypatch.setattr(srv, "_resolve", lambda code: (fake_client, "pid", "cid"))

    captured = {}

    def fake_helper(client, project_id):
        captured["args"] = (client, project_id)
        return [{"recording_id": "rec-1", "summary_embedded": True}]

    monkeypatch.setattr(
        "cp_engine.project_sources.list_project_meetings", fake_helper
    )

    out = srv.list_project_meetings("sap-5171")

    assert captured["args"] == (fake_client, "pid")
    assert out == [{"recording_id": "rec-1", "summary_embedded": True}]


def test_list_project_meetings_unresolved_returns_note(monkeypatch):
    """An unresolvable code yields a structured note, NOT a bare [] — the
    v0.39.0 false-negative where an unresolvable code looked like empty."""
    monkeypatch.setattr(srv, "_resolve", lambda code: None)
    out = srv.list_project_meetings("nope")
    assert len(out) == 1 and "nope" in out[0]["note"]


def test_list_project_meetings_pure_fn_raises_returns_error(monkeypatch):
    """A raising helper is caught and returned as a structured error note."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))

    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr("cp_engine.project_sources.list_project_meetings", boom)
    out = srv.list_project_meetings("sap-5171")
    assert "error" in out[0]


# ---------------------------------------------------------------------------
# Spine write tools (create_spine_element / add_spine_version)
# ---------------------------------------------------------------------------


class _FakeWriteQuery:
    """A chainable query that records upserts/updates and serves seeded selects.

    Supports the chains the write tools use:
      - .select(...).eq(...).eq(...).limit(...).execute()  (create existing-check)
      - .select(...).eq(...).execute()                     (version prior-fetch)
      - .upsert(rows, on_conflict=...).execute()           (write)
      - .update({...}).eq(...).execute()                   (demote prior live)
    Selects APPLY their `.eq()` filters against `select_rows` (so tests can
    prove a query filters by the right column) — the earlier fake ignored
    `.eq()`, which is exactly why the project_code-vs-project_id resolver bug
    slipped through.
    """

    def __init__(self, table, client):
        self._table = table
        self._client = client
        self._mode = None  # "select" | "upsert" | "update"
        self._payload = None
        self._filters: list[tuple[str, object]] = []

    def select(self, *_a, **_k):
        self._mode = "select"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def limit(self, _n):
        return self

    def upsert(self, rows, on_conflict=None):
        self._mode = "upsert"
        self._payload = rows
        self._client.upserts.append((self._table, rows, on_conflict))
        return self

    def update(self, patch):
        self._mode = "update"
        self._payload = patch
        return self

    def execute(self):
        if self._mode == "update":
            self._client.updates.append((self._table, self._payload))
            return type("R", (), {"data": []})()
        if self._mode == "upsert":
            return type("R", (), {"data": list(self._payload)})()
        # select — apply the recorded eq() filters against the seeded rows.
        rows = [
            r for r in self._client.select_rows
            if all(r.get(c) == v for c, v in self._filters)
        ]
        return type("R", (), {"data": rows})()


class _FakeWriteClient:
    def __init__(self, select_rows=None):
        self.select_rows = select_rows or []
        self.upserts = []  # list of (table, rows, on_conflict)
        self.updates = []  # list of (table, patch)

    def table(self, name):
        return _FakeWriteQuery(name, self)


def test_create_spine_element_writes_authored_v1(monkeypatch):
    """Creates a live v1 authored row and writes it via upsert."""
    client = _FakeWriteClient(select_rows=[])  # existing-check finds nothing
    monkeypatch.setattr(srv, "_resolve", lambda code: (client, "pid", "cid"))

    out = srv.create_spine_element("ibx-5192", "Email from Janet", "email", "Hi", [])

    assert out == {"element_id": "_authored/email-from-janet", "version_label": "v1"}
    assert len(client.upserts) == 1
    table, rows, _ = client.upserts[0]
    assert table == "spine_substance"
    row = rows[0]
    assert row["origin"] == "authored"
    assert row["project_code"] == "ibx-5192"
    assert row["status"] == "live"
    assert row["version_label"] == "v1"


def test_create_spine_element_conflict(monkeypatch):
    """An existing element with the same slug → error, no clobbering upsert."""
    client = _FakeWriteClient(select_rows=[{
        "id": "ibx-5192/_authored/email-from-janet/v1",
        "project_id": "pid",
        "est_item_id": "_authored/email-from-janet",
    }])
    monkeypatch.setattr(srv, "_resolve", lambda code: (client, "pid", "cid"))

    out = srv.create_spine_element("ibx-5192", "Email from Janet", "email", "Hi", [])

    assert "already exists" in out["error"]
    assert client.upserts == []


def test_create_spine_element_unresolved(monkeypatch):
    """An unresolvable code → structured error containing 'not found'."""
    monkeypatch.setattr(srv, "_resolve", lambda code: None)
    out = srv.create_spine_element("nope", "X", "note")
    assert "not found" in out["error"]


def _prior_v1(**over):
    """A live v1 authored row. `project_id`/`project_code` default to the pair
    the resolver hands the write tool (pid) vs. the SLUG stored on the row —
    deliberately different from the short code a caller usually types."""
    row = {
        "id": "ibx-5153/_authored/hyp/v1",
        "est_item_id": "_authored/hyp",
        "est_item_kind": None,
        "phase": None,
        "binding": "unbound",
        "layer": "note",
        "placement": "context",
        "serves": [],
        "version_label": "v1",
        "version_date": "2026-06-18",
        "status": "live",
        "framing": "Latest hypothesis",
        "body": "old",
        "sources": [],
        "origin": "authored",
        "project_id": "pid",
        "project_code": "ibx-5153-ai-campaign",
    }
    row.update(over)
    return row


def test_add_spine_version(monkeypatch):
    """Demotes the prior live v1 then upserts a new live v2 with the note."""
    client = _FakeWriteClient(select_rows=[_prior_v1()])
    monkeypatch.setattr(srv, "_resolve", lambda code: (client, "pid", "cid"))

    out = srv.add_spine_version("ibx-5153", "_authored/hyp", "new", "changed")

    assert out == {"element_id": "_authored/hyp", "version_label": "v2"}
    # prior live v1 demoted via targeted update
    assert client.updates == [("spine_substance", {"status": "superseded"})]
    # new live v2 upserted carrying the version_note
    assert len(client.upserts) == 1
    _, rows, _ = client.upserts[0]
    assert len(rows) == 1
    row = rows[0]
    assert row["version_label"] == "v2"
    assert row["status"] == "live"
    assert row["version_note"] == "changed"
    assert row["body"] == "new"
    # New rows carry the element's OWN stored slug, not the caller's short code.
    assert row["project_code"] == "ibx-5153-ai-campaign"


def test_add_spine_version_resolves_by_project_id_not_code(monkeypatch):
    """REGRESSION: caller passes `ibx-5153`, the row stores the full slug
    `ibx-5153-ai-campaign`. The old code filtered `.eq("project_code", <short>)`
    and returned 'no authored element' despite a resolvable project + element.
    The fix filters by project_id (the resolved UUID)."""
    client = _FakeWriteClient(select_rows=[_prior_v1()])
    # _resolve maps EITHER code form to the same project_id 'pid'.
    monkeypatch.setattr(srv, "_resolve", lambda code: (client, "pid", "cid"))

    out = srv.add_spine_version("ibx-5153", "_authored/hyp", "new", "changed")

    assert "error" not in out
    assert out == {"element_id": "_authored/hyp", "version_label": "v2"}


def test_add_spine_version_by_framing_substring(monkeypatch):
    """The key may be a framing (title) substring, like pull_spine_element."""
    client = _FakeWriteClient(select_rows=[_prior_v1()])
    monkeypatch.setattr(srv, "_resolve", lambda code: (client, "pid", "cid"))

    out = srv.add_spine_version("ibx-5153", "latest hypothesis", "new", "changed")

    assert out == {"element_id": "_authored/hyp", "version_label": "v2"}


def test_add_spine_version_picks_next_number_across_history(monkeypatch):
    """Prior versions of any status are fetched, so the new label is v3 when a
    superseded v1 and a live v2 already exist."""
    client = _FakeWriteClient(select_rows=[
        _prior_v1(id="a/v1", version_label="v1", status="superseded", body="oldest"),
        _prior_v1(id="a/v2", version_label="v2", status="live", body="current"),
    ])
    monkeypatch.setattr(srv, "_resolve", lambda code: (client, "pid", "cid"))

    out = srv.add_spine_version("ibx-5153", "_authored/hyp", "newer", "changed")

    assert out["version_label"] == "v3"
    # Only the LIVE v2 gets demoted (one update), not the already-superseded v1.
    assert client.updates == [("spine_substance", {"status": "superseded"})]


def test_add_spine_version_unknown_element(monkeypatch):
    """No prior versions for the element → error, no writes."""
    client = _FakeWriteClient(select_rows=[])
    monkeypatch.setattr(srv, "_resolve", lambda code: (client, "pid", "cid"))

    out = srv.add_spine_version("ibx-5153", "_authored/missing", "new", "changed")

    assert "no authored element" in out["error"]
    assert client.upserts == []
    assert client.updates == []


def test_create_spine_element_conflict_detected_across_code_forms(monkeypatch):
    """REGRESSION twin: the collision guard scopes by project_id, so an existing
    element is detected even when the row stores a different code slug than the
    caller passes — the old project_code filter would MISS it and clobber."""
    existing = _prior_v1(
        est_item_id="_authored/email-from-janet",
        project_code="ibx-5153-ai-campaign",
    )
    client = _FakeWriteClient(select_rows=[existing])
    monkeypatch.setattr(srv, "_resolve", lambda code: (client, "pid", "cid"))

    out = srv.create_spine_element("ibx-5153", "Email from Janet", "email", "Hi", [])

    assert "already exists" in out["error"]
    assert client.upserts == []


class _FakeQuery:
    """Records filters and returns seeded rows for a (table, filters) key.

    `eq` is case-sensitive (mirrors PostgREST). `ilike` is case-insensitive:
    its filter is keyed as ``(col, "ilike", value.lower())`` so a seeded row's
    company code (`IBX`) matches a lowercased lookup pattern — the real bug was
    using a case-sensitive `eq` against an UPPERCASE `companies.code`.
    """

    def __init__(self, table, store, log):
        self._table = table
        self._store = store
        self._log = log
        self._filters = set()

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters.add((col, val))
        return self

    def ilike(self, col, val):
        self._filters.add((col, "ilike", str(val).lower()))
        return self

    def limit(self, _n):
        return self

    def execute(self):
        key = (self._table, frozenset(self._filters))
        self._log.append(key)
        rows = self._store.get(key, [])
        return type("R", (), {"data": list(rows)})()


class _FakeClient:
    def __init__(self, store):
        self._store = store
        self.log = []

    def table(self, name):
        return _FakeQuery(name, self._store, self.log)


def test_resolve_project_id_by_exact_code():
    """A real slug code resolves directly via projects.code."""
    store = {
        ("projects", frozenset([("code", "IBX-platform-sales-readiness-summit")])): [
            {"id": "pid-slug"}
        ],
    }
    client = _FakeClient(store)
    assert srv._resolve_project_id(client, "IBX-platform-sales-readiness-summit") == "pid-slug"


def test_resolve_project_id_by_raw_full_job_name():
    """A RAW full_job_name (the display form, e.g. "IBX 5167 DDI Platform Video")
    resolves directly via projects.full_job_name.

    Fathom stores this exact display string in fathom_meetings.project_tags, so
    the meetings flow passes it to the resolver verbatim — it is neither the
    slug code (`IBX-ddi-platform-video`) nor the slugified on-disk id
    (`ibx-5167-ddi-platform-video`), so without this branch every tagged meeting
    fails to resolve and the backfill links nothing.
    """
    store = {
        # exact-code lookup MISSES (the raw display name is not the code)
        ("projects", frozenset([("code", "IBX 5167 DDI Platform Video")])): [],
        # NEW branch: exact full_job_name match
        ("projects", frozenset([("full_job_name", "IBX 5167 DDI Platform Video")])): [
            {"id": "pid-5167"}
        ],
    }
    client = _FakeClient(store)
    assert (
        srv._resolve_project_id(client, "IBX 5167 DDI Platform Video") == "pid-5167"
    )


def test_resolve_project_id_falls_back_to_company_prefix_and_number():
    """The working-dir form `ibx-5192` (company prefix + number) resolves to the
    project even though projects.code is the slug `IBX-platform-sales-readiness-summit`.

    This is the bridge: cp-engine's synthesized <company>-<number> id is NOT the
    canonical projects.code, so an exact-code lookup misses; we fall back to
    matching companies.code (case-insensitive) + projects.number.
    """
    store = {
        # exact-code lookup MISSES (ibx-5192 is not a real code)
        ("projects", frozenset([("code", "ibx-5192")])): [],
        # company prefix resolves CASE-INSENSITIVELY: stored code is UPPERCASE
        # `IBX`, the working-dir prefix is lowercase `ibx` — must match via ilike.
        ("companies", frozenset([("code", "ilike", "ibx")])): [{"id": "co-ibx"}],
        # company_id + number resolves to the project
        ("projects", frozenset([("company_id", "co-ibx"), ("number", 5192)])): [
            {"id": "pid-5192"}
        ],
    }
    client = _FakeClient(store)
    assert srv._resolve_project_id(client, "ibx-5192") == "pid-5192"


def test_resolve_project_id_by_full_job_name_slug():
    """The canonical on-disk id `ibx-5153-ai-campaign` resolves even though
    projects.code is `IBX-ai-campaign` and the number 5153 lives only in the
    middle of `full_job_name` ("IBX 5153 AI Campaign").

    Since v0.35.0 `code = slug_full_job_name(full_job_name)` is the canonical id
    that cp.md Facts, the working-dir name, and CLAUDE.md all use — so it's the
    natural thing a caller passes. It matches neither the exact-code branch nor
    the legacy `<prefix>-<number>` bridge (the number isn't the trailing
    segment), so a third branch must reverse `slug_full_job_name`: scan the
    company-prefixed candidate rows and compare the slugified full_job_name.
    """
    store = {
        # exact-code lookup MISSES (the on-disk slug is not the real code)
        ("projects", frozenset([("code", "ibx-5153-ai-campaign")])): [],
        # the new branch scans company-prefixed candidates by full_job_name
        ("projects", frozenset([("code", "ilike", "ibx-%")])): [
            {"id": "pid-other", "full_job_name": "IBX Something Else"},
            {"id": "pid-5153", "full_job_name": "IBX 5153 AI Campaign"},
        ],
    }
    client = _FakeClient(store)
    assert srv._resolve_project_id(client, "ibx-5153-ai-campaign") == "pid-5153"


def test_resolve_project_id_full_job_name_slug_no_match_falls_through():
    """When no candidate's slugified full_job_name matches, the branch yields no
    false positive and resolution falls through to None (not the wrong project)."""
    store = {
        ("projects", frozenset([("code", "ibx-5153-ai-campaign")])): [],
        ("projects", frozenset([("code", "ilike", "ibx-%")])): [
            {"id": "pid-other", "full_job_name": "IBX Something Else"},
        ],
    }
    client = _FakeClient(store)
    assert srv._resolve_project_id(client, "ibx-5153-ai-campaign") is None


def test_resolve_project_id_falls_back_to_initiative_code():
    """A slug initiative code (`mission-control`) resolves via the initiatives
    table when it matches no project.

    Initiatives live in their OWN table, not `projects`; without this fallback
    every cp-sources tool returns empty for an initiative code because both the
    exact-`projects.code` lookup and the `<prefix>-<number>` bridge miss. The
    initiative's id goes into spine_substance.project_id exactly like a project's.
    """
    store = {
        # not a project, and `mission-control` has no trailing number to bridge
        ("projects", frozenset([("code", "mission-control")])): [],
        # resolves via initiatives.code
        ("initiatives", frozenset([("code", "mission-control")])): [
            {"id": "init-mc"}
        ],
    }
    client = _FakeClient(store)
    assert srv._resolve_project_id(client, "mission-control") == "init-mc"


def test_resolve_project_id_unknown_returns_none():
    """A code that matches nothing (and isn't a company-number form) → None."""
    client = _FakeClient({})
    assert srv._resolve_project_id(client, "totally-unknown") is None


def test_resolve_initiative_tolerates_missing_folders(monkeypatch):
    """`_resolve` returns (client, initiative_id, None) for an initiative.

    Initiatives have no Drive/Dropbox folders, so resolve_project_folders_by_id
    (a projects-table lookup keyed by id) returns None for an initiative id.
    Pre-fix, `_resolve` treated that None as 'unresolvable' and returned None,
    making every spine tool empty for an initiative even though its id resolved
    fine. The spine tools only need project_id (company_id is unused), so
    `_resolve` must degrade to a None company_id, not bail.
    """
    fake_client = object()
    monkeypatch.setattr(srv, "_tenant_root", lambda: "/tenant")
    monkeypatch.setattr(
        "cp_engine.config.load", lambda root: object()
    )
    monkeypatch.setattr(
        "cp_engine.mc2_db.get_client", lambda config=None, **kw: fake_client
    )
    monkeypatch.setattr(srv, "_resolve_project_id", lambda client, code: "init-mc")
    # initiative id has no project row → folders resolve to None
    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders_by_id",
        lambda client, pid: None,
    )

    resolved = srv._resolve("mission-control")
    assert resolved == (fake_client, "init-mc", None)


def test_module_import_is_light():
    """Importing the server must NOT pull in config / supabase at module load.

    The config + supabase imports live inside the tool bodies on purpose, so a
    fresh `import cp_engine.mcp_server` leaves them out of sys.modules.
    """
    evicted = ("cp_engine.mcp_server", "cp_engine.sync_mc2", "cp_engine.cli")
    # Snapshot so we can RESTORE after asserting. Without this, evicting
    # cp_engine.sync_mc2 leaves a later test's `monkeypatch.setattr(
    # "cp_engine.sync_mc2.<fn>", ...)` patching a different module object than
    # the one a tool body re-imports — silent cross-test pollution.
    saved = {mod: sys.modules.get(mod) for mod in evicted}
    try:
        for mod in evicted:
            sys.modules.pop(mod, None)

        import cp_engine.mcp_server  # noqa: F401  (re-import after eviction)

        assert "cp_engine.mcp_server" in sys.modules
        assert "cp_engine.sync_mc2" not in sys.modules
        assert "cp_engine.cli" not in sys.modules
    finally:
        for mod, obj in saved.items():
            if obj is not None:
                sys.modules[mod] = obj
            else:
                sys.modules.pop(mod, None)


# ---------------------------------------------------------------------------
# Commitment tools (create_commitment / list_commitments / resolve_commitment)
# ---------------------------------------------------------------------------


def _scope(kind="project"):
    return {"id": "own-1", "code": "ggl-5168", "kind": kind}


def test_commitment_scope_initiative_wins(monkeypatch):
    """Initiatives are checked FIRST so _resolve_project_id's own initiative
    fallback can never mislabel one as a project."""
    monkeypatch.setattr(srv, "_resolve_initiative_id", lambda c, code: "init-1")
    monkeypatch.setattr(
        srv, "_resolve_project_id",
        lambda c, code: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    scope = srv._commitment_scope(object(), "mission-control")
    assert scope == {"id": "init-1", "code": "mission-control", "kind": "initiative"}


def test_commitment_scope_project_after_initiative_miss(monkeypatch):
    monkeypatch.setattr(srv, "_resolve_initiative_id", lambda c, code: None)
    monkeypatch.setattr(srv, "_resolve_project_id", lambda c, code: "proj-1")
    scope = srv._commitment_scope(object(), "ggl-5168")
    assert scope == {"id": "proj-1", "code": "ggl-5168", "kind": "project"}


def test_commitment_scope_unresolved(monkeypatch):
    monkeypatch.setattr(srv, "_resolve_initiative_id", lambda c, code: None)
    monkeypatch.setattr(srv, "_resolve_project_id", lambda c, code: None)
    assert srv._commitment_scope(object(), "cp-engine") is None


def test_create_commitment_delegates(monkeypatch):
    fake_client = object()
    monkeypatch.setattr(srv, "_resolve_commitments",
                        lambda code: (fake_client, _scope()))
    captured = {}

    def fake_write(client, **kwargs):
        captured["client"] = client
        captured.update(kwargs)
        return "inserted"

    monkeypatch.setattr("cp_engine.commitments.write_commitment", fake_write)
    out = srv.create_commitment(
        "ggl-5168", "  Deliver the grids  ", owner="drew@firstperson.is",
        due_date="2026-07-20", direction="us_to_them",
    )
    assert out["result"] == "inserted"
    assert out["kind"] == "project"
    assert captured["client"] is fake_client
    assert captured["description"] == "Deliver the grids"
    assert captured["direction"] == "us_to_them"
    assert captured["owner_email"] == "drew@firstperson.is"
    assert captured["owner_name"] is None
    assert captured["due_date"] == "2026-07-20"
    assert captured["source_kind"] == "session"
    assert captured["cp_hash"] == out["cp_hash"]


def test_create_commitment_name_owner(monkeypatch):
    monkeypatch.setattr(srv, "_resolve_commitments",
                        lambda code: (object(), _scope()))
    captured = {}
    monkeypatch.setattr(
        "cp_engine.commitments.write_commitment",
        lambda client, **kw: captured.update(kw) or "inserted",
    )
    srv.create_commitment("ggl-5168", "Deliver", owner="Marcello")
    assert captured["owner_name"] == "Marcello"
    assert captured["owner_email"] is None
    assert captured["direction"] == "internal"


def test_create_commitment_bad_direction():
    out = srv.create_commitment("ggl-5168", "Deliver", direction="sideways")
    assert "direction" in out["error"]


def test_create_commitment_bad_due_date():
    out = srv.create_commitment("ggl-5168", "Deliver", due_date="next Tuesday")
    assert "ISO" in out["error"]


def test_create_commitment_empty_description():
    out = srv.create_commitment("ggl-5168", "   ")
    assert "description" in out["error"]


def test_create_commitment_unresolved(monkeypatch):
    monkeypatch.setattr(srv, "_resolve_commitments", lambda code: (object(), None))
    out = srv.create_commitment("nope-1", "Deliver")
    assert "resolved to no" in out["error"]


def test_create_commitment_hash_uses_owner_id(monkeypatch):
    """Two code forms for the same project must produce the same cp_hash."""
    monkeypatch.setattr(
        srv, "_resolve_commitments",
        lambda code: (object(), {"id": "own-1", "code": code, "kind": "project"}),
    )
    monkeypatch.setattr("cp_engine.commitments.write_commitment",
                        lambda client, **kw: "inserted")
    a = srv.create_commitment("ibx-5153", "Deliver the thing")
    b = srv.create_commitment("ibx-5153-ai-campaign", "Deliver the thing")
    assert a["cp_hash"] == b["cp_hash"]


def test_list_commitments_delegates(monkeypatch):
    fake_client = object()
    monkeypatch.setattr(srv, "_resolve_commitments",
                        lambda code: (fake_client, _scope("initiative")))
    captured = {}

    def fake_list(client, owner, status="open"):
        captured["args"] = (client, owner, status)
        return [{"id": "c1"}]

    monkeypatch.setattr("cp_engine.commitments.list_commitments", fake_list)
    out = srv.list_commitments("mission-control", status="all")
    assert out == [{"id": "c1"}]
    assert captured["args"] == (fake_client, _scope("initiative"), "all")


def test_list_commitments_bad_status():
    out = srv.list_commitments("ggl-5168", status="pending")
    assert "status" in out[0]["error"]


def test_list_commitments_unresolved(monkeypatch):
    monkeypatch.setattr(srv, "_resolve_commitments", lambda code: (object(), None))
    out = srv.list_commitments("nope-1")
    assert "resolved to no" in out[0]["note"]


def test_resolve_commitment_delegates(monkeypatch):
    fake_client = object()
    monkeypatch.setattr(srv, "_resolve_commitments",
                        lambda code: (fake_client, _scope()))
    row = {"id": "c1", "description": "Deliver the grids"}
    monkeypatch.setattr("cp_engine.commitments.find_open_commitment",
                        lambda client, owner, key: (row, None))
    captured = {}
    monkeypatch.setattr(
        "cp_engine.commitments.close_commitment",
        lambda client, cid, outcome: captured.update(cid=cid, outcome=outcome),
    )
    out = srv.resolve_commitment("ggl-5168", "grids", outcome="done")
    assert out == {"resolved": "c1", "description": "Deliver the grids",
                   "outcome": "done"}
    assert captured == {"cid": "c1", "outcome": "done"}


def test_resolve_commitment_bad_outcome():
    out = srv.resolve_commitment("ggl-5168", "grids", outcome="deleted")
    assert "outcome" in out["error"]


def test_resolve_commitment_ambiguous_returns_error(monkeypatch):
    monkeypatch.setattr(srv, "_resolve_commitments",
                        lambda code: (object(), _scope()))
    monkeypatch.setattr("cp_engine.commitments.find_open_commitment",
                        lambda client, owner, key: (None, "2 open commitments match"))
    closed = []
    monkeypatch.setattr("cp_engine.commitments.close_commitment",
                        lambda *a: closed.append(a))
    out = srv.resolve_commitment("ggl-5168", "the")
    assert "match" in out["error"]
    assert not closed


def test_commitment_tools_resolve_raises_returns_error(monkeypatch):
    def boom(code):
        raise RuntimeError("no creds")

    monkeypatch.setattr(srv, "_resolve_commitments", boom)
    assert "no creds" in srv.create_commitment("ggl-5168", "Deliver")["error"]
    assert "no creds" in srv.list_commitments("ggl-5168")[0]["error"]
    assert "no creds" in srv.resolve_commitment("ggl-5168", "x")["error"]


# ---------------------------------------------------------------------------
# push_to_dropbox (#120 — rich-doc write-back)
# ---------------------------------------------------------------------------


def test_push_to_dropbox_unresolved(monkeypatch):
    """A code resolving to no project returns a structured error, never raises."""
    monkeypatch.setattr(srv, "_resolve", lambda code: None)
    out = srv.push_to_dropbox("nope-9999", "/tmp/deck.pptx")
    assert "not found" in out["error"]


def test_push_to_dropbox_no_folder_configured(monkeypatch):
    """A project with no Dropbox folder id gets a clear 'nowhere to push' error."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))

    class _Folders:
        mc_dropbox_folder_id = None

    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders_by_id",
        lambda client, pid: _Folders(),
    )
    out = srv.push_to_dropbox("ibx-5153", "/tmp/deck.pptx")
    assert "no Dropbox folder configured" in out["error"]


def test_push_to_dropbox_delegates(monkeypatch, tmp_path):
    """Resolves folder, loads creds, uploads via the connector, returns result."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))

    class _Folders:
        mc_dropbox_folder_id = "id:abc123"

    monkeypatch.setattr(
        "cp_engine.asset_ingest.resolve_project_folders_by_id",
        lambda client, pid: _Folders(),
    )
    monkeypatch.setattr("cp_engine.config.load", lambda root: object())
    creds_loaded = {}
    monkeypatch.setattr(
        "cp_engine.sync_mc2._load_dropbox_creds",
        lambda config: creds_loaded.setdefault("called", True),
    )
    # Stub the connector class the tool imports at runtime.
    import cloud_storage.dropbox_connector as dc

    monkeypatch.setattr(dc, "DropboxConnector", lambda: object())

    captured = {}

    def fake_push(connector, folder_id, local_path, dest_name=None, overwrite=False):
        captured.update(folder_id=folder_id, local_path=local_path,
                        dest_name=dest_name, overwrite=overwrite)
        return {"dropbox_path": "/Clients/x/deck.pptx", "name": "deck.pptx",
                "size": 10, "overwrote": overwrite}

    monkeypatch.setattr("cp_engine.project_sources.push_to_dropbox", fake_push)

    f = tmp_path / "deck.pptx"
    f.write_bytes(b"x" * 10)
    out = srv.push_to_dropbox("ibx-5153", str(f), overwrite=True)
    assert creds_loaded["called"] is True
    assert captured["folder_id"] == "id:abc123"
    assert captured["overwrite"] is True
    assert out["name"] == "deck.pptx"


# ---------------------------------------------------------------------------
# add_spine_document (#120 — file / ingested-source → element)
# ---------------------------------------------------------------------------


def test_add_spine_document_requires_exactly_one_source():
    """Neither file_path nor source_title, or both, is a usage error."""
    assert "exactly one" in srv.add_spine_document("ibx-5153", "L")["error"]
    assert "exactly one" in srv.add_spine_document(
        "ibx-5153", "L", file_path="/a", source_title="b"
    )["error"]


def test_add_spine_document_from_file(monkeypatch, tmp_path):
    """Reads a UTF-8 file and authors an element from its text."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))
    captured = {}

    def fake_create(code, label, type_, body="", serves=None):
        captured.update(label=label, type=type_, body=body)
        return {"element_id": "_authored/l", "version_label": "v1"}

    monkeypatch.setattr(srv, "create_spine_element", fake_create)

    f = tmp_path / "synth.md"
    f.write_text("# Synthesis\n\nBody text.", encoding="utf-8")
    out = srv.add_spine_document("ibx-5153", "My synth", file_path=str(f))
    assert captured["type"] == "synthesis"
    assert "Body text." in captured["body"]
    assert out["element_id"] == "_authored/l"
    assert "source_attached" not in out  # file path never attaches a source


def test_add_spine_document_missing_file(monkeypatch):
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))
    out = srv.add_spine_document("ibx-5153", "L", file_path="/no/such/file.md")
    assert "file not found" in out["error"]


def test_add_spine_document_from_source_attaches(monkeypatch):
    """Pulls an ingested source's text as body AND attaches the source."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))
    monkeypatch.setattr("cp_engine.config.load", lambda root: object())
    monkeypatch.setattr("cp_engine.sync_mc2._load_ingest_creds", lambda config: None)
    monkeypatch.setattr(
        "cp_engine.project_sources.pull_source",
        lambda client, pid, cid, title: {"chunks": ["chunk one", "chunk two"]},
    )
    monkeypatch.setattr(
        srv, "create_spine_element",
        lambda *a, **k: {"element_id": "_authored/b", "version_label": "v1"},
    )
    monkeypatch.setattr(
        srv, "add_element_source",
        lambda code, key, title: {"source": {"title": title}},
    )
    out = srv.add_spine_document("ibx-5153", "Brief card",
                                 source_title="The Brief")
    assert out["element_id"] == "_authored/b"
    assert out["source_attached"] == {"title": "The Brief"}


def test_add_spine_document_source_needs_engagement(monkeypatch):
    """An initiative (cid=None) can't use source_title (no ingested sources)."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", None))
    out = srv.add_spine_document("mission-control", "L", source_title="X")
    assert "engagements" in out["error"]


# ---------------------------------------------------------------------------
# pull_element_from_project + set_element_account_scope (#120)
# ---------------------------------------------------------------------------


def test_set_element_account_scope_dispatches(monkeypatch):
    """account=True → promote, account=False → demote."""
    monkeypatch.setattr(srv, "promote_stakeholder",
                        lambda code, key: {"scope": "account"})
    monkeypatch.setattr(srv, "demote_stakeholder",
                        lambda code, key: {"scope": "project"})
    assert srv.set_element_account_scope("ibx-5153", "k", True)["scope"] == "account"
    assert srv.set_element_account_scope("ibx-5153", "k", False)["scope"] == "project"


def test_pull_element_from_project_copies_with_provenance(monkeypatch):
    """Reads source element, authors a copy carrying an origin line in the body."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))
    monkeypatch.setattr(
        "cp_engine.project_sources.pull_spine",
        lambda client, pid, key, cid: {
            "est_item_id": "_authored/insight", "framing": "Key insight",
            "body": "The insight body.",
        },
    )
    captured = {}

    def fake_create(code, label, type_, body="", serves=None):
        captured.update(code=code, label=label, body=body)
        return {"element_id": "_authored/insight-copy", "version_label": "v1"}

    monkeypatch.setattr(srv, "create_spine_element", fake_create)
    out = srv.pull_element_from_project("ggl-5168", "ibx-5153", "Key insight")
    assert captured["code"] == "ibx-5153"          # authored INTO the target
    assert "Pulled from **ggl-5168**" in captured["body"]
    assert "The insight body." in captured["body"]
    assert out["origin"]["project"] == "ggl-5168"
    assert out["account_scoped"] is False


def test_pull_element_from_project_account_tag(monkeypatch):
    """account=True promotes the copy to account scope."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))
    monkeypatch.setattr(
        "cp_engine.project_sources.pull_spine",
        lambda client, pid, key, cid: {
            "est_item_id": "_authored/x", "framing": "X", "body": "b",
        },
    )
    monkeypatch.setattr(
        srv, "create_spine_element",
        lambda *a, **k: {"element_id": "_authored/x-copy", "version_label": "v1"},
    )
    monkeypatch.setattr(srv, "set_element_account_scope",
                        lambda code, key, account: {"scope": "account"})
    out = srv.pull_element_from_project("ggl-5168", "ibx-5153", "X", account=True)
    assert out["account_scoped"] is True


def test_pull_element_from_project_source_miss(monkeypatch):
    """A miss in the source project surfaces as an error, never raises."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))
    monkeypatch.setattr(
        "cp_engine.project_sources.pull_spine",
        lambda client, pid, key, cid: {"body": "", "error": "no spine element"},
    )
    out = srv.pull_element_from_project("ggl-5168", "ibx-5153", "ghost")
    assert "ggl-5168" in out["error"] and "no spine element" in out["error"]
