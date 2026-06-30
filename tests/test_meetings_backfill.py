"""Tests for `backfill_meetings` — the batch driver that runs the existing
`link_meeting` over the already-tagged `fathom_meetings` rows (no Fathom API).

Everything is INJECTED: a fake Supabase client whose
`.table("fathom_meetings").select(...).execute()` returns canned rows; an
injected `link` recorder standing in for `link_meeting`; an injected
`company_resolver` so nothing touches Supabase/Voyage. The module-level
`resolve_meeting_project` is monkeypatched to control tag → project resolution.

KEY correctness points exercised here:
  (a) the listing select() NEVER asks for the heavy `transcript` column / never
      `select *`;
  (b) unresolved tagged rows are SURFACED (in `unresolved`), not silently dropped,
      and link is NOT called for them;
  (c) per-row failure isolation: one row's link raising never aborts the batch;
  (d) the rescope closure threaded to link carries the company of the MEETING's
      OWN resolved project.
"""
from __future__ import annotations

import cp_engine.meetings as meetings
from cp_engine.meetings import backfill_meetings


# ──────────────────────────────────────────────────────────────────────
#  Fakes
# ──────────────────────────────────────────────────────────────────────


class _FakeSelectChain:
    """Records the columns passed to `.select(...)` and returns canned rows."""

    def __init__(self, rows, recorder):
        self._rows = rows
        self._recorder = recorder

    def select(self, cols):
        self._recorder["select_cols"] = cols
        return self

    def execute(self):
        class _Resp:
            data = list(self._rows)

        return _Resp()


class _FakeListClient:
    """Fake client whose only job is to serve the fathom_meetings listing."""

    def __init__(self, rows):
        self.rows = rows
        self.recorder: dict = {}

    def table(self, name):
        assert name == "fathom_meetings"
        return _FakeSelectChain(self.rows, self.recorder)


class _LinkRecorder:
    """Stand-in for `link_meeting`: records every call, returns canned results.

    `results` maps a meeting's recording_id → the dict link should return, or a
    `raises` exception class → raise it for that row. Default: linked True.
    """

    def __init__(self, results=None, raises_for=None):
        self.calls = []
        self._results = results or {}
        self._raises_for = raises_for or {}

    def __call__(self, client, meeting_row, **kwargs):
        self.calls.append({"row": meeting_row, "kwargs": kwargs})
        rid = meeting_row.get("recording_id")
        if rid in self._raises_for:
            raise self._raises_for[rid]
        return self._results.get(
            rid, {"ok": True, "linked": True, "project_id": "pid-x"}
        )


def _row(rid, tags, **overrides):
    base = {
        "recording_id": rid,
        "title": f"Meeting {rid}",
        "project_tags": tags,
        "project_id": None,
        "summary": "notes",
        "summary_embedded_at": None,
    }
    base.update(overrides)
    return base


def _resolver_by_tag(mapping):
    """Build a `resolve_meeting_project` replacement: tag-string → project_id."""

    def _fake(client, tags, **kwargs):
        for t in tags or []:
            pid = mapping.get(t)
            if pid:
                return pid, t
        return None, None

    return _fake


# ──────────────────────────────────────────────────────────────────────
#  all-rows backfill
# ──────────────────────────────────────────────────────────────────────


def test_all_rows_backfill_links_each_tagged_row(monkeypatch):
    rows = [
        _row(1, ["IBX 5167 DDI Platform Video"]),
        _row(2, ["IBX 5167 DDI Platform Video"]),
        _row(3, ["GGL 5168 Activation"]),
    ]
    monkeypatch.setattr(
        meetings,
        "resolve_meeting_project",
        _resolver_by_tag(
            {"IBX 5167 DDI Platform Video": "pid-ibx", "GGL 5168 Activation": "pid-ggl"}
        ),
    )
    link = _LinkRecorder()
    client = _FakeListClient(rows)

    summary = backfill_meetings(
        client,
        supabase_url="u",
        supabase_key="k",
        link=link,
        company_resolver=lambda c, pid: "cid-1",
    )

    assert len(link.calls) == 3
    assert summary["total"] == 3
    assert summary["linked"] == 3
    assert summary["failed"] == 0
    assert summary["unresolved"] == []


