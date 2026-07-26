"""Tests for webhook/commitments_propose.py ingest hygiene (cp-engine #114).

Two behaviors on the Fathom action-item → proposed-commitment path:

1. **Owner resolution** — diarization labels ("Speaker N") resolve
   against the meeting's ``participants`` list; unresolvable labels
   store owner NULL + an ``[owner unresolved: …]`` annotation. The raw
   label is never stored as an owner.
2. **Off-project flagging** — item text screened against the #88
   roster; a foreign match gets ``[off-project? → <code>]`` so the
   review gate can reject/re-route cheaply. Detection only, no
   auto-routing.

Both annotations are display-only: ``cp_hash`` stays on the CLEAN
description (sprint-ask identity + idempotency unchanged).
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

# webhook/ is a sibling of src/; not on the import path by default.
_WEBHOOK = Path(__file__).resolve().parent.parent / "webhook"
if str(_WEBHOOK) not in sys.path:
    sys.path.insert(0, str(_WEBHOOK))

import commitments_propose as cprop

from cp_engine.ingest import _content_hash


# ─────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────

PARTICIPANTS = [
    {"name": "Drew Fiero", "email": "drew@firstperson.is"},
    {"name": "Marcello Verona", "email": "marcello@firstperson.is"},
    {"name": "Janet Lee", "email": "janet@infoblox.example"},
]


def _roster():
    """A #88-style roster: ProjectState-shaped rows (attribute access)."""
    return [
        SimpleNamespace(
            code="ibx-5192", name="Platform Sales Readiness Summit",
            company_name="Infoblox", source="engagement",
        ),
        SimpleNamespace(
            code="ibx-5153", name="AI Campaign",
            company_name="Infoblox", source="engagement",
        ),
        SimpleNamespace(
            code="ggl-5168", name="Playbooks (Activation)",
            company_name="Google", source="engagement",
        ),
        SimpleNamespace(
            code="mission-control", name="Mission Control",
            company_name=None, source="initiative",
        ),
    ]


class _FakeQuery:
    def __init__(self, data):
        self._data = data

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def single(self):
        return self

    def limit(self, *_a):
        return self

    def execute(self):
        return SimpleNamespace(data=self._data)


class _FakeClient:
    """Just enough Supabase: fathom_meetings row fetch."""

    def __init__(self, meeting_row):
        self._meeting_row = meeting_row

    def table(self, name):
        return _FakeQuery(self._meeting_row)


def _run_propose(monkeypatch, action_items, *, roster=None, participants=PARTICIPANTS):
    """Run propose_commitments against a fake meeting; capture writes."""
    meeting_row = {
        "id": "meet-1",
        "action_items": action_items,
        "participants": participants,
    }
    monkeypatch.setattr(
        cprop.mc2_db, "get_client", lambda required=False: _FakeClient(meeting_row)
    )
    monkeypatch.setattr(
        cprop, "resolve_commitment_owner",
        lambda client, code: {"id": "proj-1", "code": code, "kind": "project"},
    )
    written: list[dict] = []

    def fake_write(client, **kwargs):
        written.append(kwargs)
        return "inserted"

    monkeypatch.setattr(cprop, "write_commitment", fake_write)
    summary = cprop.propose_commitments("meet-1", ["ibx-5192"], roster=roster)
    return summary, written


# ─────────────────────────────────────────────────────────────
#  resolve_action_owner — the unit
# ─────────────────────────────────────────────────────────────


def test_real_assignee_passes_through_untouched():
    name, email, label = cprop.resolve_action_owner(
        {"name": "Drew Fiero", "email": "drew@firstperson.is"}, PARTICIPANTS
    )
    assert (name, email, label) == ("Drew Fiero", "drew@firstperson.is", None)


def test_real_name_without_email_enriches_from_attendees():
    name, email, label = cprop.resolve_action_owner(
        {"name": "Marcello"}, PARTICIPANTS
    )
    assert name == "Marcello Verona"
    assert email == "marcello@firstperson.is"
    assert label is None


def test_speaker_label_with_email_resolves_to_attendee_name():
    name, email, label = cprop.resolve_action_owner(
        {"name": "Speaker 2", "email": "marcello@firstperson.is"}, PARTICIPANTS
    )
    assert name == "Marcello Verona"
    assert email == "marcello@firstperson.is"
    assert label is None


def test_speaker_label_with_unknown_email_keeps_email_drops_label():
    name, email, label = cprop.resolve_action_owner(
        {"name": "Speaker 3", "email": "guest@elsewhere.example"}, PARTICIPANTS
    )
    assert name is None  # never "Speaker 3"
    assert email == "guest@elsewhere.example"
    assert label is None


def test_speaker_label_unresolvable_yields_null_owner_plus_label():
    name, email, label = cprop.resolve_action_owner(
        {"name": "Speaker 1"}, PARTICIPANTS
    )
    assert name is None
    assert email is None
    assert label == "Speaker 1"


def test_ambiguous_fuzzy_name_does_not_guess():
    people = [
        {"name": "Drew Fiero", "email": "drew@firstperson.is"},
        {"name": "Drew Barry", "email": "db@x.example"},
    ]
    name, email, label = cprop.resolve_action_owner({"name": "Drew"}, people)
    assert (name, email) == ("Drew", None)  # passed through, no enrichment
    assert label is None


