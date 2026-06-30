"""The promote path must configure document-ingest's settings + OpenAI client
factory BEFORE building the pipeline.

`IngestPipeline.__init__` eagerly constructs an AudioParser, which calls the
default `get_openai_client()` (reads OPENAI_API_KEY from the raw env) UNLESS
`configure_ingest` has installed cp's own client factory. The normal ingest
path (`ingest_project_assets`) calls `_configure_pipeline_once()` right before
`_build_pipeline`; the promote path's `_default_pipeline_factory` did not, so
pipeline construction raised `OpenAIError: Missing credentials` wherever
OPENAI_API_KEY wasn't in the environment (e.g. the Railway webhook) — surfacing
to the user as the misleading downstream "stamp matched no row".
"""
from __future__ import annotations

import cp_engine.spine_promote as sp


def test_default_pipeline_factory_configures_before_building(monkeypatch):
    calls: list[str] = []

    def fake_configure():
        calls.append("configure")

    def fake_build(project_id, supabase_url, supabase_key):
        calls.append("build")
        return object()

    monkeypatch.setattr(
        "cp_engine.asset_ingest._configure_pipeline_once", fake_configure
    )
    monkeypatch.setattr("cp_engine.asset_ingest._build_pipeline", fake_build)

    sp._default_pipeline_factory("p1", "url", "key")

    # configure must run, and must run BEFORE build (the pipeline's parsers are
    # constructed eagerly in _build_pipeline, so the client factory must already
    # be installed).
    assert calls == ["configure", "build"]
