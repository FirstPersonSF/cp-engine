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


def test_stdio_surface_is_local_io_plus_reads():
    """4 source-read + 1 dropbox-write + 2 spine-read + 5 spine-write +
    1 spine-promote + 1 meetings-read + 3 framework + 2 commitment + 1 note.

    source-read grew 3→4 (#108 pull_document_comments); spine-write grew 8→11
    (#104 add/remove_element_provenance, #105 retire_spine_elements); create_note
    added (#107); spine-step-write added 4 (#119 add/set/reorder/remove_spine_step);
    then +4 (#120): push_to_dropbox (rich-doc write-back), add_spine_document
    (file/source → element), pull_element_from_project (cross-project copy +
    account-tag + lineage), set_element_account_scope (type-agnostic promote);
    then +1 (auto-journey-steps): propose_spine_step (machine-authored, proposed
    review-state).

    Then 37→34 (#143 hosted-MCP ratchet batch 1): `create_spine_relation`,
    `add_spine_step` and `propose_spine_step` were PORTED to the hosted server
    and deleted here, so the write surface never exists twice.

    Then 34→29 (#143 batch 2 — the UPDATE verbs): `set_spine_element`,
    `resolve_commitment` and set/reorder/remove_spine_step ported and deleted.
    `retire_spine_relation`, `retire_spine_element(s)` and the CREATE verbs are
    NOT yet ported and stay on stdio. Caveat carried on #143:
    `set_spine_element`'s important-flip RAG promotion is not mirrored on hosted
    — `promote_spine_transcript` (still here) is the standalone door for it.

    Then 29→25 (#143 batch 3 — the sources/provenance quartet):
    add/remove_element_source and add/remove_element_provenance ported and
    deleted. `project_sources.modify_element_sources` stays and is still called
    IN-PROCESS by `add_spine_document`'s source_title attach — an internal step
    of a surviving verb, not a second write door.

    Then 25→19 (#143 batch 4 — the retire/scope guarded verbs):
    `retire_spine_element(s)`, `retire_spine_relation`,
    `promote_stakeholder`/`demote_stakeholder` and `set_element_account_scope`
    ported and deleted. `project_sources.resolve_live_element` stays (the shared
    element matcher behind pull_spine_element / pull_element_from_project /
    the framework verbs). The account-scope MOVE also stays, as the PRIVATE
    `_set_account_scope`: `pull_element_from_project(account=True)` account-tags
    the copy it authored as an internal step of a surviving verb.

    Then 19→21 (#126): archive_project_source + rename_project_source — the
    source-store curation pair (STORE cleanup, distinct from the spine
    retire verbs that live on hosted).

    Then +1 (#160): compare_project_sources — the structural diff verb for
    feedback that arrives as a revised copy of the artifact.

    Then 22→13 (#138 ratchet, final batch): the remaining dual write verbs
    (create_spine_element, add_spine_document, add_spine_version,
    promote_spine_transcript, create_commitment, create_note) and the
    portable curation/copy verbs (archive_project_source,
    rename_project_source, pull_element_from_project) moved to hosted.
    stdio is now the LOCAL-I/O + READS surface: things that need this
    machine's disk or credentials the hosted env deliberately lacks."""
    names = {t.name for t in srv.mcp._tool_manager.list_tools()}
    assert names == {
        "list_project_sources",
        "pull_project_source",
        "fetch_project_source",
        "push_to_dropbox",
        "pull_document_comments",
        "compare_project_sources",
        "list_spine_elements",
        "pull_spine_element",
        "framework_readiness",
        "framework_decompose",
        "framework_compose",
        "list_project_meetings",
        "list_commitments",
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
                        layer=None, scope=None, binding=None, compact=False,
                        tier=None):
        captured["args"] = (client, project_id)
        captured["filters"] = (layer, scope, binding)
        captured["compact"] = compact
        captured["tier"] = tier
        return [{"est_item_id": "_authored/brief", "framing": "Brief"}]

    monkeypatch.setattr("cp_engine.project_sources.list_spine", fake_list_spine)

    out = srv.list_spine_elements("sap-5171")

    assert captured["args"] == (fake_client, "pid")
    # empty-string filter args normalize to None (no filtering)
    assert captured["filters"] == (None, None, None)
    assert captured["compact"] is False
    assert out == [{"est_item_id": "_authored/brief", "framing": "Brief"}]

    srv.list_spine_elements("sap-5171", compact=True)
    assert captured["compact"] is True


