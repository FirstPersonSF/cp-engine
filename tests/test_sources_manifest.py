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
    def __init__(self, data):
        self._data = data

    def select(self, columns):
        return self

    def eq(self, col, val):
        return self

    def order(self, col, desc=False):
        return self

    def execute(self):
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

    def table(self, name):
        return _FakeTableQuery(self._assets)

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

    assert n == 2
    assert llm.calls == 2  # one per asset

    manifest = _read_manifest(tmp_path)
    assert "<!-- cp-engine:start sources -->" in manifest
    assert "<!-- cp-engine:end sources -->" in manifest
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
    assert n == 2
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

    assert n == 3
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

    assert n == 1
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

    assert n == 2
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

    assert n == 1
    assert llm.calls == 1  # corrupt cache → re-summarized, no crash