def test_link_result_not_linked_counts_as_skipped(monkeypatch):
    rows = [_row(1, ["T"]), _row(2, ["T"])]
    monkeypatch.setattr(meetings, "resolve_meeting_project", _resolver_by_tag({"T": "pid"}))
    link = _LinkRecorder(
        results={
            1: {"ok": True, "linked": True, "project_id": "pid"},
            2: {"ok": True, "linked": False, "reason": "already"},
        }
    )

    summary = backfill_meetings(
        _FakeListClient(rows),
        supabase_url="u",
        supabase_key="k",
        link=link,
        company_resolver=lambda c, pid: "cid",
    )
    assert summary["total"] == 2
    assert summary["linked"] == 1
    assert summary["skipped"] == 1


# ──────────────────────────────────────────────────────────────────────
#  untagged rows skipped (never passed to link)
# ──────────────────────────────────────────────────────────────────────


def test_untagged_rows_skipped_not_linked(monkeypatch):
    rows = [
        _row(1, ["IBX 5167 DDI Platform Video"]),
        _row(2, []),  # empty tags
        _row(3, ["untagged"]),  # marker
        _row(4, None),  # null tags
    ]
    monkeypatch.setattr(
        meetings,
        "resolve_meeting_project",
        _resolver_by_tag({"IBX 5167 DDI Platform Video": "pid"}),
    )
    link = _LinkRecorder()

    summary = backfill_meetings(
        _FakeListClient(rows),
        supabase_url="u",
        supabase_key="k",
        link=link,
        company_resolver=lambda c, pid: "cid",
    )

    # Only the one tagged row reaches link.
    assert [c["row"]["recording_id"] for c in link.calls] == [1]
    assert summary["total"] == 4
    assert summary["linked"] == 1
    assert summary["skipped"] == 3
    # Untagged is NOT a resolution failure — these are not in `unresolved`.
    assert summary["unresolved"] == []


# ──────────────────────────────────────────────────────────────────────
#  unresolved tag (tagged, but tag doesn't map to a project) — surfaced
# ──────────────────────────────────────────────────────────────────────


def test_unresolved_tag_surfaced_and_not_linked(monkeypatch):
    rows = [
        _row(1, ["IBX 5167 DDI Platform Video"]),
        _row(99, ["Tag That Does Not Resolve"]),
    ]
    monkeypatch.setattr(
        meetings,
        "resolve_meeting_project",
        _resolver_by_tag({"IBX 5167 DDI Platform Video": "pid"}),
    )
    link = _LinkRecorder()

    summary = backfill_meetings(
        _FakeListClient(rows),
        supabase_url="u",
        supabase_key="k",
        link=link,
        company_resolver=lambda c, pid: "cid",
    )

    # The unresolved row never reaches link.
    assert [c["row"]["recording_id"] for c in link.calls] == [1]
    assert summary["linked"] == 1
    # The unresolved row is surfaced (recording_id or title), NOT dropped.
    assert len(summary["unresolved"]) == 1
    surfaced = str(summary["unresolved"][0])
    assert "99" in surfaced or "Meeting 99" in surfaced


# ──────────────────────────────────────────────────────────────────────
#  code filter
# ──────────────────────────────────────────────────────────────────────


def test_code_filter_processes_only_matching_project(monkeypatch):
    rows = [
        _row(1, ["IBX 5167 DDI Platform Video"]),  # → pid-ibx
        _row(2, ["GGL 5168 Activation"]),  # → pid-ggl
        _row(3, ["IBX 5167 DDI Platform Video"]),  # → pid-ibx
    ]
    monkeypatch.setattr(
        meetings,
        "resolve_meeting_project",
        _resolver_by_tag(
            {"IBX 5167 DDI Platform Video": "pid-ibx", "GGL 5168 Activation": "pid-ggl"}
        ),
    )
    # code "ibx-5167" resolves to pid-ibx.
    monkeypatch.setattr(
        meetings, "_default_resolver", lambda client, code: "pid-ibx"
    )
    link = _LinkRecorder()

    summary = backfill_meetings(
        _FakeListClient(rows),
        code="ibx-5167",
        supabase_url="u",
        supabase_key="k",
        link=link,
        company_resolver=lambda c, pid: "cid",
    )

    assert [c["row"]["recording_id"] for c in link.calls] == [1, 3]
    assert summary["linked"] == 2
    assert summary["skipped"] == 1  # the GGL row filtered out


