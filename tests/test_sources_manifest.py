"""Tests for `cp_engine.project_sources.write_sources_manifest`.

No Supabase / Voyage / Anthropic. A fake supabase client fronts BOTH the
`.table().select()...execute()` chain (rag_assets, for list_sources) AND the
`.rpc(name, params).execute()` shape (asset_chunks, for the per-doc summary's
pull_source). A fake `llm` callable records its calls and returns canned
summaries; a tmp `project_dir` receives `_sources.md` + `_sources.cache.json`.

The key assertion is the CACHE-HIT test: a second run over unchanged docs must
NOT grow the llm call count.
"""

from __future__ import annotations

import json

from cp_engine.project_sources import write_sources_manifest


# ──────────────────────────────────────────────────────────────────────
#  Fakes
# ──────────────────────────────────────────────────────────────────────


class _FakeExecute:
    def __init__(self, data):
        self.data = data


class _FakeTableQuery:
    def __init__(self, data, updates=None):
        self._data = data
        self._updates = updates if updates is not None else []
        self._pending = None
        self._eq = {}

    def select(self, columns):
        return self

    def update(self, payload):
        # `_persist_description` writes here (#210); record it so tests can
        # assert the description reached the row and not just the local cache.
        self._pending = payload
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def order(self, col, desc=False):
        return self

    def execute(self):
        if self._pending is not None:
            self._updates.append({"payload": self._pending, "where": dict(self._eq)})
            self._pending = None
            return _FakeExecute([])
        return _FakeExecute(self._data)


class _FakeRpcQuery:
    def __init__(self, rows):
        self._rows = rows

    def execute(self):
        return _FakeExecute(list(self._rows))


class _FakeClient:
    """Fronts rag_assets (table chain) + asset_chunks (rpc) for the manifest.

    `assets` are the rag_assets rows list_sources returns. `chunk_rows` are the
    scoped-chunk rows pull_source's RPC returns (shared across docs; filtered by
    title inside pull_source).
    """

    def __init__(self, assets, chunk_rows):
        self._assets = assets
        self._chunk_rows = chunk_rows
        self.updates: list[dict] = []

    def table(self, name):
        return _FakeTableQuery(self._assets, self.updates)

    def rpc(self, name, params):
        return _FakeRpcQuery(self._chunk_rows)


class _FakeLLM:
    """Records prompts; returns a canned summary derived from the prompt."""

    def __init__(self, raise_on_title=None):
        self.prompts: list[str] = []
        self._raise_on_title = raise_on_title

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if self._raise_on_title and self._raise_on_title in prompt:
            raise RuntimeError("simulated LLM failure")
        # Echo the doc title so we can assert which doc was summarized.
        return f"Summary of prompt #{len(self.prompts)}."

    @property
    def calls(self):
        return len(self.prompts)


# Each asset's title is also the chunk title so pull_source matches it.
def _chunk(title, text):
    return {
        "text": text,
        "citation_url": f"https://drive/{title}",
        "title": title,
        "scope": "project",
    }


def _assets(*specs):
    """specs: (id, title, file_hash) tuples → rag_assets rows."""
    rows = []
    for i, (aid, title, fhash) in enumerate(specs):
        rows.append(
            {
                "id": aid,
                "title": title,
                "source_type": "drive",
                "status": "active",
                "created_at": f"2026-06-0{i + 1}T00:00:00Z",
                "file_hash": fhash,
            }
        )
    return rows


def _read_manifest(project_dir):
    return (project_dir / "_sources.md").read_text(encoding="utf-8")


def _read_cache(project_dir):
    return json.loads((project_dir / "_sources.cache.json").read_text(encoding="utf-8"))


# ──────────────────────────────────────────────────────────────────────
#  Tests
# ──────────────────────────────────────────────────────────────────────


def test_first_run_writes_manifest_and_cache(tmp_path):
    assets = _assets(("a", "Alpha Doc", "h-a"), ("b", "Beta Doc", "h-b"))
    chunks = [_chunk("Alpha Doc", "alpha text"), _chunk("Beta Doc", "beta text")]
    client = _FakeClient(assets, chunks)
    llm = _FakeLLM()

    n = write_sources_manifest(client, tmp_path, "proj-1", "co-9", llm=llm)

    assert len(n) == 2
    assert llm.calls == 2  # one per asset

    manifest = _read_manifest(tmp_path)
    # M5: whole file is engine-owned and fully regenerated — no region markers.
    assert "cp-engine:start sources" not in manifest
    assert "cp-engine:end sources" not in manifest
    assert "2 ingested document(s)" in manifest
    assert "**Alpha Doc** · drive — Summary of prompt #1." in manifest
    assert "**Beta Doc** · drive — Summary of prompt #2." in manifest

    cache = _read_cache(tmp_path)
    assert set(cache) == {"a", "b"}
    assert cache["a"]["hash"] == "h-a"
    assert cache["a"]["summary"] == "Summary of prompt #1."


