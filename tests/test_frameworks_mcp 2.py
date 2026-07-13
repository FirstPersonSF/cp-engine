# tests/test_frameworks_mcp.py — slice 1 of the inbound-frameworks integration:
# readiness menu, corpus assembly, and the three MCP tools with a MockLLM
# (per the package's IMPLEMENTATION.md §6 — never a real LLM in this suite).
from pathlib import Path

import pytest

from cp_engine.frameworks import assemble_corpus, readiness_menu
from inbound_frameworks import FrameworkCatalog
from inbound_frameworks.llm import LLMResult


class MockLLM:
    """Records calls, returns a canned LLMResult (playbook §6 pattern)."""

    def __init__(self, result: LLMResult):
        self.result = result
        self.calls: list[dict] = []

    def complete(self, *, system, prompt, tools=None, tool_choice=None,
                 max_tokens=2048):
        self.calls.append({"system": system, "prompt": prompt, "tools": tools,
                           "tool_choice": tool_choice})
        return self.result


def _curated_fw():
    cat = FrameworkCatalog()
    fws = [f for f in cat.decomposable() if f.has_compose_template]
    assert fws, "bundled snapshot must carry at least one fully curated framework"
    return fws[0]


# ── readiness_menu ───────────────────────────────────────────────────────────

def test_readiness_lists_only_curated_frameworks():
    menu = readiness_menu()
    assert menu["frameworks"], "curated menu must not be empty"
    total = menu["snapshot"]["framework_count"]
    assert len(menu["frameworks"]) < total          # never all 130
    assert all(r["decomposable"] or r["composable"] for r in menu["frameworks"])


def test_readiness_layer_filter_is_case_insensitive():
    fw = _curated_fw()
    menu = readiness_menu(layer=(fw.unf_layer or "").upper())
    assert any(r["id"] == fw.id for r in menu["frameworks"])


def test_readiness_carries_snapshot_identity():
    meta = readiness_menu()["snapshot"]
    assert meta.get("catalog_hash") and meta.get("exported_at")


# ── assemble_corpus ──────────────────────────────────────────────────────────

class _SpineMissClient:
    """A client whose spine/source reads all miss (forces the file door)."""
    def table(self, n):
        class _T:
            def select(self, c): return self
            def eq(self, c, v): return self
            def order(self, *a, **k): return self
            def execute(self): return type("R", (), {"data": []})()
        return _T()
    def rpc(self, *a, **k):
        class _R:
            def execute(self): return type("R", (), {"data": []})()
        return _R()


def test_assemble_corpus_reads_repo_relative_files(tmp_path):
    (tmp_path / "notes.md").write_text("the corpus body")
    corpus, manifest = assemble_corpus(
        _SpineMissClient(), "pid", None, tmp_path, ["notes.md"])
    assert "the corpus body" in corpus
    assert manifest == [{"key": "notes.md", "resolved": "file", "chars": 15}]


def test_assemble_corpus_blocks_path_escape(tmp_path):
    root = tmp_path / "tenant"
    root.mkdir()
    (tmp_path / "secret.md").write_text("outside")
    corpus, manifest = assemble_corpus(
        _SpineMissClient(), "pid", None, root, ["../secret.md"])
    assert corpus == ""
    assert manifest[0]["resolved"] is None          # escape recorded as a miss


def test_assemble_corpus_records_misses_not_drops(tmp_path):
    corpus, manifest = assemble_corpus(
        _SpineMissClient(), "pid", None, tmp_path, ["nope.md"])
    assert manifest[0]["resolved"] is None and manifest[0]["note"]


def test_assemble_corpus_caps_runaway_corpus(tmp_path, monkeypatch):
    monkeypatch.setattr("cp_engine.frameworks.MAX_CORPUS_CHARS", 10)
    (tmp_path / "big.md").write_text("x" * 100)
    with pytest.raises(ValueError, match="narrow source_keys"):
        assemble_corpus(_SpineMissClient(), "pid", None, tmp_path, ["big.md"])