# ─────────────────────────────────────────────────────────────
#  screen_off_project — the unit
# ─────────────────────────────────────────────────────────────


def test_explicit_code_mention_flags_that_project():
    got = cprop.screen_off_project(
        "Draft concise brief for ibx-5153 kickoff",
        ingest_codes=["ibx-5192"], roster=_roster(),
    )
    assert got == "ibx-5153"


def test_initiative_name_phrase_flags_the_initiative():
    got = cprop.screen_off_project(
        "Add Scorecard link to Mission Control dashboard",
        ingest_codes=["ibx-5192"], roster=_roster(),
    )
    assert got == "mission-control"


def test_unambiguous_company_mention_flags_its_only_project():
    got = cprop.screen_off_project(
        "Send the revised playbook to the Google team",
        ingest_codes=["ibx-5192"], roster=_roster(),
    )
    assert got == "ggl-5168"


def test_own_company_mention_is_not_foreign():
    # "Infoblox" is the tagged project's own company — no flag, even
    # though sibling ibx-5153 shares the company.
    got = cprop.screen_off_project(
        "Draft concise Infoblox brief for Rob",
        ingest_codes=["ibx-5192"], roster=_roster(),
    )
    assert got is None


def test_ambiguous_foreign_company_proposes_nothing():
    roster = _roster()
    got = cprop.screen_off_project(
        "Follow up on the Infoblox security review",
        ingest_codes=["ggl-5168"], roster=roster,
    )
    # Infoblox has TWO active projects → ambiguous → no guess.
    assert got is None


def test_plain_on_project_text_is_untouched():
    got = cprop.screen_off_project(
        "Send updated summit deck to Janet",
        ingest_codes=["ibx-5192"], roster=_roster(),
    )
    assert got is None


def test_no_roster_turns_screening_off():
    got = cprop.screen_off_project(
        "Add Scorecard link to Mission Control dashboard",
        ingest_codes=["ibx-5192"], roster=None,
    )
    assert got is None


# ─────────────────────────────────────────────────────────────
#  propose_commitments — end to end over a fixture meeting
# ─────────────────────────────────────────────────────────────


def test_mixed_action_items_end_to_end(monkeypatch):
    action_items = [
        # 1 — clean, on-project, real assignee → untouched.
        {
            "description": "Send updated summit deck to Janet",
            "assignee": {"name": "Drew Fiero", "email": "drew@firstperson.is"},
        },
        # 2 — diarization label with email → resolves to the attendee.
        {
            "description": "Follow up on the security review notes",
            "assignee": {"name": "Speaker 2", "email": "marcello@firstperson.is"},
        },
        # 3 — foreign item (initiative name) + unresolvable label.
        {
            "description": "Add Scorecard link to Mission Control dashboard",
            "assignee": {"name": "Speaker 1"},
        },
    ]
    summary, written = _run_propose(monkeypatch, action_items, roster=_roster())

    assert summary["proposed"] == 3
    assert summary["owner_unresolved"] == 1
    assert summary["off_project_flagged"] == 1
    assert len(written) == 3

    clean, resolved, foreign = written

    # 1 — untouched.
    assert clean["description"] == "Send updated summit deck to Janet"
    assert clean["owner_name"] == "Drew Fiero"
    assert clean["owner_email"] == "drew@firstperson.is"

    # 2 — Speaker 2 resolved; label never stored.
    assert resolved["owner_name"] == "Marcello Verona"
    assert resolved["owner_email"] == "marcello@firstperson.is"
    assert resolved["description"] == "Follow up on the security review notes"

    # 3 — owner NULL + both annotations on the description.
    assert foreign["owner_name"] is None
    assert foreign["owner_email"] is None
    assert "[owner unresolved: Speaker 1]" in foreign["description"]
    assert "[off-project? → mission-control]" in foreign["description"]
    assert foreign["description"].startswith(
        "Add Scorecard link to Mission Control dashboard"
    )

    # No "Speaker N" owner ever reaches the store.
    for row in written:
        assert not (row["owner_name"] or "").lower().startswith("speaker")


def test_cp_hash_computed_on_clean_description(monkeypatch):
    """Annotations are display-only: the hash matches the sprint-ask
    recipe on the CLEAN text, so re-ingest stays an idempotent no-op and
    the sprint-bullet identity holds."""
    action_items = [
        {
            "description": "Add Scorecard link to Mission Control dashboard",
            "assignee": {"name": "Speaker 1"},
        },
    ]
    _summary, written = _run_propose(monkeypatch, action_items, roster=_roster())

    (row,) = written
    assert row["description"] != "Add Scorecard link to Mission Control dashboard"
    assert row["cp_hash"] == _content_hash(
        "ibx-5192", "record-ask", "Add Scorecard link to Mission Control dashboard"
    )


def test_no_roster_degrades_to_no_flags(monkeypatch):
    action_items = [
        {
            "description": "Add Scorecard link to Mission Control dashboard",
            "assignee": {"name": "Drew Fiero", "email": "drew@firstperson.is"},
        },
    ]
    summary, written = _run_propose(monkeypatch, action_items, roster=None)
    assert summary["off_project_flagged"] == 0
    assert written[0]["description"] == (
        "Add Scorecard link to Mission Control dashboard"
    )
