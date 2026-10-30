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


# --- #354: the sweep hands back a ready resolve_commitments payload ---------
#
# The sweep FOUND the redundant rows but could not close them, so every
# wrap-up re-keyed each id into a separate resolve call. It still never
# resolves anything — it returns the exact argument object to send.

def test_suggested_resolutions_name_the_redundant_row() -> None:
    from cp_engine.commitments_sweep import suggested_resolutions

    rows = _rows(
        ("s1", "Written interpretation of Brad's feedback + clarifying questions "
               "sent to Janet for her email to Jaime/Brad", "session"),
        ("m1", "Email to Janet + Brad with team's interpretation of Brad's "
               "feedback and clarifying questions", "meeting_ingest"),
        ("m2", "Book the Fort Worth studio for the pre-light", "meeting_ingest"),
    )
    out = suggested_resolutions({"ibx-5153": rows})
    # The hand-logged row is kept; the ingest restatement is the one to drop.
    assert out["redundant"] == [
        {"project_code": "ibx-5153", "drop": "m1", "keep": "s1"}
    ]
    assert out["resolve_commitments"] == [
        {"project_code": "ibx-5153", "keys": ["m1"], "outcome": "dropped"}
    ]


def test_a_chain_of_restatements_keeps_exactly_one_row() -> None:
    """Three rows for one obligation pair three ways (a↔b, a↔c, b↔c).
    Dropping 'the other side of each pair' would close all three."""
    from cp_engine.commitments_sweep import suggested_resolutions

    d = ("Send Janet the migration runbook with the rollback plan attached "
         "before the cutover window")
    rows = [
        _row(_c("old", d, created="2026-07-01"), _TODAY),
        _row(_c("mid", d + " [confidence: low]", created="2026-07-10"), _TODAY),
        _row(_c("new", d + " (from Janet Doe (Infoblox))", created="2026-07-20"), _TODAY),
    ]
    out = suggested_resolutions({"ibx-5153": rows})
    (call,) = out["resolve_commitments"]
    assert sorted(call["keys"]) == ["mid", "new"]  # the oldest survives
    assert {r["keep"] for r in out["redundant"]} == {"old"}


def test_a_dated_row_outranks_an_undated_one() -> None:
    from cp_engine.commitments_sweep import suggested_resolutions

    d = "Send Janet the migration runbook with the rollback plan attached"
    rows = [
        _row(_c("undated", d, created="2026-07-01", source_kind="session"), _TODAY),
        _row(_c("dated", d, created="2026-07-20", due="2026-08-20"), _TODAY),
    ]
    (call,) = suggested_resolutions({"p": rows})["resolve_commitments"]
    assert call["keys"] == ["undated"]


def test_no_duplicates_means_no_payload() -> None:
    from cp_engine.commitments_sweep import suggested_resolutions

    rows = _rows(("a", "Book the Fort Worth studio", "session"),
                 ("b", "Invoice #2 for SAP 5198", "session"))
    assert suggested_resolutions({"p": rows}) == {"redundant": [], "resolve_commitments": []}


def test_render_prints_the_ready_to_call_verb() -> None:
    rows = _rows(
        ("11111111-aaaa", "Written interpretation of Brad's feedback + clarifying "
                          "questions sent to Janet for her email to Jaime/Brad", "session"),
        ("22222222-bbbb", "Email to Janet + Brad with team's interpretation of Brad's "
                          "feedback and clarifying questions", "meeting_ingest"),
    )
    text = render_sweep({"ibx-5153-ai-campaign": rows}, today=_TODAY)
    assert (
        '    resolve_commitments(project_code="ibx-5153-ai-campaign", '
        'keys=["22222222-bbbb"], outcome="dropped")'
    ) in text


# --- #366 dates written into the text --------------------------------------

