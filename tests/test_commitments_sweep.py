"""Tests for cp_engine.commitments_sweep (#135) — grouping, filters, render."""
from __future__ import annotations

from datetime import date

from cp_engine.commitments_sweep import SweepRow, _row, render_sweep

_TODAY = date(2026, 8, 6)


def _c(
    id: str,
    desc: str,
    *,
    due: str | None = None,
    created: str = "2026-07-14",
    source_kind: str = "meeting_ingest",
    date_status: str = "proposed",
) -> dict:
    return {
        "id": id,
        "description": desc,
        "owner_email": "marcello@firstperson.is",
        "owner_name": "Marcello Grande",
        "due_date": due,
        "date_status": date_status,
        "status": "open",
        "source_kind": source_kind,
        "project_id": "p1",
        "initiative_id": None,
        "created_at": created + "T12:00:00+00:00",
    }


def test_row_ages_and_flags() -> None:
    r = _row(_c("a", "Old undated", created="2026-07-14"), _TODAY)
    assert r.age_days == 23
    assert r.undated and r.stale
    assert r.ttl == "expire"  # undated meeting_ingest proposed, 23d

    dated = _row(_c("b", "Dated", due="2026-08-04"), _TODAY)
    assert not dated.undated and not dated.stale and dated.ttl is None

    fresh = _row(_c("c", "Fresh", created="2026-08-01"), _TODAY)
    assert fresh.undated and not fresh.stale and fresh.ttl is None

    session = _row(_c("d", "Session", source_kind="session"), _TODAY)
    assert session.stale and session.ttl is None  # stale but TTL-exempt


def test_render_sweep_shapes() -> None:
    rows = [
        _row(_c("a", "Add Fred's fear to Hopes & Fears board"), _TODAY),
        _row(_c("b", "Write up notes from the Jul 29 customer session",
                due="2026-08-04", source_kind="session"), _TODAY),
        _row(_c("c", "Future thing", due="2026-08-20", date_status="agreed"), _TODAY),
    ]
    text = render_sweep({"sap-5174-vision-update-2026": rows}, today=_TODAY)
    assert "sap-5174-vision-update-2026 — 3 open" in text
    assert "⚠ UNDATED · 23d" in text
    assert "[meeting_ingest]  Marcello Grande" in text
    assert "SLIPPED · due 2026-08-04 (2d ago)" in text
    assert "due 2026-08-20 [agreed]" in text
    assert "past the 14d TTL — expires at the next daily sync unless dated" in text
    assert "3 open across 1 project(s)" in text
    assert "1 stale" in text


def test_render_sweep_empty() -> None:
    assert render_sweep({}, today=_TODAY) == "No open commitments match."


def test_sweep_row_properties() -> None:
    r = SweepRow(
        id="x", description="d", owner="o", source_kind="manual",
        due_date=None, date_status="proposed", age_days=14, ttl=None,
    )
    assert r.stale


# --- the scope column: engagements must not fall through to initiatives ----


class _FakeQuery:
    """Records .eq() calls so a test can assert WHICH column was filtered."""

    def __init__(self, rows: list[dict], calls: list[tuple[str, object]]):
        self._rows, self.calls = rows, calls

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self.calls.append((col, val))
        return self

    def execute(self):
        class _R:
            data = self._rows
        return _R()


class _FakeClient:
    def __init__(self, rows: list[dict]):
        self._rows, self.calls = rows, []

    def table(self, _name):
        return _FakeQuery(self._rows, self.calls)


def test_sweep_scopes_an_engagement_by_project_id(monkeypatch) -> None:
    """Regression: `resolve_commitment_owner` emits kind='project', never
    'engagement'.

    The filter previously read `"project_id" if kind == "engagement" else
    "initiative_id"` — so EVERY engagement fell through to `initiative_id`
    and matched nothing. `cp commitments-sweep ibx-5192` reported "No open
    commitments match" against 72 real open rows, and that empty result was
    carried into a close-out retro as fact.
    """
    from cp_engine import commitments_sweep as cs

    monkeypatch.setattr(
        cs, "_owner_codes", lambda _c: {}, raising=False
    )
    import cp_engine.commitments as _cmt
    monkeypatch.setattr(
        _cmt, "resolve_commitment_owner",
        lambda _c, _code: {"id": "proj-uuid", "code": "ibx-5192", "kind": "project"},
    )

    client = _FakeClient([_c("c1", "a real open commitment")])
    groups = cs.sweep(client, code="ibx-5192", today=_TODAY)

    assert ("project_id", "proj-uuid") in client.calls, (
        "an engagement must be scoped by project_id"
    )
    assert ("initiative_id", "proj-uuid") not in client.calls
    assert groups, "the open commitment must survive the filter"