# ── MCP tools ────────────────────────────────────────────────────────────────

def _wire(monkeypatch, tmp_path, llm):
    import cp_engine.mcp_server as m
    monkeypatch.setattr(m, "_resolve", lambda code: (_SpineMissClient(), "pid", "cid"))
    monkeypatch.setattr(m, "_tenant_root", lambda: tmp_path)
    monkeypatch.setattr("cp_engine.frameworks.make_llm", lambda kind: llm)
    monkeypatch.setattr("cp_engine.sync_mc2._load_ingest_creds", lambda cfg: None)
    monkeypatch.setattr("cp_engine.config.load", lambda root: object())
    return m


def test_decompose_tool_passes_curated_template_through(monkeypatch, tmp_path):
    """Anti-graveyard read-path check: the framework's DB-curated template text
    must appear in the prompt the LLM actually received."""
    fw = _curated_fw()
    (tmp_path / "src.md").write_text("client source text")
    llm = MockLLM(LLMResult(tool_input={"field_values": {"f": "v"},
                                        "field_confidence": {"f": "certain"}},
                            usage={"input_tokens": 1, "output_tokens": 1}))
    m = _wire(monkeypatch, tmp_path, llm)
    res = m.framework_decompose("p", fw.id, ["src.md"])
    assert res.get("outcome") and "error" not in res
    assert res["sources"][0]["resolved"] == "file"
    joined = llm.calls[0]["system"] + llm.calls[0]["prompt"]
    assert "client source text" in llm.calls[0]["prompt"]
    # a distinctive slice of the curated template reached the LLM
    template = fw.active_version.get("decompose_prompt_template") or ""
    assert template[:40] in joined


def test_decompose_tool_requires_source_keys(monkeypatch, tmp_path):
    m = _wire(monkeypatch, tmp_path, MockLLM(LLMResult()))
    res = m.framework_decompose("p", _curated_fw().id, [])
    assert "error" in res and "source_keys" in res["error"]


def test_decompose_tool_unknown_framework_is_structured_error(monkeypatch, tmp_path):
    m = _wire(monkeypatch, tmp_path, MockLLM(LLMResult()))
    res = m.framework_decompose("p", "FW-999", ["x.md"])
    assert "error" in res


def test_uncurated_framework_is_a_note_no_llm_call(monkeypatch, tmp_path):
    """Anti-graveyard: no curated template → structured note, zero LLM calls."""
    cat = FrameworkCatalog()
    bare = next((f for f in cat.all()
                 if not f.has_decompose_template and not f.has_compose_template), None)
    if bare is None:
        pytest.skip("snapshot has no uncurated framework")
    llm = MockLLM(LLMResult())
    m = _wire(monkeypatch, tmp_path, llm)
    assert "note" in m.framework_decompose("p", bare.id, ["x.md"])
    assert "note" in m.framework_compose(bare.id, {"f": "v"})
    assert llm.calls == []


def test_compose_tool_returns_content_and_element_type(monkeypatch, tmp_path):
    fw = _curated_fw()
    llm = MockLLM(LLMResult(
        tool_input={"content": {"sections": [
            {"id": "s1", "text": "drafted", "order": 1, "title": "T"}]}},
        text='{"sections": [{"id": "s1", "text": "drafted", "order": 1, "title": "T"}]}',
        usage={"input_tokens": 1, "output_tokens": 1}))
    m = _wire(monkeypatch, tmp_path, llm)
    res = m.framework_compose(fw.id, {"field": "confirmed value"})
    assert "error" not in res
    assert res["outcome"]
    assert "confirmed value" in llm.calls[0]["prompt"]


def test_compose_tool_requires_field_values(monkeypatch, tmp_path):
    m = _wire(monkeypatch, tmp_path, MockLLM(LLMResult()))
    res = m.framework_compose(_curated_fw().id, {})
    assert "error" in res and "field_values" in res["error"]


def test_readiness_tool_never_errors_without_project():
    import cp_engine.mcp_server as m
    menu = m.framework_readiness()
    assert menu["frameworks"]
