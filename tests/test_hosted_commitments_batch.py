# tests/test_hosted_commitments_batch.py — issue #159: batch commitment
# resolution on the hosted MCP server.
#
# The hosted server's decorated verbs are MCP Tool objects, not plain
# callables, so these tests target the module-level helpers the verbs are
# built from: `_match_open_commitment` (pure), `_close_commitment_row`
# (fake client), and `_resolve_commitment_batch` (the shrinking-snapshot
# loop). The contract under test is retire_spine_elements' (#105): per-key
# results, a miss never aborts the batch, and a closed row can never be
# matched twice.
import importlib.util
import os
from pathlib import Path

import pytest

pytest.importorskip("jwt")
pytest.importorskip("mcp")
pytest.importorskip("supabase")

_SERVER_PATH = (
    Path(__file__).resolve().parents[1] / "prototypes" / "hosted-mcp" / "server.py"
)


@pytest.fixture(scope="module")
def srv():
    os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
    os.environ.setdefault("SUPABASE_ANON_KEY", "anon-key-for-tests")
    spec = importlib.util.spec_from_file_location("hosted_mcp_server", _SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(rid, desc, meeting=None):
    return {
        "id": rid,
        "description": desc,
        "status": "open",
        "source_meeting_id": meeting,
        "owner_name": None,
    }


class _FakeClient:
    """Captures updates; `fail_ids` rows update 0 rows (policy denial),
    `raise_ids` rows raise (transport error)."""

    def __init__(self, fail_ids=(), raise_ids=()):
        self.updates = []
        self.fail_ids = set(fail_ids)
        self.raise_ids = set(raise_ids)

    def table(self, name):
        client = self

        class _T:
            def __init__(self):
                self._patch = None
                self._id = None

            def update(self, patch):
                self._patch = patch
                return self

            def eq(self, col, val):
                if col == "id":
                    self._id = val
                return self

            def execute(self):
                if self._id in client.raise_ids:
                    raise RuntimeError("boom")
                client.updates.append((self._id, dict(self._patch)))
                data = [] if self._id in client.fail_ids else [dict(self._patch)]
                return type("R", (), {"data": data})()

        return _T()


# ── _match_open_commitment ──────────────────────────────────────────────

def test_match_exact_id_wins_over_substring(srv):
    rows = [_row("aaa", "review the budget"), _row("bbb", "id is aaa in the text")]
    row, err = srv._match_open_commitment(rows, "aaa")
    assert err is None and row["id"] == "aaa"


def test_match_unique_substring_case_insensitive(srv):
    rows = [_row("aaa", "Review Infoblox BUDGET"), _row("bbb", "ship the deck")]
    row, err = srv._match_open_commitment(rows, "budget")
    assert err is None and row["id"] == "aaa"


def test_match_none_reports_open_count(srv):
    rows = [_row("aaa", "ship the deck")]
    row, err = srv._match_open_commitment(rows, "zzz")
    assert row is None and err["open_count"] == 1


def test_match_ambiguous_returns_candidates_never_guesses(srv):
    rows = [_row(f"r{i}", f"deck task {i}") for i in range(7)]
    row, err = srv._match_open_commitment(rows, "deck")
    assert row is None
    assert "7" in err["error"]
    assert len(err["candidates"]) == 5  # capped


# ── #367: the sweep's 8-character ids are keys ─────────────────────────

_U1 = "40b36fb6-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
_U2 = "576f1de3-aaaa-4bbb-8ccc-dddddddddddd"
_U3 = "40b36fb6-ffff-4eee-8ddd-cccccccccccc"  # shares _U1's first 8


def test_match_a_short_id_from_the_sweep(srv):
    """`commitments_sweep` prints `40b36fb6 ↔ 576f1de3`; before #367 that
    key was "no open commitment matches"."""
    rows = [_row(_U1, "Send the recut"), _row(_U2, "Send the recut again")]
    row, err = srv._match_open_commitment(rows, "576f1de3")
    assert err is None and row["id"] == _U2


def test_match_prefix_is_case_insensitive_and_may_run_into_the_dashes(srv):
    rows = [_row(_U1, "a"), _row(_U2, "b")]
    for key in ("40B36FB6", "40b36fb6-1c2d", " 40b36fb6 "):
        row, err = srv._match_open_commitment(rows, key)
        assert err is None and row["id"] == _U1, key


def test_match_ambiguous_prefix_returns_candidates_never_guesses(srv):
    rows = [_row(_U1, "Send the recut"), _row(_U3, "Book the studio")]
    row, err = srv._match_open_commitment(rows, "40b36fb6")
    assert row is None
    assert {c["id"] for c in err["candidates"]} == {_U1, _U3}
    # A longer prefix disambiguates.
    row, err = srv._match_open_commitment(rows, "40b36fb6-f")
    assert err is None and row["id"] == _U3


def test_match_prefix_shorter_than_8_is_not_an_id(srv):
    """Seven hex characters fall through to the description substring —
    a short id must be long enough not to collide by accident."""
    rows = [_row(_U1, "Send the recut")]
    row, err = srv._match_open_commitment(rows, "40b36fb")
    assert row is None and "no open commitment" in err["error"]


def test_match_prefix_only_sees_open_rows(srv):
    """The snapshot is OPEN rows; a prefix of a closed row matches nothing,
    and the batch's shrinking snapshot keeps a closed row from re-matching."""
    rows = [_row(_U2, "Send the recut")]
    row, err = srv._match_open_commitment(rows, "40b36fb6")
    assert row is None and err["open_count"] == 1


def test_batch_resolves_sweep_short_ids(srv):
    client = _FakeClient()
    rows = [_row(_U1, "a"), _row(_U2, "b")]
    resolved, results, remaining = srv._resolve_commitment_batch(
        client, rows, ["40b36fb6", "576f1de3", "40b36fb6"], "dropped"
    )
    assert resolved == 2 and remaining == []
    assert [u[0] for u in client.updates] == [_U1, _U2]
    assert "error" in results[2]  # the repeated key: already taken


# ── _close_commitment_row ───────────────────────────────────────────────

def test_close_writes_status_and_updated_at(srv):
    client = _FakeClient()
    out = srv._close_commitment_row(client, _row("aaa", "d"), "done")
    assert out["resolved"] == "aaa" and out["outcome"] == "done"
    (rid, patch), = client.updates
    assert rid == "aaa" and patch["status"] == "done" and patch["updated_at"]


def test_close_zero_rows_is_an_error_not_a_phantom_win(srv):
    client = _FakeClient(fail_ids={"aaa"})
    out = srv._close_commitment_row(client, _row("aaa", "d"), "done")
    assert "error" in out and out["commitment_id"] == "aaa"


# ── _resolve_commitment_batch ───────────────────────────────────────────

def test_batch_miss_does_not_abort_and_reports_per_key(srv):
    rows = [_row("aaa", "ship deck"), _row("bbb", "review budget")]
    client = _FakeClient()
    resolved, results, remaining = srv._resolve_commitment_batch(
        client, rows, ["nope", "aaa", "budget"], "done"
    )
    assert resolved == 2
    assert [("error" in r) for r in results] == [True, False, False]
    assert remaining == []


def test_batch_closed_row_leaves_snapshot_no_double_match(srv):
    # Both keys substring-match the same single row: the second must MISS,
    # not double-write.
    rows = [_row("aaa", "ship the deck")]
    client = _FakeClient()
    resolved, results, remaining = srv._resolve_commitment_batch(
        client, rows, ["ship", "deck"], "done"
    )
    assert resolved == 1
    assert len(client.updates) == 1
    assert "error" in results[1]


def test_batch_transport_error_recorded_and_continues(srv):
    rows = [_row("aaa", "ship deck"), _row("bbb", "review budget")]
    client = _FakeClient(raise_ids={"aaa"})
    resolved, results, remaining = srv._resolve_commitment_batch(
        client, rows, ["aaa", "bbb"], "done"
    )
    assert resolved == 1
    assert "error" in results[0] and results[1]["resolved"] == "bbb"
    # the errored row was NOT removed from the snapshot — it is still open
    assert [r["id"] for r in remaining] == ["aaa"]


def test_batch_policy_denial_keeps_row_in_snapshot(srv):
    rows = [_row("aaa", "ship deck")]
    client = _FakeClient(fail_ids={"aaa"})
    resolved, results, remaining = srv._resolve_commitment_batch(
        client, rows, ["aaa"], "done"
    )
    assert resolved == 0 and "error" in results[0]
    assert [r["id"] for r in remaining] == ["aaa"]


# ── COMMITMENT_COLUMNS carries the sweep's grouping key ─────────────────

def test_commitment_columns_include_source_meeting_id(srv):
    assert "source_meeting_id" in srv.COMMITMENT_COLUMNS


# ── #159 part 3: off-project partition + routed-copy construction ───────


def test_routed_copy_strips_annotation_adds_provenance(srv):
    row = {
        **_row("bbb", "Fix the budget field [off-project? → mission-control]",
               meeting="m1"),
        "owner_email": "drew@firstperson.is",
        "owner_name": "Drew Fiero",
        "direction": "internal",
        "due_date": "2026-08-10",
        "date_status": "agreed",
        "source_kind": "meeting_ingest",
    }
    copy = srv._routed_copy_row(row, "ibx-5192", {"kind": "project", "id": "init-1"})
    assert copy["description"] == "Fix the budget field [routed from ibx-5192]"
    assert copy["status"] == "open"
    assert copy["owner_email"] == "drew@firstperson.is"
    assert copy["date_status"] == "agreed"          # ratification survives
    assert copy["source_kind"] == "meeting_ingest"  # origin survives
    assert copy["source_meeting_id"] == "m1"        # sweep linkage survives
    assert copy["project_id"] == "init-1"  # one owner column (#301)


def test_routed_copy_targets_project_column_for_engagements(srv):
    copy = srv._routed_copy_row(_row("aaa", "d"), "src", {"kind": "project", "id": "p1"})
    assert copy["project_id"] == "p1" and "initiative_id" not in copy  # never the retired column


def test_routed_copy_defaults_when_source_fields_null(srv):
    copy = srv._routed_copy_row(
        {"id": "aaa", "description": "d", "owner_email": None, "owner_name": None,
         "direction": None, "due_date": None, "date_status": None,
         "source_kind": None, "source_meeting_id": None},
        "src", {"kind": "project", "id": "p1"},
    )
    assert copy["direction"] == "internal"
    assert copy["date_status"] == "proposed"
    assert copy["source_kind"] == "session"