# ──────────────────────────────────────────────────────────────────────
#  per-row failure isolation
# ──────────────────────────────────────────────────────────────────────


def test_one_row_failure_isolated_batch_continues(monkeypatch):
    rows = [_row(1, ["T"]), _row(2, ["T"]), _row(3, ["T"])]
    monkeypatch.setattr(meetings, "resolve_meeting_project", _resolver_by_tag({"T": "pid"}))
    link = _LinkRecorder(raises_for={2: RuntimeError("boom on 2")})

    summary = backfill_meetings(
        _FakeListClient(rows),
        supabase_url="u",
        supabase_key="k",
        link=link,
        company_resolver=lambda c, pid: "cid",
    )

    # All three attempted despite row 2 raising.
    assert [c["row"]["recording_id"] for c in link.calls] == [1, 2, 3]
    assert summary["total"] == 3
    assert summary["linked"] == 2
    assert summary["failed"] == 1


def test_batch_never_raises_on_row_error(monkeypatch):
    rows = [_row(1, ["T"])]
    monkeypatch.setattr(meetings, "resolve_meeting_project", _resolver_by_tag({"T": "pid"}))
    link = _LinkRecorder(raises_for={1: ValueError("nope")})
    # Should NOT raise.
    summary = backfill_meetings(
        _FakeListClient(rows),
        supabase_url="u",
        supabase_key="k",
        link=link,
        company_resolver=lambda c, pid: "cid",
    )
    assert summary["failed"] == 1


# ──────────────────────────────────────────────────────────────────────
#  NEVER select the heavy transcript column / never select *
# ──────────────────────────────────────────────────────────────────────


def test_listing_select_excludes_transcript_and_is_explicit(monkeypatch):
    rows = [_row(1, ["T"])]
    monkeypatch.setattr(meetings, "resolve_meeting_project", _resolver_by_tag({"T": "pid"}))
    client = _FakeListClient(rows)

    backfill_meetings(
        client,
        supabase_url="u",
        supabase_key="k",
        link=_LinkRecorder(),
        company_resolver=lambda c, pid: "cid",
    )

    cols = client.recorder["select_cols"]
    assert cols != "*"
    assert "transcript" not in cols
    # Explicit minimum columns the contract requires are present.
    for needed in ("recording_id", "title", "project_tags", "project_id",
                   "summary", "summary_embedded_at"):
        assert needed in cols


# ──────────────────────────────────────────────────────────────────────
#  company threading — rescope closure carries the meeting's-project company
# ──────────────────────────────────────────────────────────────────────


def test_rescope_closure_threads_meeting_project_company(monkeypatch):
    rows = [
        _row(1, ["IBX 5167 DDI Platform Video"]),
        _row(2, ["GGL 5168 Activation"]),
    ]
    monkeypatch.setattr(
        meetings,
        "resolve_meeting_project",
        _resolver_by_tag(
            {"IBX 5167 DDI Platform Video": "pid-ibx", "GGL 5168 Activation": "pid-ggl"}
        ),
    )
    # company_resolver maps each project to a DIFFERENT company.
    company_by_pid = {"pid-ibx": "cid-ibx", "pid-ggl": "cid-ggl"}

    captured_rescope = {}

    def link(client, meeting_row, **kwargs):
        rid = meeting_row["recording_id"]
        rescope = kwargs["rescope"]
        # Invoke the closure exactly like link_meeting would on a retag and
        # capture the company it threads.
        recorder = {"co": None}

        def fake_rescope_meeting(c, row, new_pid, *, new_company_id=None):
            recorder["co"] = new_company_id
            return {"ok": True}

        monkeypatch.setattr(meetings, "rescope_meeting", fake_rescope_meeting)
        rescope(client, meeting_row, "new-pid")
        captured_rescope[rid] = recorder["co"]
        return {"ok": True, "linked": True}

    backfill_meetings(
        _FakeListClient(rows),
        supabase_url="u",
        supabase_key="k",
        link=link,
        company_resolver=lambda c, pid: company_by_pid[pid],
    )

    # Each meeting's rescope closure threaded its OWN project's company.
    assert captured_rescope[1] == "cid-ibx"
    assert captured_rescope[2] == "cid-ggl"
