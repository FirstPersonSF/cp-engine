import os
from cp_engine.asset_ingest_settings import AssetIngestSettings


def test_defaults_to_voyage_3_large(monkeypatch):
    monkeypatch.delenv("INGEST_EMBEDDING_MODEL", raising=False)
    s = AssetIngestSettings()
    assert s.ingest_embedding_model == "voyage-3-large"


def test_reads_voyage_api_key_from_env(monkeypatch):
    monkeypatch.setenv("VOYAGE_API_KEY", "vk-123")
    s = AssetIngestSettings()
    assert s.voyage_api_key == "vk-123"


def test_exposes_full_ingest_settings_surface(monkeypatch):
    s = AssetIngestSettings()
    # the four attrs the document-ingest IngestSettings protocol requires
    assert hasattr(s, "openai_chat_model")
    assert hasattr(s, "openai_embedding_model")
    assert hasattr(s, "openai_whisper_model")
    assert isinstance(s.classifier_intelligence_enabled, bool)


def test_classifier_intelligence_defaults_off(monkeypatch):
    monkeypatch.delenv("CLASSIFIER_INTELLIGENCE_ENABLED", raising=False)
    s = AssetIngestSettings()
    assert s.classifier_intelligence_enabled is False
