"""Learning phase 3: lessons surface where they help, and never leak.

Drew, 2026-11-11: client lookouts (the Janet lookouts, for every client),
lessons by kind of work, small operational ones ("expenses in before the
final invoice") — shown when a project is opened, in sprint prep when the
moment is near, and in proposals/RFPs for scoping. Person and client
lessons never reach client-facing work.
"""
from __future__ import annotations

from datetime import date

from cp_engine import lessons as L


class _Q:
    def __init__(self, rows):
        self.rows = list(rows)

    def select(self, _c):
        return self

    def eq(self, c, v):
        self.rows = [r for r in self.rows if r.get(c) == v]
        return self

    def overlaps(self, c, vals):
        self.rows = [r for r in self.rows if set(r.get(c) or []) & set(vals)]
        return self

    def lte(self, c, v):
        self.rows = [r for r in self.rows if (r.get(c) or "9999") <= v]
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        self.rows = self.rows[:n]
        return self

    def execute(self):
        return type("R", (), {"data": self.rows})()


class _C:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Q(self.tables.get(name, []))


def _lesson(id, text, scope, **kw):
    return {"id": id, "text": text, "scope": scope, "status": kw.pop("status", "confirmed"),
            "company_id": kw.pop("company_id", None), "project_types": kw.pop("types", []),
            "moments": kw.pop("moments", []), "stakeholder_name": kw.pop("person", None),
            "owner_name": kw.pop("owner", "Drew Fiero"), "review_due": kw.pop("due", None),
            "source_project_id": kw.pop("src", None)}


def _client():
    return _C({
        "projects": [{"id": "p-ibx", "company_id": "ibx", "project_types": ["campaign"]}],
        "lesson_moments": [{"slug": s, "active": True} for s in
                           ("invoicing", "travel", "shoot", "client-review-round", "estimating",
                            "scope-change", "podcast-booking")],
        "project_types": [{"slug": s, "active": True} for s in ("campaign", "video")],
        "lessons": [
            _lesson("j", "Janet ends a review round only once legal has seen the work.", "person",
                    company_id="ibx", person="Janet Noe", moments=["client-review-round"]),
            _lesson("i", "Infoblox decisions slip to the final two weeks of an engagement.", "client",
                    company_id="ibx"),
            _lesson("s", "SAP legal adds a round to every deliverable they see.", "client",
                    company_id="sap", moments=["client-review-round"]),
            _lesson("c", "Lock the campaign review calendar at kickoff.", "type", types=["campaign"],
                    moments=["client-review-round"]),
            _lesson("v", "Video crews book out three weeks ahead.", "type", types=["video"],
                    moments=["shoot"]),
            _lesson("e", "Get travelling directors' expenses in before the final invoice.", "practice",
                    moments=["travel", "invoicing"]),
            _lesson("x", "Price a second round of changes into every estimate.", "practice",
                    moments=["estimating", "scope-change"]),
            _lesson("q", "A candidate nobody has reviewed yet.", "client", company_id="ibx",
                    status="candidate", src="p-ibx", owner="Marcello Grande"),
            _lesson("d", "Due for its yearly review.", "practice", due="2026-11-01"),
        ],
    })


def test_lookouts_are_the_projects_own_and_never_another_clients():
    block = L.lookouts(_client(), "p-ibx")
    ids = {g: [i["id"] for i in block[g]["items"]] for g in ("people", "client", "type", "practice")}
    assert ids["people"] == ["j"] and ids["client"] == ["i"]
    assert ids["type"] == ["c"]  # its campaign type; not video
    assert "s" not in sum(ids.values(), [])  # never SAP's
    assert block["candidates"] == 1


def test_lookouts_render_names_people_and_says_internal_once():
    text = L.render_lookouts(L.lookouts(_client(), "p-ibx"))
    assert text.startswith("_Internal: never in client-facing work._")
    assert "Janet Noe: Janet ends a review round" in text
    assert "1 candidate(s) awaiting" in text


def test_moments_in_reads_what_is_coming_up_and_new_moments_match_by_name():
    assert set(L.moments_in("Send the final invoice; book flights for the shoot")) >= {
        "invoicing", "travel", "shoot"}
    assert L.moments_in("podcast booking next week", ["podcast-booking"]) == ["podcast-booking"]


def test_watch_for_brings_the_moments_lessons_for_this_project_only():
    out = L.watch_for(_client(), "p-ibx", "Client review of round 2 Friday; invoice after.")
    ids = [w["id"] for w in out]
    assert "j" in ids and "c" in ids and "e" in ids
    assert "s" not in ids and "v" not in ids
    assert all(w["moment"] for w in out)


def test_scoping_lessons_keep_client_lookouts_apart():
    out = L.scoping_lessons(_client(), "p-ibx")
    assert [s["text"] for s in out["scoping"]] == [
        "Price a second round of changes into every estimate."]
    assert {c["text"][:5] for c in out["client_lookouts"]} == {"Janet", "Infob"}
    assert "never write them" in out["rule"]


def test_leak_check_catches_quoted_wording_not_names():
    lessons = [{"text": "Janet ends a review round only once legal has seen the work."}]
    assert L.leaks("Janet Noe approves each round of the campaign.", lessons) == []
    assert L.leaks("Note: Janet ends a review round only once legal has seen it.", lessons)
    assert L.client_facing("Engagement Summary — AI Campaign")
    assert L.client_facing("Partner RFP — production")
    assert not L.client_facing("Synthesis — interview themes")


def test_internal_leaks_reads_the_projects_client():
    hits = L.internal_leaks(_client(), "p-ibx",
                            "We know Infoblox decisions slip to the final two weeks of an engagement.")
    assert [h["id"] for h in hits] == ["i"]
    # Another client's lesson is not this project's to check (and not a leak here).
    assert L.internal_leaks(_client(), "p-ibx", "SAP legal adds a round to every deliverable") == []


def test_review_queue_by_owner():
    q = L.review_queue_by_owner(_client(), today=date(2026, 11, 11))
    assert q == [{"owner": "Drew Fiero", "candidates": 0, "due": 1},
                 {"owner": "Marcello Grande", "candidates": 1, "due": 0}]


def test_brief_adds_lookouts_after_the_summary():
    from cp_engine.brief import compose_brief

    args = ("ibx-5153", None, None, None, None, None, None)
    plain = compose_brief(*args)
    assert "## Lookouts" not in plain
    text = compose_brief(*args, lookouts=L.lookouts(_client(), "p-ibx"))
    assert "## Lookouts" in text and text.index("## Lookouts") > text.index("## Exec Summary")


def test_master_cp_lists_lessons_to_review_by_owner(tmp_path):
    from datetime import datetime, timezone

    from cp_engine.render import render_master_cp
    from tests.test_sync import make_config

    queue = [{"owner": "Drew Fiero", "candidates": 2, "due": 1},
             {"owner": "Marcello Grande", "candidates": 0, "due": 3}]
    out = render_master_cp(make_config(tmp_path), (), datetime(2026, 11, 11, tzinfo=timezone.utc),
                           lessons_to_review=queue)
    assert "**Lessons to review** (6)" in out
    assert "- Drew Fiero: 2 to confirm, 1 due for yearly review" in out
    assert "- Marcello Grande: 3 due for yearly review" in out
    assert "Lessons to review" not in render_master_cp(
        make_config(tmp_path), (), datetime(2026, 11, 11, tzinfo=timezone.utc))
