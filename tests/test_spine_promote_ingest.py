# tests/test_spine_promote_ingest.py
from cp_engine.spine_promote import ingest_single_file


class _FakePipeline:
    def __init__(self): self.calls = []
    def ingest_file(self, file_path, title=None, url=None):
        self.calls.append((file_path, title, url))
        return {"asset_id": "a1", "status": "new"}


def test_ingest_single_file_calls_pipeline_ingest_file():
    fake = _FakePipeline()
    out = ingest_single_file(
        "/tmp/t.txt", "pid", "My Transcript",
        supabase_url="u", supabase_key="k",
        pipeline_factory=lambda project_id, supabase_url, supabase_key: fake,
    )
    assert fake.calls == [("/tmp/t.txt", "My Transcript", None)]
    assert out["asset_id"] == "a1"
