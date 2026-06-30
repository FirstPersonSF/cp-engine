"""Tests for `rescope_meeting` (meetings-as-sources).

`rescope_meeting` is the real RETAG re-scope cascade behind the `rescope` seam
that `link_meeting` defaults to a no-op. A retag moves a meeting + its RAG rows
from the OLD project to a new one:

  1. ONE UPDATE on `rag_assets` (project_id + company_id), located by
     (source_provider='fathom', source_file_id=str(recording_id)) — NOT by
     project_id. Because the summary + transcript rows SHARE that source_file_id,
     one UPDATE moves both kinds; chunks follow by asset_id (no re-embed).
  2. ONE UPDATE on `fathom_meetings` (project_id=new, work_item_id=None,
     work_item_confidence=None) located by recording_id.

The locate key on (1) excluding project_id is the load-bearing correctness
property: it makes the cascade idempotent / no-ghost — re-running it (even with a
stale row that already points at the new project) re-finds the rows and
harmlessly re-sets the same project_id, and after a success NO rag_assets row for
this recording_id is left pointing at the old project.

A recorder fake client captures every `.table(name).update(payload).eq(col,val)
...execute()` chain (table + payload + eq-filters). NO real Supabase.
"""
from __future__ import annotations

from cp_engine.meetings import rescope_meeting


class _FakeUpdateChain:
    """Records a `.table(name).update(...).eq(...)...execute()` chain."""

    def __init__(self, recorder, table):
        self._recorder = recorder
        self._table = table
        self._payload = None
        self._filters = []

    def update(self, payload):
        self._payload = payload
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def execute(self):
        self._recorder.updates.append(
            {"table": self._table, "payload": self._payload,
             "filters": self._filters}
        )

        class _Resp:
            # Two rows: summary + transcript share the source_file_id.
            data = [{"id": "asset-summary"}, {"id": "asset-transcript"}]

        return _Resp()


class _FakeClient:
    """Captures UPDATE calls on rag_assets + fathom_meetings; no real Supabase."""

    def __init__(self):
        self.updates = []

    def table(self, name):
        return _FakeUpdateChain(self, name)


class _RaisingClient:
    def table(self, name):
        raise RuntimeError("db exploded")


def _row(**overrides):
    base = {"recording_id": 12345, "project_id": "p1"}
    base.update(overrides)
    return base


def _by_table(updates, table):
    return [u for u in updates if u["table"] == table]


def test_happy_retag_moves_assets_and_meeting():
    client = _FakeClient()
    result = rescope_meeting(client, _row(project_id="p1"), "p2",
                             new_company_id="co2")

    assert result["ok"] is True
    assert result["rescoped"] is True
    assert result["old_project_id"] == "p1"
    assert result["new_project_id"] == "p2"
    assert result["assets_moved"] == 2

    # rag_assets UPDATE: payload moves project + company; located by the
    # fathom source key, NOT by project_id.
    ra = _by_table(client.updates, "rag_assets")
    assert len(ra) == 1
    assert ra[0]["payload"]["project_id"] == "p2"
    assert ra[0]["payload"]["company_id"] == "co2"
    assert ("source_provider", "fathom") in ra[0]["filters"]
    assert ("source_file_id", "12345") in ra[0]["filters"]

    # fathom_meetings UPDATE: new project, work item cleared, by recording_id.
    fm = _by_table(client.updates, "fathom_meetings")
    assert len(fm) == 1
    assert fm[0]["payload"]["project_id"] == "p2"
    assert fm[0]["payload"]["work_item_id"] is None
    assert fm[0]["payload"]["work_item_confidence"] is None
    assert ("recording_id", 12345) in fm[0]["filters"]


def test_rag_assets_locate_key_excludes_project_id():
    """The load-bearing property: the rag_assets locate key is
    (source_provider, source_file_id) and does NOT include project_id — that's
    what makes the cascade idempotent / no-ghost."""
    client = _FakeClient()
    rescope_meeting(client, _row(project_id="p1"), "p2", new_company_id="co2")

    ra = _by_table(client.updates, "rag_assets")
    filter_cols = [col for (col, _val) in ra[0]["filters"]]
    assert "project_id" not in filter_cols


def test_noop_when_old_equals_new():
    client = _FakeClient()
    result = rescope_meeting(client, _row(project_id="p2"), "p2")

    assert result["ok"] is True
    assert result["rescoped"] is False
    assert "project unchanged" in result["reason"]
    # No work at all.
    assert client.updates == []


def test_guard_missing_recording_id():
    client = _FakeClient()
    result = rescope_meeting(client, _row(recording_id=None), "p2")

    assert result["ok"] is False
    assert "recording_id" in result["reason"]
    assert client.updates == []


def test_company_id_threaded_into_rag_assets_payload():
    client = _FakeClient()
    rescope_meeting(client, _row(project_id="p1"), "p2", new_company_id="co-xyz")

    ra = _by_table(client.updates, "rag_assets")
    assert ra[0]["payload"]["company_id"] == "co-xyz"


def test_company_id_defaults_to_none():
    client = _FakeClient()
    rescope_meeting(client, _row(project_id="p1"), "p2")

    ra = _by_table(client.updates, "rag_assets")
    assert ra[0]["payload"]["company_id"] is None


def test_single_rag_assets_update_covers_both_kinds():
    """Summary + transcript share source_file_id, so ONE UPDATE moves both —
    assert there's exactly one rag_assets update, not two."""
    client = _FakeClient()
    rescope_meeting(client, _row(project_id="p1"), "p2")

    assert len(_by_table(client.updates, "rag_assets")) == 1


def test_never_raises():
    result = rescope_meeting(_RaisingClient(), _row(project_id="p1"), "p2")

    assert result["ok"] is False
    assert "db exploded" in result["reason"]