def test_second_run_same_assets_is_cache_hit_no_llm(tmp_path):
    """KEY TEST: unchanged docs → llm call count does NOT grow on rerun."""
    assets = _assets(("a", "Alpha Doc", "h-a"), ("b", "Beta Doc", "h-b"))
    chunks = [_chunk("Alpha Doc", "alpha text"), _chunk("Beta Doc", "beta text")]
    client = _FakeClient(assets, chunks)
    llm = _FakeLLM()

    write_sources_manifest(client, tmp_path, "proj-1", "co-9", llm=llm)
    assert llm.calls == 2

    # Second run, identical inputs → ZERO additional llm calls.
    n = write_sources_manifest(client, tmp_path, "proj-1", "co-9", llm=llm)
    assert len(n) == 2
    assert llm.calls == 2  # unchanged

    manifest = _read_manifest(tmp_path)
    assert "**Alpha Doc** · drive — Summary of prompt #1." in manifest
    assert "**Beta Doc** · drive — Summary of prompt #2." in manifest


def test_new_asset_summarized_only_for_the_new_one(tmp_path):
    assets = _assets(("a", "Alpha Doc", "h-a"), ("b", "Beta Doc", "h-b"))
    chunks = [
        _chunk("Alpha Doc", "alpha text"),
        _chunk("Beta Doc", "beta text"),
        _chunk("Gamma Doc", "gamma text"),
    ]
    client = _FakeClient(assets, chunks)
    llm = _FakeLLM()
    write_sources_manifest(client, tmp_path, "proj-1", "co-9", llm=llm)
    assert llm.calls == 2

    # Add a third asset; reuse cache from first run.
    assets2 = _assets(
        ("a", "Alpha Doc", "h-a"),
        ("b", "Beta Doc", "h-b"),
        ("c", "Gamma Doc", "h-c"),
    )
    client2 = _FakeClient(assets2, chunks)
    n = write_sources_manifest(client2, tmp_path, "proj-1", "co-9", llm=llm)

    assert len(n) == 3
    assert llm.calls == 3  # only the new doc summarized
    cache = _read_cache(tmp_path)
    assert set(cache) == {"a", "b", "c"}


def test_removed_asset_drops_from_manifest_and_cache(tmp_path):
    assets = _assets(("a", "Alpha Doc", "h-a"), ("b", "Beta Doc", "h-b"))
    chunks = [_chunk("Alpha Doc", "alpha text"), _chunk("Beta Doc", "beta text")]
    client = _FakeClient(assets, chunks)
    llm = _FakeLLM()
    write_sources_manifest(client, tmp_path, "proj-1", "co-9", llm=llm)

    # 'b' removed from list_sources.
    assets2 = _assets(("a", "Alpha Doc", "h-a"))
    client2 = _FakeClient(assets2, chunks)
    n = write_sources_manifest(client2, tmp_path, "proj-1", "co-9", llm=llm)

    assert len(n) == 1
    manifest = _read_manifest(tmp_path)
    assert "Alpha Doc" in manifest
    assert "Beta Doc" not in manifest
    cache = _read_cache(tmp_path)
    assert set(cache) == {"a"}  # 'b' pruned


def test_changed_file_hash_resummarizes(tmp_path):
    assets = _assets(("a", "Alpha Doc", "h-a"), ("b", "Beta Doc", "h-b"))
    chunks = [_chunk("Alpha Doc", "alpha text"), _chunk("Beta Doc", "beta text")]
    client = _FakeClient(assets, chunks)
    llm = _FakeLLM()
    write_sources_manifest(client, tmp_path, "proj-1", "co-9", llm=llm)
    assert llm.calls == 2

    # 'a' content changed → new file_hash → re-summarize just 'a'.
    assets2 = _assets(("a", "Alpha Doc", "h-a-v2"), ("b", "Beta Doc", "h-b"))
    client2 = _FakeClient(assets2, chunks)
    write_sources_manifest(client2, tmp_path, "proj-1", "co-9", llm=llm)

    assert llm.calls == 3  # only 'a' re-summarized
    cache = _read_cache(tmp_path)
    assert cache["a"]["hash"] == "h-a-v2"