def test_text_dates_reads_each_calendar_form() -> None:
    from cp_engine.commitments_sweep import text_dates

    ref = date(2026, 10, 26)
    oct30 = (date(2026, 10, 30),)
    assert text_dates("Schedule note to Janet (status + 10/30 share date)", ref) == oct30
    assert text_dates("Email Janet re: kickoff + first ideas due Oct 30", ref) == oct30
    assert text_dates("Concepts to Janet 2026-10-30", ref) == oct30
    assert text_dates("Review moved to 10-30", ref) == oct30
    assert text_dates("Share by 30 October", ref) == oct30
    assert text_dates("Ship October 30th", ref) == oct30
    assert text_dates("Ship 10/30/26", ref) == oct30
    assert text_dates("Kickoff Dec. 3rd, 2027", ref) == (date(2027, 12, 3),)
    # A yearless date takes the year nearest the row's creation day.
    assert text_dates("Jan 2 kickoff", ref) == (date(2027, 1, 2),)
    # Two dates, in text order; a repeat counts once.
    assert text_dates("Move from 10/30 to 11/02 (was Oct 30)", ref) == (
        date(2026, 10, 30), date(2026, 11, 2),
    )


def test_text_dates_ignores_numbers_that_are_not_dates() -> None:
    from cp_engine.commitments_sweep import text_dates

    ref = date(2026, 10, 26)
    for text in (
        "Share Figma 5167 board link, v10 brief doc",
        "Deck in 4-5 wks",
        "10-12 weeks of production",
        "24/7 support line",
        "Draft 120k engagement summary [confidence: medium]",
        "we may 2 things",  # lower-case "may" is the verb
        "Send Janet the recut on Friday",  # weekdays are not read
        "Invoice #2 for $40k",
    ):
        assert text_dates(text, ref) == (), text


def test_date_drift_flags_text_that_disagrees_with_due_date() -> None:
    created = "2026-10-26"
    # ibx-5153 bffaba36: date in text, no due_date.
    undated = _row(_c("bffaba36", "Email Janet re: kickoff + first ideas due Oct 30",
                      created=created), _TODAY)
    assert undated.date_drift == "undated"
    # ibx-5153 9cb989df: text and due_date agree — fine.
    agree = _row(_c("9cb989df", "Schedule note to Janet (status + 10/30 share date)",
                    due="2026-10-30", created=created), _TODAY)
    assert agree.date_drift is None
    # ... until the due date moves and the text does not.
    moved = _row(_c("9cb989df", "Schedule note to Janet (status + 10/30 share date)",
                    due="2026-11-02", created=created), _TODAY)
    assert moved.date_drift == "mismatch"
    # One of several dates matching is enough.
    both = _row(_c("x", "Review moves from 10/30 to 11/02", due="2026-11-02",
                   created=created), _TODAY)
    assert both.date_drift is None
    # No date in the text: never flagged, dated or not.
    plain = _row(_c("y", "Send Janet the recut", created=created), _TODAY)
    assert plain.date_drift is None


def test_render_sweep_shows_the_date_drift_flag() -> None:
    from cp_engine.commitments_sweep import date_drift_rows

    today = date(2026, 10, 30)
    rows = [
        _row(_c("bffaba36", "Email Janet re: kickoff + first ideas due Oct 30",
                created="2026-10-26"), today),
        _row(_c("9cb989df", "Schedule note to Janet (status + 10/30 share date)",
                due="2026-11-02", created="2026-10-26"), today),
        _row(_c("ok", "Schedule note to Janet (status + 10/30 share date)",
                due="2026-10-30", created="2026-10-26"), today),
    ]
    text = render_sweep({"ibx-5153": rows}, today=today)
    assert "⚠ DATE IN TEXT · names 2026-10-30, no due date" in text
    assert "⚠ DATE IN TEXT · names 2026-10-30, due 2026-11-02" in text
    assert text.count("⚠ DATE IN TEXT") == 2
    assert "2 with a date in the text" in text

    drift = date_drift_rows({"ibx-5153": rows})
    assert [(d["key"], d["drift"]) for d in drift] == [
        ("bffaba36", "undated"), ("9cb989df", "mismatch"),
    ]
    assert drift[1]["text_dates"] == ["2026-10-30"]
    assert drift[1]["due_date"] == "2026-11-02"


def test_render_sweep_without_drift_has_no_drift_footer() -> None:
    rows = [_row(_c("a", "Send Janet the recut"), _TODAY)]
    assert "date in the text" not in render_sweep({"p": rows}, today=_TODAY)