def test_list_spine_elements_singular_query_matches_plural_layer_e2e(monkeypatch):
    """Regression for the 2026-07-26 Phase 6 benchmark bug report: three
    independent cold agents called `list_spine_elements(layer="Decision")`
    (singular, per the tool's own docstring example) against live data whose
    layer value is stored as "Decisions" (plural) and got a silent empty
    result, forcing a fall back to an unfiltered scan.

    Unlike `test_list_spine_elements_delegates` (which mocks out
    `list_spine` entirely and can only prove parameter delegation), this
    goes through the REAL `list_spine` / `_layer_filter` / `_fold_layer`
    pipeline end to end via the actual MCP tool function, against a fake
    Supabase-shaped client — closing the gap where a wrapper-level
    regression (or a stale/rebuilt server not actually running the fixed
    code) would slip past a fully-mocked delegation test.
    """
    import cp_engine.project_sources as ps

    class _T:
        def select(self, c): self._c = c; return self
        def eq(self, c, v): return self
        def order(self, *a, **k): return self
        def execute(self): return type("R", (), {"data": rows})()

    class _C:
        def table(self, n): return _T()

    rows = [
        {"est_item_id": "_authored/pillar-ruling",
         "framing": "Pillar ruling", "layer": "Decisions",
         "binding": "unbound", "status": "live", "serves": [], "body": "b",
         "important": False, "note": None, "scope": None,
         "version_label": "v2", "version_date": "2026-07-26"},
        {"est_item_id": "_authored/some-note",
         "framing": "Some note", "layer": "Note",
         "binding": "unbound", "status": "live", "serves": [], "body": "b",
         "important": False, "note": None, "scope": None,
         "version_label": "v1", "version_date": "2026-07-26"},
    ]

    monkeypatch.setattr(ps, "fetch_project_done_map", lambda c, p: {})
    monkeypatch.setattr(srv, "_resolve", lambda code: (_C(), "pid", "cid"))

    out = srv.list_spine_elements("ibx-5192", layer="Decision")

    assert [r["est_item_id"] for r in out] == ["_authored/pillar-ruling"]


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












# --- auto-step wiring on content-writes (2026-07-23-auto-step-on-version-write)


def _capture_auto_step(monkeypatch):
    """Replace spine_steps.upsert_auto_step with a capturing stub; return the dict
    it records its args into."""
    captured = {}

    def fake(client, pid, key, title, *, step_date, company_id=None):
        captured.update(key=key, title=title, step_date=step_date,
                        company_id=company_id)
        return {"est_item_id": key, "created": True}

    monkeypatch.setattr("cp_engine.spine_steps.upsert_auto_step", fake)
    return captured
















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
            # The re-import above also rebound the PARENT PACKAGE attribute
            # (`cp_engine.mcp_server` etc.) to the fresh module object.
            # Restoring sys.modules alone leaves that attribute pointing at
            # the orphan copy — and `monkeypatch.setattr("cp_engine.
            # mcp_server.run_stdio", ...)` resolves through the attribute
            # while `from cp_engine.mcp_server import ...` resolves through
            # sys.modules, so a later test patches one module and runs the
            # other (bit test_cli_mcp when files ran out of alphabetical
            # order). Restore the attribute too.
            parent_name, _, child = mod.rpartition(".")
            parent = sys.modules.get(parent_name)
            if parent is None:
                continue
            if obj is not None:
                setattr(parent, child, obj)
            elif hasattr(parent, child):
                delattr(parent, child)


# ---------------------------------------------------------------------------
# Commitment tools (create_commitment / list_commitments)
#
# `resolve_commitment` was ported to the hosted MCP server and deleted from
# stdio (cp-engine #143), so its wrapper tests went with it. The
# cp_engine.commitments MODULE keeps close_commitment/find_open_commitment
# (tested in tests/test_commitments.py).
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
# Dropbox credential loading on the READ paths (#111)
#
# push_to_dropbox loaded DROPBOX_* before building its connector; the two read
# verbs did not, so every Dropbox-hosted source failed with "No Dropbox
# credentials found" while Supabase-backed verbs worked. Regression cover for
# both, since the asymmetry is exactly what made the bug hard to see.
# ---------------------------------------------------------------------------