def test_llm_failure_yields_unavailable_and_completes(tmp_path):
    assets = _assets(("a", "Alpha Doc", "h-a"), ("b", "Beta Doc", "h-b"))
    chunks = [_chunk("Alpha Doc", "alpha text"), _chunk("Beta Doc", "beta text")]
    client = _FakeClient(assets, chunks)
    # llm raises for the Alpha doc only.
    llm = _FakeLLM(raise_on_title="Alpha Doc")

    n = write_sources_manifest(client, tmp_path, "proj-1", "co-9", llm=llm)

    assert len(n) == 2
    manifest = _read_manifest(tmp_path)
    assert "**Alpha Doc** · drive — (summary unavailable)" in manifest
    # Beta still got a real summary.
    assert "**Beta Doc** · drive — Summary of prompt #2." in manifest


def test_corrupt_cache_tolerated(tmp_path):
    (tmp_path / "_sources.cache.json").write_text("{not json", encoding="utf-8")
    assets = _assets(("a", "Alpha Doc", "h-a"))
    chunks = [_chunk("Alpha Doc", "alpha text")]
    client = _FakeClient(assets, chunks)
    llm = _FakeLLM()

    n = write_sources_manifest(client, tmp_path, "proj-1", "co-9", llm=llm)

    assert len(n) == 1
    assert llm.calls == 1  # corrupt cache → re-summarized, no crash


def test_failed_summary_is_not_cached_and_retries(tmp_path):
    """I1: a failed summary must NOT be cached → next sync retries it.

    Caching the `(summary unavailable)` sentinel would poison the doc forever
    (hash unchanged → cache HIT → never re-summarized even after the API is
    fixed). So a failure leaves NO cache entry, and the next run is a cache MISS
    that re-summarizes.
    """
    assets = _assets(("a", "Alpha Doc", "h-a"))
    chunks = [_chunk("Alpha Doc", "alpha text")]
    client = _FakeClient(assets, chunks)

    # First run: llm raises for Alpha → manifest shows unavailable, no cache entry.
    failing_llm = _FakeLLM(raise_on_title="Alpha Doc")
    write_sources_manifest(client, tmp_path, "proj-1", "co-9", llm=failing_llm)

    manifest = _read_manifest(tmp_path)
    assert "**Alpha Doc** · drive — (summary unavailable)" in manifest
    cache = _read_cache(tmp_path)
    assert "a" not in cache  # failure NOT cached

    # Second run, SAME hash, llm now succeeds → cache MISS → re-summarized.
    ok_llm = _FakeLLM()
    write_sources_manifest(client, tmp_path, "proj-1", "co-9", llm=ok_llm)

    assert ok_llm.calls == 1  # retried despite unchanged hash
    manifest = _read_manifest(tmp_path)
    assert "**Alpha Doc** · drive — Summary of prompt #1." in manifest
    cache = _read_cache(tmp_path)
    assert cache["a"]["summary"] == "Summary of prompt #1."


def test_summary_cap_limits_new_calls(tmp_path):
    """M1: at most `max_new_summaries` fresh summaries per call; rest deferred.

    Deferred (uncached, over-cap) docs show `(summary unavailable)` and are NOT
    cached, so the next sync picks them up — a cold project fills over several
    syncs instead of one thundering herd.
    """
    specs = [(f"id{i}", f"Doc {i}", f"h-{i}") for i in range(30)]
    assets = _assets(*specs)
    chunks = [_chunk(f"Doc {i}", f"text {i}") for i in range(30)]
    client = _FakeClient(assets, chunks)

    llm = _FakeLLM()
    n = write_sources_manifest(
        client, tmp_path, "proj-1", "co-9", llm=llm, max_new_summaries=10
    )

    assert len(n) == 30
    assert llm.calls == 10  # capped
    cache = _read_cache(tmp_path)
    assert len(cache) == 10  # only the summarized ones cached
    manifest = _read_manifest(tmp_path)
    assert manifest.count("(summary unavailable)") == 20  # the deferred 20

    # Second run: the 10 cached are HITS (free); the next 10 deferred get done.
    llm2 = _FakeLLM()
    write_sources_manifest(
        client, tmp_path, "proj-1", "co-9", llm=llm2, max_new_summaries=10
    )
    assert llm2.calls == 10  # next batch
    cache = _read_cache(tmp_path)
    assert len(cache) == 20


