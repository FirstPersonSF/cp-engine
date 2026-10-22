"""Cross-engagement line for a quiet job (cp-engine #355 item 4).

The case it exists for: ibx-5153 had no meeting for 19+ days in August while
the same client people (Janet, Kimber) met on its sibling ibx-5192 — the
bundle said nothing, so the job read as stalled.
"""

from __future__ import annotations

import dataclasses
from datetime import date

from cp_engine.prep_planning import build_planning_result, render_planning_bundle
from cp_engine.sibling_activity import sibling_activity_line
from tests.test_prep_planning import make_config, make_state

TODAY = date(2026, 8, 17)
TEAM = ("drew", "tony", "marcello")


def _p(name, email=None, external=None):
    out = {"name": name}
    if email:
        out["email"] = email
        out["is_external"] = external
    return out


DREW = _p("Drew Fiero", "drew@firstperson.is", False)
JANET = _p("Janet Noe", "jnoe@infoblox.com", True)
KIMBER = _p("Kimber Myers")  # transcript-sourced: no email
MARCELLO = _p("Marcello")  # team member, transcript-sourced
STRANGER = _p("Pat Other")

MEETINGS = [
    # the quiet job's last meeting — Janet + Kimber are its people
    {"project_id": "id-5153", "meeting_date": "2026-07-29T17:00:00Z",
     "participants": [DREW, JANET, KIMBER, MARCELLO]},
    # sibling, BEFORE the gap starts — must not count
    {"project_id": "id-5192", "meeting_date": "2026-07-20T17:00:00Z",
     "participants": [DREW, JANET]},
    # sibling, inside the gap, shared client people
    {"project_id": "id-5192", "meeting_date": "2026-08-04T17:00:00Z",
     "participants": [DREW, JANET, MARCELLO]},
    {"project_id": "id-5192", "meeting_date": "2026-08-11T17:00:00Z",
     "participants": [_p("Kimber Myers <kmyers@infoblox.com>"), DREW]},
    # sibling, inside the gap, only TEAM overlap — must not count
    {"project_id": "id-5200", "meeting_date": "2026-08-06T17:00:00Z",
     "participants": [DREW, MARCELLO, STRANGER]},
    # ignored meeting — must not count
    {"project_id": "id-5192", "meeting_date": "2026-08-12T17:00:00Z",
     "participants": [JANET], "ignored": True},
]
SIBLINGS = {"id-5192": "ibx-5192-srs", "id-5200": "ibx-5200-other"}


def test_quiet_job_names_the_sibling_and_shared_client_people():
    line = sibling_activity_line(
        "id-5153", SIBLINGS, MEETINGS, today=TODAY, team=TEAM
    )
    assert line == (
        "quiet 19d — Janet Noe, Kimber Myers active on ibx-5192-srs (2 meetings)"
    )


def test_not_quiet_no_line():
    assert sibling_activity_line(
        "id-5153", SIBLINGS, MEETINGS, today=date(2026, 8, 5), team=TEAM
    ) is None


def test_team_only_overlap_is_not_an_explanation():
    only_team = [MEETINGS[0], MEETINGS[4]]
    assert sibling_activity_line(
        "id-5153", SIBLINGS, only_team, today=TODAY, team=TEAM
    ) is None


class _Builder:
    """Chainable stand-in for a PostgREST query: every method returns self;
    `execute()` yields the meeting rows for fathom_meetings, nothing else."""

    def __init__(self, table, log):
        self._table = table
        self._log = log

    def __getattr__(self, name):
        def _chain(*args, **kwargs):
            if name == "select":
                self._log.append((self._table, args[0] if args else None))
            return self
        return _chain

    def execute(self):
        rows = MEETINGS if self._table == "fathom_meetings" else []
        return type("R", (), {"data": rows})()


class _Client:
    def __init__(self):
        self.selects: list = []

    def table(self, name):
        return _Builder(name, self.selects)

    def rpc(self, *a, **k):
        return _Builder("rpc", self.selects)


def test_bundle_carries_the_cross_engagement_line(tmp_path):
    """CONTROL: fails on main — the bundle has no cross-engagement line."""
    config = dataclasses.replace(make_config(tmp_path), team=TEAM)
    quiet = dataclasses.replace(
        make_state("ibx-5153-ai-campaign", company_code="IBX",
                   company_name="Infoblox"),
        mc2_id="id-5153",
    )
    sibling = dataclasses.replace(
        make_state("ibx-5192-srs", company_code="IBX", company_name="Infoblox",
                   status="Holding"),
        mc2_id="id-5192",
    )
    client = _Client()
    result = build_planning_result(
        config, (quiet, sibling), today=TODAY, supabase_client=client
    )
    bundle = render_planning_bundle(result)
    assert (
        "**Cross-engagement:** quiet 19d — Janet Noe, Kimber Myers active on "
        "ibx-5192-srs (2 meetings)"
    ) in bundle
    # explicit columns, never `*`
    cols = [c for t, c in client.selects if t == "fathom_meetings"]
    assert cols and all(c and "*" not in c for c in cols)


def test_last_comma_first_matches_first_last():
    rows = [
        {"project_id": "a", "meeting_date": "2026-07-01",
         "participants": [_p("Olivia Lubel")]},
        {"project_id": "b", "meeting_date": "2026-07-10",
         "participants": [_p("Lubel, Olivia", "ol@sap.com", True)]},
    ]
    assert sibling_activity_line("a", {"b": "sap-2027"}, rows,
                                 today=date(2026, 7, 20)) == (
        "quiet 19d — Olivia Lubel active on sap-2027 (1 meeting)"
    )