def test_pull_document_comments_loads_dropbox_creds(monkeypatch):
    """The comment read loads DROPBOX_* before the connector self-configures."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))
    creds_loaded = {}
    monkeypatch.setattr(
        srv, "_try_load_dropbox_creds",
        lambda: creds_loaded.setdefault("called", True),
    )

    def fake_pull(client, pid, doc_title, dest):
        # Creds must already be loaded by the time the download runs.
        assert creds_loaded.get("called") is True
        return {"title": doc_title, "provider": "dropbox", "comment_count": 2,
                "comments": [{"author": "Kimber Myers"}, {"author": "Jaime Mehra"}]}

    monkeypatch.setattr(
        "cp_engine.project_sources.pull_document_comments", fake_pull
    )

    out = srv.pull_document_comments("ibx-5192", "deck_JM comments.pptx")
    assert creds_loaded["called"] is True
    assert out["comment_count"] == 2


def test_fetch_project_source_loads_dropbox_creds(monkeypatch, tmp_path):
    """The binary fetch loads DROPBOX_* too — same connector, same failure."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))
    creds_loaded = {}
    monkeypatch.setattr(
        srv, "_try_load_dropbox_creds",
        lambda: creds_loaded.setdefault("called", True),
    )

    def fake_fetch(client, pid, doc_title, dest):
        assert creds_loaded.get("called") is True
        return {"local_path": str(tmp_path / "deck.pptx"), "title": doc_title}

    monkeypatch.setattr("cp_engine.project_sources.fetch_source", fake_fetch)

    out = srv.fetch_project_source("ibx-5192", "deck.pptx")
    assert creds_loaded["called"] is True
    assert out["title"] == "deck.pptx"


def test_cred_load_failure_does_not_break_the_read(monkeypatch):
    """Cred loading is best-effort: a blowup must not fail an otherwise-fine read.

    Outside a tenant repo `load_config` raises NotATenantRepo, and in the
    webhook the creds already live in the process env. Neither should turn a
    working Drive-hosted read into an error.
    """
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))

    def boom(root):
        raise RuntimeError("No .cp-engine.toml — not a tenant repo?")

    monkeypatch.setattr("cp_engine.config.load", boom)
    monkeypatch.setattr(
        "cp_engine.project_sources.pull_document_comments",
        lambda client, pid, doc_title, dest: {
            "title": doc_title, "provider": "google_drive", "comment_count": 1,
            "comments": [{"author": "Kimber Myers"}],
        },
    )

    out = srv.pull_document_comments("ibx-5192", "deck.pptx")
    assert out["comment_count"] == 1
    assert "error" not in out


# ---------------------------------------------------------------------------
# add_spine_document (#120 — file / ingested-source → element)
# ---------------------------------------------------------------------------














# ---------------------------------------------------------------------------
# pull_element_from_project + _set_account_scope (#120)
#
# The `set_element_account_scope` / `promote_stakeholder` / `demote_stakeholder`
# TOOLS moved to the hosted server (#143 batch 4). The move itself survives here
# as the private `_set_account_scope`, because `pull_element_from_project`
# account-tags in-process.
# ---------------------------------------------------------------------------










# ──────────────────────────────────────────────────────────────────────
#  #150 — version stamping on tool results
# ──────────────────────────────────────────────────────────────────────


def test_error_payloads_carry_server_version(monkeypatch):
    """A tool failure names the code that produced it."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (_ for _ in ()).throw(
        RuntimeError("boom")))
    monkeypatch.setattr(srv, "_SERVER_VERSION", "9.9.9")
    monkeypatch.setattr(srv, "_installed_version", lambda: "9.9.9")

    out = srv.list_project_sources("IBX-5153")

    assert out[0]["server_version"] == "9.9.9"
    assert "engine_version_warning" not in out[0]


def test_version_mismatch_warns_to_restart(monkeypatch):
    """Server ≠ disk → every dict result tells the caller to restart /mcp."""
    monkeypatch.setattr(srv, "_resolve", lambda code: (_ for _ in ()).throw(
        RuntimeError("No valid credentials found")))
    monkeypatch.setattr(srv, "_SERVER_VERSION", "0.84.0")
    monkeypatch.setattr(srv, "_installed_version", lambda: "0.84.1")

    out = srv.list_project_sources("IBX-5153")

    assert out[0]["server_version"] == "0.84.0"
    assert "restart" in out[0]["engine_version_warning"]
    assert "0.84.1" in out[0]["engine_version_warning"]


def test_matched_versions_leave_success_results_untouched(monkeypatch):
    """No mismatch, no error → results pass through byte-identical."""
    fake_client = object()
    monkeypatch.setattr(srv, "_resolve", lambda code: (fake_client, "pid", "cid"))
    monkeypatch.setattr(srv, "_installed_version", lambda: None)
    monkeypatch.setattr(
        "cp_engine.project_sources.list_sources",
        lambda client, pid, cid: [{"id": "a1", "title": "Doc"}],
    )

    out = srv.list_project_sources("IBX-5153")

    assert out == [{"id": "a1", "title": "Doc"}]