def test_sweep_scopes_an_internal_workstream_by_project_id(monkeypatch) -> None:
    """#301: there is no second owner column — an internal workstream is
    scoped by `project_id` like every other workstream."""
    from cp_engine import commitments_sweep as cs

    monkeypatch.setattr(cs, "_owner_codes", lambda _c: {}, raising=False)
    import cp_engine.commitments as _cmt
    monkeypatch.setattr(
        _cmt, "resolve_commitment_owner",
        lambda _c, _code: {
            "id": "init-uuid", "code": "1pi-9005-mission-control", "kind": "project",
        },
    )

    client = _FakeClient([])
    cs.sweep(client, code="1pi-9005-mission-control", today=_TODAY)

    assert ("project_id", "init-uuid") in client.calls
    assert not any(col == "initiative_id" for col, _ in client.calls)


# --- #311: likely-duplicate pairs -------------------------------------------
#
# Every pair below is a REAL pair from MC-2 (2026-09-30 tuning read), trimmed
# only where noted. A hand-logged row and the row auto-ingest writes for the
# same meeting never share text, so the content-hash dedupe cannot see them;
# the sweep must.

def _rows(*pairs: tuple[str, str, str]) -> list[SweepRow]:
    """(id, description, source_kind) -> rows in one project group."""
    return [_row(_c(i, d, source_kind=k), _TODAY) for i, d, k in pairs]


def test_paraphrased_rows_for_one_obligation_are_paired() -> None:
    """The #311 shape: one obligation, two writers, no shared text."""
    from cp_engine.commitments_sweep import likely_duplicates

    rows = _rows(
        ("s1", "Written interpretation of Brad's feedback + clarifying questions "
               "sent to Janet for her email to Jaime/Brad", "session"),
        ("m1", "Email to Janet + Brad with team's interpretation of Brad's "
               "feedback and clarifying questions", "meeting_ingest"),
        ("m2", "Book the Fort Worth studio for the pre-light", "meeting_ingest"),
    )
    pairs = likely_duplicates(rows)
    assert [(p.a.id, p.b.id) for p in pairs] == [("s1", "m1")]


def test_short_row_contained_in_a_longer_one_is_paired() -> None:
    from cp_engine.commitments_sweep import is_likely_duplicate

    assert is_likely_duplicate(
        "Confirm poster print specs w/ Tony; print posters at FedEx Kinko's",
        "Confirm poster print specs w/ Tony; print posters + worksheets at FedEx Kinko's",
    )


def test_ingest_annotations_do_not_count_as_shared_content() -> None:
    """`[confidence: medium]`, `(from X (Co))`, `[off-project? → code]` are
    provenance the writers append. Two unrelated rows that both carry
    `[off-project? → ibx-5153-ai-campaign]` paired on it before stripping."""
    from cp_engine.commitments_sweep import is_likely_duplicate, similarity

    a = ("Review Infoblox AI doc (Final V1) from Slack; extract proof points "
         "[off-project? → ibx-5153-ai-campaign]")
    b = ("Collect Infoblox feedback on 1-pager by Sep 21 EOD; compile "
         "[off-project? → ibx-5153-ai-campaign]")
    assert not is_likely_duplicate(a, b)
    jac_with, _, _ = similarity(a + " [confidence: low]", b + " (from Tara Haney (SAP Concur))")
    jac_bare, _, _ = similarity(a, b)
    assert jac_with == jac_bare


def test_numbered_siblings_are_not_duplicates() -> None:
    """Revision rounds, invoice tranches and differently-priced projects are
    separate obligations that read alike — all real false positives before
    the number rule."""
    from cp_engine.commitments_sweep import is_likely_duplicate

    assert not is_likely_duplicate("Deck Design r1", "Deck Design r2")
    assert not is_likely_duplicate("Narrative and Talk Track r2", "Narrative and Talk Track r3")
    assert not is_likely_duplicate(
        "Invoice #1 (50%) for SAP 5198 ad videos — ~$212,500",
        "Invoice #2 (25%) for SAP 5198 ad videos — must land in 2026 tax year [confidence: medium]",
    )
    assert not is_likely_duplicate(
        "Spin up Q4 project: Infoblox platform display-ad campaign (from $40k, ~4–5 wks) "
        "— Janet-approved 2026-08-07, invoiced. Kickoff expected in a few weeks (not yet dated).",
        "Spin up Q4 project: AI platform video recut ($30k, ~5–6 wks active) "
        "— Janet-approved 2026-08-07, invoiced. Kickoff expected in a few weeks (not yet dated).",
    )


def test_a_long_rows_stray_date_does_not_veto() -> None:
    """A re-logged session row that adds one id or date is still the same row."""
    from cp_engine.commitments_sweep import is_likely_duplicate

    base = ("Send the second team's Jul 30 workshop breakout recording. The Zoom "
            "capture covers one table only; the other team's reasoning is missing. "
            "Needed to confirm whether both teams rejected the Story Essence either/or")
    assert is_likely_duplicate(base, base.replace("capture covers", "capture (87d0004f) covers"))


