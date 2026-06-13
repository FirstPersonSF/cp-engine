"""Settings object for the asset-ingest pipeline (the document-ingest seam).

Exposes the IngestSettings surface document-ingest's configure_ingest() needs,
plus the Voyage embedder's voyage_api_key + ingest_embedding_model. Env-backed
so it reads the same creds cp already resolves (OPENAI_API_KEY, VOYAGE_API_KEY).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes")


@dataclass
class AssetIngestSettings:
    openai_chat_model: str = field(
        default_factory=lambda: os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
    )
    openai_embedding_model: str = field(
        default_factory=lambda: os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
    )
    openai_whisper_model: str = field(
        default_factory=lambda: os.getenv("OPENAI_WHISPER_MODEL", "whisper-1")
    )
    classifier_intelligence_enabled: bool = field(
        default_factory=lambda: _env_flag("CLASSIFIER_INTELLIGENCE_ENABLED", False)
    )
    voyage_api_key: str | None = field(default_factory=lambda: os.getenv("VOYAGE_API_KEY"))
    ingest_embedding_model: str = field(
        default_factory=lambda: os.getenv("INGEST_EMBEDDING_MODEL", "voyage-3-large")
    )
