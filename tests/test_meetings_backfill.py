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
    """Records `.select(...)` columns + every `.range(...)` and serves the rows
    for the requested half-open PostgREST range (inclusive start, inclusive end).

    A client with no `.range()` call returns ALL rows on a single `.execute()`
    (so the OLD single-fetch driver, were it still in place, would see every row
    — the pagination test instead asserts via the recorded ranges that the new
    driver actually paginated)."""

    def __init__(self, rows, recorder):
        self._rows = rows
        self._recorder = recorder
        self._range = None

    def select(self, cols):
        self._recorder["select_cols"] = cols
        return self

    def range(self, start, end):
        self._recorder.setdefault("ranges", []).append((start, end))
        self._range = (start, end)
        return self

    def execute(self):
        if self._range is None:
            data = list(self._rows)
        else:
            start, end = self._range
            data = list(self._rows[start:end + 1])  # PostgREST end is inclusive

        class _Resp:
            pass

        resp = _Resp()
        resp.data = data
        return resp


class _FakeListClient:
    """Fake client whose only job is to serve the fathom_meetings listing.

    A fresh chain per `.table()` call so each paginated fetch gets its own
    `.range()` state, but all share one `recorder` dict."""

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
    # ok:True, linked:False is a LEGITIMATE skip (e.g. "already embedded"),
    # distinct from an ok:False ERROR (which counts as failed — see below).
    rows = [_row(1, ["T"]), _row(2, ["T"])]
    monkeypatch.setattr(meetings, "resolve_meeting_project", _resolver_by_tag({"T": "pid"}))
    link = _LinkRecorder(
        results={
            1: {"ok": True, "linked": True, "project_id": "pid"},
            2: {"ok": True, "linked": False, "reason": "already embedded"},
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
    assert summary["failed"] == 0


def test_link_ok_false_counts_as_failed_not_skipped(monkeypatch):
    # link_meeting NEVER raises — it self-wraps errors as {"ok": False, ...}.
    # A genuine ERROR must count as FAILED (so the CLI exits non-zero), NOT be
    # silently swallowed as a skip.
    rows = [_row(1, ["T"]), _row(7, ["T"])]
    monkeypatch.setattr(meetings, "resolve_meeting_project", _resolver_by_tag({"T": "pid"}))
    link = _LinkRecorder(
        results={
            1: {"ok": True, "linked": True, "project_id": "pid"},
            7: {"ok": False, "reason": "embed exploded"},
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
    assert summary["skipped"] == 0
    assert summary["failed"] == 1


def test_nested_embed_failure_counts_as_failed_not_linked(monkeypatch):
    # link_meeting returns top-level {ok:True, linked:True} once the link WRITE
    # succeeds, nesting the embed outcome under `out["embed"]`. A genuine embed
    # outage (Voyage/OpenAI down, stamp matched 0 rows) surfaces as
    # embed.ok==False. The backfill MUST classify that row as FAILED — counting
    # it as `linked` defeats the embed-outage alarm and lets the CLI exit 0.
    rows = [_row(1, ["T"]), _row(9, ["T"])]
    monkeypatch.setattr(meetings, "resolve_meeting_project", _resolver_by_tag({"T": "pid"}))
    link = _LinkRecorder(
        results={
            1: {"ok": True, "linked": True, "project_id": "pid",
                "embed": {"ok": True}},
            9: {"ok": True, "linked": True, "project_id": "pid",
                "embed": {"ok": False, "reason": "voyage down"}},
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
    assert summary["skipped"] == 0
    assert summary["failed"] == 1
    # The embed sub-failure is surfaced in `failures` with the embed reason.
    assert any(rid == 9 and "voyage down" in (reason or "")
               for rid, reason in summary["failures"])


def test_nested_embed_already_embedded_counts_as_skipped_not_failed(monkeypatch):
    # The PRODUCTION idempotent-rerun case: link_meeting returns
    # {ok:True, linked:True, embed:{ok:False, reason:"summary already embedded"}}
    # when the meeting's summary is ALREADY in RAG (summary_embedded_at set). That
    # is a benign idempotent SKIP, NOT an embed outage — a re-run of backfill (or
    # --all after a scoped run) must NOT report it as `failed` / exit non-zero, or
    # every re-run cries wolf. Only GENUINE embed failures (voyage down, stamp
    # matched no row) are `failed`.
    rows = [_row(1, ["T"]), _row(9, ["T"])]
    monkeypatch.setattr(meetings, "resolve_meeting_project", _resolver_by_tag({"T": "pid"}))
    link = _LinkRecorder(
        results={
            1: {"ok": True, "linked": True, "project_id": "pid",
                "embed": {"ok": True}},
            9: {"ok": True, "linked": True, "project_id": "pid",
                "embed": {"ok": False, "reason": "summary already embedded"}},
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
    assert summary["skipped"] == 1   # the already-embedded row
    assert summary["failed"] == 0    # NOT a failure
    assert summary["failures"] == []


def test_nested_embed_no_summary_counts_as_skipped_not_failed(monkeypatch):
    # A meeting with no summary text returns embed {ok:False, reason:"meeting has
    # no summary"} — also benign (nothing to embed), a SKIP not a failure.
    rows = [_row(9, ["T"])]
    monkeypatch.setattr(meetings, "resolve_meeting_project", _resolver_by_tag({"T": "pid"}))
    link = _LinkRecorder(
        results={
            9: {"ok": True, "linked": True, "project_id": "pid",
                "embed": {"ok": False, "reason": "meeting has no summary"}},
        }
    )
    summary = backfill_meetings(
        _FakeListClient(rows),
        supabase_url="u",
        supabase_key="k",
        link=link,
        company_resolver=lambda c, pid: "cid",
    )
    assert summary["skipped"] == 1
    assert summary["failed"] == 0


def test_untagged_linked_false_no_embed_key_stays_skipped(monkeypatch):
    # An untagged / unresolvable meeting returns {ok:True, linked:False} with NO
    # `embed` key. The new embed-failure check must NOT over-trigger on the
    # ABSENCE of an embed key — this row must stay `skipped`, not `failed`.
    rows = [_row(1, ["T"]), _row(2, ["T"])]
    monkeypatch.setattr(meetings, "resolve_meeting_project", _resolver_by_tag({"T": "pid"}))
    link = _LinkRecorder(
        results={
            1: {"ok": True, "linked": True, "project_id": "pid"},
            2: {"ok": True, "linked": False, "reason": "no resolvable project tag"},
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
    assert summary["failed"] == 0


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


# ──────────────────────────────────────────────────────────────────────
#  initiative rows deferred (no FK-violating project_id write)
# ──────────────────────────────────────────────────────────────────────


def test_initiative_resolved_row_deferred_not_linked(monkeypatch):
    """v1 DEFER: a row whose tag resolves to an INITIATIVE (company_resolver →
    None) must be DEFERRED — link is called with is_engagement=False so no
    FK-violating fathom_meetings.project_id write is attempted. It counts as a
    skip (an intentional defer), NOT a hard failure, and is not in `unresolved`
    (it DID resolve to an id — just not an engagement)."""
    rows = [
        _row(1, ["IBX 5167 DDI Platform Video"]),  # engagement → company present
        _row(2, ["Mission Control"]),  # initiative → company None → defer
    ]
    monkeypatch.setattr(
        meetings,
        "resolve_meeting_project",
        _resolver_by_tag(
            {"IBX 5167 DDI Platform Video": "pid-ibx", "Mission Control": "init-1"}
        ),
    )

    captured = {}

    def link(client, meeting_row, **kwargs):
        captured[meeting_row["recording_id"]] = kwargs.get("is_engagement")
        if kwargs.get("is_engagement") is False:
            return {"ok": True, "linked": False,
                    "reason": "initiative meetings are deferred in v1"}
        return {"ok": True, "linked": True, "project_id": "pid-ibx"}

    # Engagement project has a company; initiative has none.
    company_by_pid = {"pid-ibx": "cid-ibx", "init-1": None}

    summary = backfill_meetings(
        _FakeListClient(rows),
        supabase_url="u",
        supabase_key="k",
        link=link,
        company_resolver=lambda c, pid: company_by_pid[pid],
    )

    # Engagement got is_engagement True; initiative got False (deferred).
    assert captured[1] is True
    assert captured[2] is False
    assert summary["total"] == 2
    assert summary["linked"] == 1
    assert summary["skipped"] == 1   # the deferred initiative is a skip
    assert summary["failed"] == 0    # NOT a hard failure
    assert summary["unresolved"] == []  # it resolved to an id, just not engagement


# ──────────────────────────────────────────────────────────────────────
#  pagination — no silent >page-size cap
# ──────────────────────────────────────────────────────────────────────


def test_listing_paginates_no_silent_cap(monkeypatch):
    # 5 rows with a page size of 2 → 3 fetches (2, 2, 1). All 5 must process;
    # nothing is capped at the first PostgREST page.
    monkeypatch.setattr(meetings, "_BACKFILL_PAGE_SIZE", 2)
    rows = [_row(i, ["T"]) for i in range(1, 6)]
    monkeypatch.setattr(meetings, "resolve_meeting_project", _resolver_by_tag({"T": "pid"}))
    link = _LinkRecorder()
    client = _FakeListClient(rows)

    summary = backfill_meetings(
        client,
        supabase_url="u",
        supabase_key="k",
        link=link,
        company_resolver=lambda c, pid: "cid",
    )

    assert summary["total"] == 5
    assert summary["linked"] == 5
    assert [c["row"]["recording_id"] for c in link.calls] == [1, 2, 3, 4, 5]
    # Pagination actually happened: ranges walked 0-1, 2-3, 4-5 (last short page
    # of 1 row < page size stops the loop).
    assert client.recorder["ranges"] == [(0, 1), (2, 3), (4, 5)]


def test_listing_stops_when_page_exactly_fills_then_empties(monkeypatch):
    # 4 rows, page size 2 → 2 full pages then an empty third page stops it.
    monkeypatch.setattr(meetings, "_BACKFILL_PAGE_SIZE", 2)
    rows = [_row(i, ["T"]) for i in range(1, 5)]
    monkeypatch.setattr(meetings, "resolve_meeting_project", _resolver_by_tag({"T": "pid"}))
    client = _FakeListClient(rows)

    summary = backfill_meetings(
        client,
        supabase_url="u",
        supabase_key="k",
        link=_LinkRecorder(),
        company_resolver=lambda c, pid: "cid",
    )
    assert summary["total"] == 4
    # 0-1 (full), 2-3 (full), 4-5 (empty → stop).
    assert client.recorder["ranges"] == [(0, 1), (2, 3), (4, 5)]


# ──────────────────────────────────────────────────────────────────────
#  company memoization — one lookup per distinct project
# ──────────────────────────────────────────────────────────────────────


def test_company_resolver_memoized_per_project(monkeypatch):
    # 3 rows, two distinct projects → resolver called once PER PROJECT, not
    # once per row.
    rows = [
        _row(1, ["IBX 5167 DDI Platform Video"]),  # pid-ibx
        _row(2, ["IBX 5167 DDI Platform Video"]),  # pid-ibx (memo hit)
        _row(3, ["GGL 5168 Activation"]),  # pid-ggl
    ]
    monkeypatch.setattr(
        meetings,
        "resolve_meeting_project",
        _resolver_by_tag(
            {"IBX 5167 DDI Platform Video": "pid-ibx", "GGL 5168 Activation": "pid-ggl"}
        ),
    )

    seen = []

    def counting_resolver(client, pid):
        seen.append(pid)
        return f"cid-{pid}"

    backfill_meetings(
        _FakeListClient(rows),
        supabase_url="u",
        supabase_key="k",
        link=_LinkRecorder(),
        company_resolver=counting_resolver,
    )

    # Exactly one lookup per distinct project (pid-ibx once, pid-ggl once).
    assert sorted(seen) == ["pid-ggl", "pid-ibx"]