def test_summary_cap_does_not_affect_cache_hits(tmp_path):
    """M1: cache hits are always free — a fully-cached run makes 0 calls."""
    specs = [(f"id{i}", f"Doc {i}", f"h-{i}") for i in range(30)]
    assets = _assets(*specs)
    chunks = [_chunk(f"Doc {i}", f"text {i}") for i in range(30)]
    client = _FakeClient(assets, chunks)

    # Warm the cache fully (cap above the asset count).
    write_sources_manifest(
        client, tmp_path, "proj-1", "co-9", llm=_FakeLLM(), max_new_summaries=100
    )
    assert len(_read_cache(tmp_path)) == 30

    # All cached → 0 calls regardless of a tiny cap.
    llm = _FakeLLM()
    write_sources_manifest(
        client, tmp_path, "proj-1", "co-9", llm=llm, max_new_summaries=1
    )
    assert llm.calls == 0


# ──────────────────────────────────────────────────────────────────────
#  #210 / mig 164 — the summary is persisted to rag_assets.description
#
#  The JSON cache is gitignored and local, so a summary that lives only
#  there is re-paid for on every machine and is invisible to every MCP
#  caller. That is the cost #210 is about: 38 sources on slt-5196 meant
#  38 pulls to learn what the corpus held.
# ──────────────────────────────────────────────────────────────────────


def test_a_fresh_summary_is_written_to_the_asset_row(tmp_path):
    assets = _assets(("a", "Alpha Doc", "h-a"))
    chunks = [_chunk("Alpha Doc", "alpha text")]
    client = _FakeClient(assets, chunks)

    write_sources_manifest(client, tmp_path, "pid", "cid", llm=_FakeLLM())

    writes = [u for u in client.updates if "description" in u["payload"]]
    assert len(writes) == 1
    assert writes[0]["where"]["id"] == "a"
    assert writes[0]["payload"]["description"].startswith("Summary of prompt")


def test_a_failed_summary_is_not_persisted(tmp_path):
    """A sentinel in the DB would be worse than an empty column — it reads as
    a real description and the retry mechanic would never replace it."""
    assets = _assets(("a", "Alpha Doc", "h-a"))
    chunks = [_chunk("Alpha Doc", "alpha text")]
    client = _FakeClient(assets, chunks)

    write_sources_manifest(
        client, tmp_path, "pid", "cid", llm=_FakeLLM(raise_on_title="Alpha Doc")
    )

    assert [u for u in client.updates if "description" in u["payload"]] == []


def test_a_cache_hit_backfills_an_empty_description(tmp_path):
    """Docs summarised before mig 164 are cache hits forever, so without a
    backfill they would never reach the DB at all."""
    assets = _assets(("a", "Alpha Doc", "h-a"))
    chunks = [_chunk("Alpha Doc", "alpha text")]

    # Run 1 populates the local cache.
    write_sources_manifest(_FakeClient(assets, chunks), tmp_path, "pid", "cid",
                           llm=_FakeLLM())

    # Run 2 is a pure cache hit — no LLM call, but the row is still empty.
    client2 = _FakeClient(assets, chunks)
    llm2 = _FakeLLM()
    write_sources_manifest(client2, tmp_path, "pid", "cid", llm=llm2)

    assert llm2.calls == 0, "cache hit must not re-summarise"
    writes = [u for u in client2.updates if "description" in u["payload"]]
    assert len(writes) == 1, "the empty description should be backfilled"


def test_an_existing_description_is_never_overwritten(tmp_path):
    """A hand-written description outranks a generated one."""
    assets = _assets(("a", "Alpha Doc", "h-a"))
    assets[0]["description"] = "Hand-written: the embargoed CEO draft."
    chunks = [_chunk("Alpha Doc", "alpha text")]

    write_sources_manifest(_FakeClient(assets, chunks), tmp_path, "pid", "cid",
                           llm=_FakeLLM())
    client2 = _FakeClient(assets, chunks)
    write_sources_manifest(client2, tmp_path, "pid", "cid", llm=_FakeLLM())

    assert [u for u in client2.updates if "description" in u["payload"]] == []


def test_a_persist_failure_never_breaks_the_manifest(tmp_path):
    """The manifest is the caller's job; the description is a bonus."""
    assets = _assets(("a", "Alpha Doc", "h-a"))
    chunks = [_chunk("Alpha Doc", "alpha text")]

    class _HalfBroken(_FakeClient):
        def table(self, name):
            q = _FakeTableQuery(self._assets, self.updates)
            q.update = lambda payload: (_ for _ in ()).throw(RuntimeError("42501"))
            return q

    out = write_sources_manifest(
        _HalfBroken(assets, chunks), tmp_path, "pid", "cid", llm=_FakeLLM()
    )
    assert out
    assert (tmp_path / "_sources.md").exists()
