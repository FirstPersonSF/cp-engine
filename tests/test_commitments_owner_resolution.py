"""#157: commitments owners arrive from four writers with no shared
convention (Zoom display names, diarization labels, bare first names,
emails-as-names), and the mc-2 owner filter keys on the raw strings —
every variant became a distinct "person" in the dropdown.
`resolve_owner_identity` canonicalizes against the entities person
roster at the `write_commitment` choke point.
"""
from __future__ import annotations

import cp_engine.commitments as commitments
from cp_engine.commitments import resolve_owner_identity


_PEOPLE = [
    {"name": "Drew Fiero", "email": "drew@firstperson.is", "archived_at": None},
    {"name": "Kelly Anderson", "email": "kelly@firstperson.is", "archived_at": None},
    {"name": "Marcello Grande", "email": "marcello@firstperson.is", "archived_at": None},
    {"name": "Geoff Ahmann", "email": None, "archived_at": None},
    {"name": "Eric Seanor", "email": "eseanor@icloud.com", "archived_at": None},
    {"name": "Eric Seanor", "email": None, "archived_at": "2026-01-01T00:00:00Z"},
]


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    def in_(self, *_a, **_k):
        return self

    def execute(self):
        class R:
            data = self._rows
        return R()


class _FakeClient:
    def __init__(self, rows=_PEOPLE):
        self.rows = rows

    def table(self, _name):
        return _FakeQuery(self.rows)


def setup_function(_fn):
    # The roster is cached per-process; each test starts cold.
    commitments._PEOPLE_CACHE = None
    commitments._PEOPLE_CACHE_AT = 0.0


def test_email_match_wins_and_canonicalizes_name():
    assert resolve_owner_identity(_FakeClient(), "drew", "Drew@FirstPerson.is") == (
        "Drew Fiero", "drew@firstperson.is",
    )


def test_email_only_row_gains_canonical_name():
    # The manual-form / clickup-migration shape: email, no name.
    assert resolve_owner_identity(_FakeClient(), None, "drew@firstperson.is") == (
        "Drew Fiero", "drew@firstperson.is",
    )


def test_zoom_pronoun_suffix_stripped_and_matched():
    name, email = resolve_owner_identity(
        _FakeClient(), "Marcello Grande (He/Him/His)", None,
    )
    assert (name, email) == ("Marcello Grande", "marcello@firstperson.is")


def test_glued_camelcase_name_matches():
    name, email = resolve_owner_identity(_FakeClient(), "GeoffAhmann", None)
    assert name == "Geoff Ahmann"
    assert email is None  # the entity has no email; nothing invented


def test_bare_first_name_unique_containment():
    assert resolve_owner_identity(_FakeClient(), "kelly", None) == (
        "Kelly Anderson", "kelly@firstperson.is",
    )


def test_ambiguous_or_unknown_name_passes_through():
    # Client-side person, not in the registry: raw values survive.
    assert resolve_owner_identity(_FakeClient(), "Janet Noe", None) == (
        "Janet Noe", None,
    )


def test_email_used_as_display_name_migrates_to_email_slot():
    name, email = resolve_owner_identity(
        _FakeClient(), "briak@xwf.google.com", None,
    )
    assert name is None
    assert email == "briak@xwf.google.com"


def test_diarization_label_is_not_a_name():
    assert resolve_owner_identity(_FakeClient(), "Speaker 1", None) == (None, None)


def test_exact_match_prefers_active_row():
    # Two "Eric Seanor" entities; the active one (sorted first) wins and
    # supplies the email.
    name, email = resolve_owner_identity(_FakeClient(), "Eric Seanor", None)
    assert (name, email) == ("Eric Seanor", "eseanor@icloud.com")


def test_roster_failure_never_blocks():
    class _Boom:
        def table(self, _name):
            raise RuntimeError("db down")

    assert resolve_owner_identity(_Boom(), "Tony Welch", None) == (
        "Tony Welch", None,
    )