# --- #367: one task restated across meetings and a sprint import -----------
#
# ibx-5153's 32 open rows on 2026-10-30, verbatim (a doubled surname in two
# attributions trimmed) — the full set matters: word rarity is per project. Four rows ask for one thing — a flat/vector
# Infoblox platform graphic — from two meetings and a sprint-file import,
# and the wording pass paired only two of them. The rest are the project's
# other open work, several naming the same platform graphic, Janet or Derek:
# the realistic negatives.

_IBX_VECTOR = {
    "9284616d": ("Email Janet re: vector platform graphic; request broken-down asset",
                 "meeting_ingest"),
    "9adea648": ("Flat/vector version of the Infoblox platform graphic (Celtic-style "
                 "symbol) for concept layout work (from Hector Mendoza Suarez)",
                 "meeting_ingest"),
    "4e978e2c": ("Request flat/vector platform graphic from Marcello/Hector; share w/ Derek",
                 "meeting_ingest"),
    "2310eddf": ("Provide a flat / vector version of the Infoblox platform graphic "
                 "(the Celtic-looking symbol) for use in concept exploration",
                 "sprint_import"),
}
_IBX_OTHER = [
    "Move new platform diagram asset into shared Figma for First Person; apply melon "
    "color to foundation (from Hector Mendoza Suarez)",
    "Download Magnifique R3 clips; edit 30s 'sharks' spot",
    "Fix Euralift page: repair Vimeo video; verify/restore missing content",
    "Send homepage headshot to Drew; Drew to upload [owner unresolved: Speaker 1]",
    "Log into SalesLoft portal [owner unresolved: Speaker 1]",
    "Email Marcello Claude-based scope/schedule/guardrails doc",
    "Phase-two visual exploration deck (brand library swipe + visual approach direction) "
    "for Janet [confidence: medium]",
    "Approved script for revised platform video (gate before any production) [confidence: low]",
    "Infoblox platform video (AI messaging woven in + new platform graphic) delivered for "
    "platform page launch [confidence: medium]",
    "Email Janet re: selected claims; request better options if desired",
    "Create Figma templates for ads/landing pages using Infoblox site",
    "Draft new Platform Video script w/ AI + new platform graphic; send to Janet for approval",
    "Schedule note to Janet (status + 10/30 share date)",
    "Treated stills for Infoblox landing page (color-match test) [confidence: medium]",
    "First version of new AI-campaign creative concepts to Janet [confidence: medium]",
    "Send calendar invite to Marcello for Oct 27 8 a.m. Derek sync",
    "Email Janet re: kickoff + first ideas due Oct 30",
    "Test AI still-image treatment; deliver styled stills to Drew",
    "Delete unnecessary owner's meeting from calendar",
    "Share Figma 5167 board link, v10 brief doc, and v11 copy doc with Derek; Marcello to "
    "walk Derek through Magnifique workflow",
    "Three new AI-campaign concepts (v1), each laid out across Marcello's system-template "
    "frames (web, mobile, LinkedIn, a couple of banners), delivered to Infoblox [confidence: medium]",
    "Add platform symbol to Figma brand board for Derek",
    "Ask Janet re: updated platform graphic; update Derek",
    "Share Brief 10 + Content 11 w/ Derek in Slack",
    "Add subtitle copy placeholders (lorem) across V11 for Derek",
    "Review SAP response in CP2; draft SAP competitive SOW "
    "[off-project? → sap-5200-competitive-personality]",
    "Draft 120k engagement summary; include Canonic option; review w/ Drew "
    "[off-project? → cnc-5237-soc-2]",
    "Decide Salesloft comp video graphics approach; if minimal, assign Taylor + brief "
    "[off-project? → slt-5196-brand-campaign-26]",
]


def _ibx_rows() -> list[SweepRow]:
    rows = [_row(_c(i, d, source_kind=k), _TODAY) for i, (d, k) in _IBX_VECTOR.items()]
    rows += [_row(_c(f"other-{n:02d}", d), _TODAY) for n, d in enumerate(_IBX_OTHER)]
    return rows


def _clusters(pairs) -> list[set[str]]:
    groups: list[set[str]] = []
    for p in pairs:
        hit = [g for g in groups if p.a.id in g or p.b.id in g]
        merged = {p.a.id, p.b.id}.union(*hit) if hit else {p.a.id, p.b.id}
        groups = [g for g in groups if g not in hit] + [merged]
    return groups