def test_pairs_carry_the_same_meeting_signal() -> None:
    from cp_engine.commitments_sweep import likely_duplicates

    a = _row(_c("a", "Revised three-ad round in 'Truth AI depends on' direction, "
                     "delivered to Janet ahead of Jaime/Brad MLT"), _TODAY)
    b = _row(_c("b", "Revised round of three ads (refined visual direction on 'truth "
                     "AI depends on' concept) delivered to Janet / Jaime / Brad ahead of MLT"), _TODAY)
    a.source_meeting_id = b.source_meeting_id = "m-1"
    (pair,) = likely_duplicates([a, b])
    assert pair.same_meeting and 0 < pair.jaccard <= 1
    b.source_meeting_id = None
    assert not likely_duplicates([a, b])[0].same_meeting


def test_row_reads_source_meeting_id_and_the_sweep_selects_it() -> None:
    from cp_engine.commitments_sweep import _sweep_columns

    c = _c("a", "x")
    c["source_meeting_id"] = "m-9"
    assert _row(c, _TODAY).source_meeting_id == "m-9"
    assert "source_meeting_id" in _sweep_columns(None)


def test_render_shows_the_pairs_and_counts_them() -> None:
    rows = _rows(
        ("11111111-aaaa", "Written interpretation of Brad's feedback + clarifying "
                          "questions sent to Janet for her email to Jaime/Brad", "session"),
        ("22222222-bbbb", "Email to Janet + Brad with team's interpretation of Brad's "
                          "feedback and clarifying questions", "meeting_ingest"),
    )
    text = render_sweep({"ibx-5153-ai-campaign": rows}, today=_TODAY)
    assert "≈ 1 likely duplicate pair(s)" in text
    assert "11111111 ↔ 22222222" in text
    assert text.rstrip().endswith("· 1 likely duplicate pair(s)")


def test_pairing_never_crosses_projects(monkeypatch) -> None:
    """The sweep groups by project; identical text under two projects is two
    obligations (a routed copy, a promote_uphill copy) — never a pair."""
    from cp_engine import commitments_sweep as cs

    same = "Send Janet the migration runbook with the rollback plan attached"
    a, b = _c("a", same), _c("b", same)
    b["project_id"] = "p2"
    monkeypatch.setattr(cs, "_owner_codes", lambda _c: {"p1": "ibx-5153", "p2": "ibx-5192"})
    groups = cs.sweep(_FakeClient([a, b]), today=_TODAY)
    assert set(groups) == {"ibx-5153", "ibx-5192"}
    assert all(not cs.likely_duplicates(rs) for rs in groups.values())
    assert "0 likely duplicate pair(s)" in render_sweep(groups, today=_TODAY)


# ── the 14-day TTL flag (moved from the retired dates loop, step 5a) ──

_TTL_TODAY = date(2026, 7, 6)


def _ingest_c(id: str, desc: str, created: str, **kw) -> dict:
    c = {
        "id": id, "description": desc, "due_date": None, "status": "open",
        "date_status": kw.get("date_status", "proposed"),
        "source_kind": "meeting_ingest",
        "created_at": created + "T12:00:00+00:00",
    }
    return c


def test_ttl_bucket_eligibility() -> None:
    from cp_engine.commitments_sweep import _ttl_bucket

    # 14+ days undated meeting-ingest proposed → expire.
    assert _ttl_bucket(_ingest_c("a", "old", "2026-06-20"), _TTL_TODAY) == "expire"
    # 7–13 days → warn.
    assert _ttl_bucket(_ingest_c("b", "warm", "2026-06-28"), _TTL_TODAY) == "warn"
    # Fresh → None.
    assert _ttl_bucket(_ingest_c("c", "new", "2026-07-05"), _TTL_TODAY) is None
    # A due date cancels the TTL.
    dated = _ingest_c("d", "dated", "2026-06-01")
    dated["due_date"] = "2026-08-01"
    assert _ttl_bucket(dated, _TTL_TODAY) is None
    # A ratified/changed date_status cancels it.
    agreed = _ingest_c("e", "agreed", "2026-06-01", date_status="agreed")
    assert _ttl_bucket(agreed, _TTL_TODAY) is None
    # Human-authored rows are exempt regardless of age.
    session = _ingest_c("f", "session row", "2026-06-01")
    session["source_kind"] = "session"
    assert _ttl_bucket(session, _TTL_TODAY) is None
    # Unparseable created_at → never expire.
    broken = _ingest_c("g", "broken", "2026-06-01")
    broken["created_at"] = "not-a-date"
    assert _ttl_bucket(broken, _TTL_TODAY) is None
