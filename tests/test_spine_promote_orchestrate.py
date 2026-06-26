"""Tests for `promote_transcript` orchestration (item-3, task 4).

The orchestration ties resolve→file→ingest→stamp together with two load-bearing
correctness contracts:
  A — engagement-only guard: `company_id is None` (initiative) returns ok:False
      WITHOUT any file/ingest work.
  B — stamp↔ingest seam: ONE stable file_path passed to BOTH ingest and stamp,
      and the stamp result is checked (stamped + exactly one id) before ok:True.

`ingest` and `stamp` are injected so the real ingest pipeline / Voyage / Supabase
are never touched.
"""
from __future__ import annotations

from pathlib import Path

from cp_engine.spine_promote import promote_transcript


class _Recorder:
    """A fake ingest/stamp that records every call's kwargs/args."""

    def __init__(self, result=None):
        self.calls = []
        self._result = result if result is not None else {}

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return self._result


def _make_transcript(tenant_root: Path, project_code: str, rel_path: str) -> Path:
    src = tenant_root / project_code / rel_path
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("transcript body")
    return src


def test_initiative_deferred_no_file_work(tmp_path):
    ingest = _Recorder({"ok": True})
    stamp = _Recorder({"stamped": True, "ids": ["a1"]})
    result = promote_transcript(
        client=object(),
        tenant_root=str(tmp_path),
        project_code="storyos",
        project_id="p1",
        company_id=None,  # initiative
        element_row={"est_item_id": "e1", "rel_path": "t.txt"},
        supabase_url="u",
        supabase_key="k",
        ingest=ingest,
        stamp=stamp,
    )
    assert result["ok"] is False
    assert "initiative" in result["reason"]
    assert ingest.calls == []
    assert stamp.calls == []


def test_missing_rel_path(tmp_path):
    ingest = _Recorder()
    stamp = _Recorder()
    result = promote_transcript(
        client=object(),
        tenant_root=str(tmp_path),
        project_code="ggl-5168",
        project_id="p1",
        company_id="c1",
        element_row={"est_item_id": "e1"},  # no rel_path
        supabase_url="u",
        supabase_key="k",
        ingest=ingest,
        stamp=stamp,
    )
    assert result["ok"] is False
    assert "no source file" in result["reason"]
    assert ingest.calls == []
    assert stamp.calls == []


def test_missing_file(tmp_path):
    ingest = _Recorder()
    stamp = _Recorder()
    result = promote_transcript(
        client=object(),
        tenant_root=str(tmp_path),
        project_code="ggl-5168",
        project_id="p1",
        company_id="c1",
        element_row={"est_item_id": "e1", "rel_path": "missing.txt"},
        supabase_url="u",
        supabase_key="k",
        ingest=ingest,
        stamp=stamp,
    )
    assert result["ok"] is False
    assert "not found" in result["reason"]
    assert ingest.calls == []
    assert stamp.calls == []


def test_happy_path_same_stable_path(tmp_path):
    _make_transcript(tmp_path, "ggl-5168", "meetings/t.txt")
    ingest = _Recorder({"ok": True})
    stamp = _Recorder({"stamped": True, "ids": ["a1"], "title": "T"})
    result = promote_transcript(
        client=object(),
        tenant_root=str(tmp_path),
        project_code="ggl-5168",
        project_id="p1",
        company_id="c1",
        element_row={"est_item_id": "e1/with slash", "rel_path": "meetings/t.txt",
                     "framing": "My Framing"},
        supabase_url="u",
        supabase_key="k",
        ingest=ingest,
        stamp=stamp,
    )
    assert result["ok"] is True
    assert result["title"] == "My Framing"
    assert result["ids"] == ["a1"]

    # Contract B step 1: ingest and stamp got the IDENTICAL file_path.
    ingest_path = ingest.calls[0]["args"][0]  # first positional = file_path
    stamp_path = stamp.calls[0]["kwargs"]["file_path"]
    assert ingest_path == stamp_path

    # ...and that path is under a stable est_item_id-keyed dir (sanitized).
    assert ".spine-promote" in ingest_path
    assert "e1_with_slash" in ingest_path  # est_item_id sanitized
    assert ingest_path.endswith("t.txt")
    # The file was actually copied there.
    assert Path(ingest_path).exists()


def test_stamp_zero_match_is_failure(tmp_path):
    _make_transcript(tmp_path, "ggl-5168", "t.txt")
    ingest = _Recorder({"ok": True})
    stamp = _Recorder({"stamped": False, "ids": []})
    result = promote_transcript(
        client=object(),
        tenant_root=str(tmp_path),
        project_code="ggl-5168",
        project_id="p1",
        company_id="c1",
        element_row={"est_item_id": "e1", "rel_path": "t.txt"},
        supabase_url="u",
        supabase_key="k",
        ingest=ingest,
        stamp=stamp,
    )
    assert result["ok"] is False
    assert "no row" in result["reason"]


def test_stamp_multi_row_is_failure(tmp_path):
    _make_transcript(tmp_path, "ggl-5168", "t.txt")
    ingest = _Recorder({"ok": True})
    stamp = _Recorder({"stamped": True, "ids": ["a1", "a2"]})
    result = promote_transcript(
        client=object(),
        tenant_root=str(tmp_path),
        project_code="ggl-5168",
        project_id="p1",
        company_id="c1",
        element_row={"est_item_id": "e1", "rel_path": "t.txt"},
        supabase_url="u",
        supabase_key="k",
        ingest=ingest,
        stamp=stamp,
    )
    assert result["ok"] is False
    assert "2 rows" in result["reason"]