def test_cross_source_restatements_of_one_task_cluster() -> None:
    """All four vector-graphic rows land in ONE cluster — across two
    meetings and a sprint import — and nothing else on the project joins
    it or pairs at all."""
    from cp_engine.commitments_sweep import likely_duplicates

    pairs = likely_duplicates(_ibx_rows())
    assert _clusters(pairs) == [set(_IBX_VECTOR)]
    kinds = {r.source_kind for p in pairs for r in (p.a, p.b)}
    assert kinds == {"meeting_ingest", "sprint_import"}


def test_the_object_pass_names_the_shared_phrase() -> None:
    from cp_engine.commitments_sweep import likely_duplicates

    by_pair = {frozenset((p.a.id, p.b.id)): p for p in likely_duplicates(_ibx_rows())}
    # The two long rows share their wording: the #311 pass, untagged.
    assert by_pair[frozenset(("9adea648", "2310eddf"))].shared_object is None
    # The short restatements pair on the object they name.
    p = by_pair[frozenset(("9284616d", "4e978e2c"))]
    assert p.shared_object == "vector platform graphic"
    p = by_pair[frozenset(("4e978e2c", "2310eddf"))]
    assert p.shared_object == "flat vector · platform graphic"


def test_punctuation_never_splits_or_joins_a_phrase() -> None:
    from cp_engine.commitments_sweep import _content_seq

    for text in ("flat / vector graphic", "flat/vector graphic", "Flat-vector graphic",
                 "flat, vector graphic"):
        assert _content_seq(text) == ["flat", "vector", "graphic"], text


def test_same_object_different_task_is_not_paired() -> None:
    """Rows that share the project's common nouns (platform, graphic,
    Janet, Derek) or an opening verb phrase ("Email Janet re:") are
    different work. Rarity is measured within the project, so the full
    ibx-5153 row set is the context."""
    from cp_engine.commitments_sweep import document_frequency, shared_object

    rows = _ibx_rows()
    df = document_frequency([r.description for r in rows])
    email_vector = _IBX_VECTOR["9284616d"][0]
    flat_vector = _IBX_VECTOR["9adea648"][0]
    for a, b in [
        (email_vector, "Email Janet re: selected claims; request better options if desired"),
        (email_vector, "Draft new Platform Video script w/ AI + new platform graphic; "
                       "send to Janet for approval"),
        (email_vector, "Ask Janet re: updated platform graphic; update Derek"),
        (flat_vector, "Add platform symbol to Figma brand board for Derek"),
        ("First version of new AI-campaign creative concepts to Janet [confidence: medium]",
         "Email Janet re: kickoff + first ideas due Oct 30"),
    ]:
        assert shared_object(a, b, df) is None, (a, b)


def test_numbered_siblings_stay_apart_in_the_object_pass() -> None:
    """The number rule guards the looser pass too: invoice tranches name
    one object and are still two obligations."""
    from cp_engine.commitments_sweep import likely_duplicates

    rows = _rows(
        ("i1", "Invoice #1 (50%) for SAP 5198 ad videos — ~$212,500", "session"),
        ("i2", "Invoice #2 (25%) for SAP 5198 ad videos — must land in 2026 tax year",
         "session"),
    )
    assert likely_duplicates(rows) == []


def test_object_pairs_are_shown_but_never_proposed_for_resolution() -> None:
    """A looser signal flags; it never lands in the ready-to-send drop
    payload, so a mis-pair cannot close a real obligation."""
    from cp_engine.commitments_sweep import suggested_resolutions

    out = suggested_resolutions({"ibx-5153": _ibx_rows()})
    dropped = {k for call in out["resolve_commitments"] for k in call["keys"]}
    # Only the wording pair's redundant row is proposed.
    assert dropped == {"2310eddf"} or dropped == {"9adea648"}
    text = render_sweep({"ibx-5153": _ibx_rows()}, today=_TODAY)
    assert "same object: vector platform graphic" in text
    assert "9284616d ↔ 4e978e2c" in text
    assert "the 8-character ids work as keys" in text
