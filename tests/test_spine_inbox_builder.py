"""Tests for the proposed-card builder (Task 3.2).

A fake supabase client + injected fake distiller keep these off the network.
"""

import json
from pathlib import Path

import pytest

from cp_engine.estimate import Estimate, EstimateItem, EstimatePhase
from cp_engine.spine_inbox import (
    _parse_distiller_json,
    build_inbox_card_from_transcript,
)


# ---- fakes -----------------------------------------------------------------


class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._op = None
        self._filters = []

    def upsert(self, rows, on_conflict=None):
        self._op = ("upsert", rows)
        return self

    def select(self, cols):
        assert "*" not in cols, "never select('*')"
        self._op = ("select", cols)
        return self

    def update(self, patch):
        self._op = ("update", dict(patch))
        return self

    def eq(self, col, val):
        self._filters.append(lambda r, c=col, v=val: r.get(c) == v)
        return self

    def neq(self, col, val):
        self._filters.append(lambda r, c=col, v=val: r.get(c) != v)
        return self

    def in_(self, col, vals):
        self._filters.append(lambda r, c=col, v=tuple(vals): r.get(c) in v)
        return self

    def _matching(self, rows):
        return [r for r in rows if all(f(r) for f in self._filters)]

    def execute(self):
        op, payload = self._op
        rows = self.store.setdefault(self.name, [])
        if op == "upsert":
            for r in payload:
                match = next((x for x in rows if x["id"] == r["id"]), None)
                if match is not None:
                    match.update(r)
                else:
                    rows.append(dict(r))
            return type("R", (), {"data": payload})()
        if op == "update":
            hit = self._matching(rows)
            for r in hit:
                r.update(payload)
            return type("R", (), {"data": hit})()
        return type("R", (), {"data": self._matching(rows)})()


class _FakeClient:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return _FakeTable(self.store, name)


def _estimate(items):
    phase = EstimatePhase(id="ph0", name="Phase 0", overview=None, position=0,
                          items=tuple(items))
    return Estimate(id="est1", mc_project_id="u1", name="E1", phases=(phase,))


def _item(item_id, name, kind="deliverable"):
    return EstimateItem(id=item_id, phase_id="ph0", kind=kind, name=name,
                        short_description=None, position=0, library_item_id=None)


def _distiller(payload):
    """Return a distiller fn that yields a fixed JSON blob, recording prompts."""
    calls = []

    def fn(prompt, *, model, api_key=None):
        calls.append(prompt)
        return json.dumps(payload)

    fn.calls = calls
    return fn


def _raw_distiller(raw):
    """Return a distiller fn that yields a fixed raw string, recording prompts."""
    calls = []

    def fn(prompt, *, model, api_key=None):
        calls.append(prompt)
        return raw

    fn.calls = calls
    return fn


# ---- tests -----------------------------------------------------------------


def test_builds_proposed_card_with_raw_distillation_and_match():
    est = _estimate([_item("d1", "Messaging system"),
                     _item("d2", "Stakeholder map")])
    distiller = _distiller(
        {"distillation": "We aligned the messaging system.",
         "matched_item_name": "Messaging system"}
    )
    client = _FakeClient()
    card = build_inbox_card_from_transcript(
        client,
        project_id="u1",
        project_code="ibx-5153",
        source_ref="mtg-42",
        transcript="... transcript ...",
        estimate=est,
        distiller=distiller,
        model="m",
    )
    assert card.id == "ibx-5153/inbox/mtg-42"
    assert card.status == "proposed"
    assert card.raw_distillation == "We aligned the messaging system."
    assert card.guessed_est_item_id == "d1"
    assert card.guessed_type == "deliverable"
    # exactly one LLM call (cost discipline)
    assert len(distiller.calls) == 1


def test_writes_only_to_spine_inbox_no_substance():
    est = _estimate([_item("d1", "Messaging system")])
    distiller = _distiller(
        {"distillation": "raw", "matched_item_name": "Messaging system"}
    )
    client = _FakeClient()
    build_inbox_card_from_transcript(
        client, project_id="u1", project_code="p", source_ref="s",
        transcript="t", estimate=est, distiller=distiller, model="m",
    )
    assert "spine_inbox" in client.store
    assert len(client.store["spine_inbox"]) == 1
    assert "spine_substance" not in client.store


def test_no_match_falls_back_to_source_type():
    est = _estimate([_item("d1", "Messaging system")])
    distiller = _distiller(
        {"distillation": "raw", "matched_item_name": None}
    )
    client = _FakeClient()
    card = build_inbox_card_from_transcript(
        client, project_id="u1", project_code="p", source_ref="s",
        transcript="t", estimate=est, distiller=distiller, model="m",
    )
    assert card.guessed_est_item_id is None
    assert card.guessed_type == "source"


def test_no_estimate_yields_source_guess():
    distiller = _distiller({"distillation": "raw", "matched_item_name": "X"})
    client = _FakeClient()
    card = build_inbox_card_from_transcript(
        client, project_id="u1", project_code="p", source_ref="s",
        transcript="t", estimate=None, distiller=distiller, model="m",
    )
    assert card.guessed_est_item_id is None
    assert card.guessed_type == "source"


def test_fuzzy_name_match_is_case_insensitive():
    est = _estimate([_item("d1", "Messaging System", kind="activity")])
    distiller = _distiller(
        {"distillation": "raw", "matched_item_name": "messaging system"}
    )
    client = _FakeClient()
    card = build_inbox_card_from_transcript(
        client, project_id="u1", project_code="p", source_ref="s",
        transcript="t", estimate=est, distiller=distiller, model="m",
    )
    assert card.guessed_est_item_id == "d1"
    assert card.guessed_type == "activity"


def test_row_persisted_with_raw_distillation():
    est = _estimate([_item("d1", "Messaging system")])
    distiller = _distiller(
        {"distillation": "the raw body", "matched_item_name": "Messaging system"}
    )
    client = _FakeClient()
    build_inbox_card_from_transcript(
        client, project_id="u1", project_code="p", source_ref="s",
        transcript="t", estimate=est, distiller=distiller, model="m",
    )
    row = client.store["spine_inbox"][0]
    assert row["raw_distillation"] == "the raw body"
    assert row["guessed_est_item_id"] == "d1"
    assert row["status"] == "proposed"


# ---- malformed-LLM-output coverage for _parse_distiller_json ---------------
#
# The recovery + guard branches in _parse_distiller_json (fence stripping +
# non-dict isinstance guard) were untested — every prior test fed clean
# json.dumps. These exercise the actual contract end-to-end.


def test_parse_distiller_json_strips_json_fence():
    """(a) A ```json-fenced block parses correctly (fence-recovery branch)."""
    fenced = (
        '```json\n'
        '{"distillation": "fenced body", "matched_item_name": "X"}\n'
        '```'
    )
    obj = _parse_distiller_json(fenced)
    assert obj == {"distillation": "fenced body", "matched_item_name": "X"}


def test_build_card_recovers_from_fenced_distiller_output():
    """(a) build_inbox_card_from_transcript handles a fenced distiller blob.

    Exercises the fence-recovery branch through the public builder, proving the
    card is written from the recovered JSON rather than failing.
    """
    est = _estimate([_item("d1", "Messaging system")])
    distiller = _raw_distiller(
        '```json\n'
        '{"distillation": "recovered body", "matched_item_name": "Messaging system"}\n'
        '```'
    )
    client = _FakeClient()
    card = build_inbox_card_from_transcript(
        client, project_id="u1", project_code="p", source_ref="s",
        transcript="t", estimate=est, distiller=distiller, model="m",
    )
    assert card.raw_distillation == "recovered body"
    assert card.guessed_est_item_id == "d1"


def test_parse_distiller_json_raises_on_prose():
    """(b) Non-JSON prose raises rather than silently returning garbage."""
    with pytest.raises(ValueError):
        _parse_distiller_json("I'm sorry, I can't help with that.")


def test_build_card_does_not_write_malformed_card_on_prose():
    """(b) A prose (non-JSON) distiller makes the builder raise — and writes
    NOTHING to spine_inbox (no malformed card persisted)."""
    est = _estimate([_item("d1", "Messaging system")])
    distiller = _raw_distiller("Here is a summary of the meeting in plain prose.")
    client = _FakeClient()
    with pytest.raises(ValueError):
        build_inbox_card_from_transcript(
            client, project_id="u1", project_code="p", source_ref="s",
            transcript="t", estimate=est, distiller=distiller, model="m",
        )
    # The upsert is the LAST step; a parse failure happens before it, so no
    # malformed card is written.
    assert "spine_inbox" not in client.store


def test_parse_distiller_json_raises_on_json_array():
    """(c) A JSON array (not an object) trips the isinstance(obj, dict) guard."""
    with pytest.raises(ValueError):
        _parse_distiller_json('[{"distillation": "x"}]')


def test_build_card_rejects_json_array_distiller_output():
    """(c) A JSON-array distiller blob is rejected by the dict guard, surfaced
    through the public builder, and writes no card."""
    est = _estimate([_item("d1", "Messaging system")])
    distiller = _raw_distiller('[{"distillation": "x", "matched_item_name": "Y"}]')
    client = _FakeClient()
    with pytest.raises(ValueError):
        build_inbox_card_from_transcript(
            client, project_id="u1", project_code="p", source_ref="s",
            transcript="t", estimate=est, distiller=distiller, model="m",
        )
    assert "spine_inbox" not in client.store


def test_reingest_retires_stale_cards_in_other_projects():
    """The re-route path: a meeting re-homed to a new project auto-dismisses
    the actionable cards it left in its OLD project's Frame & promote inbox.
    Promoted cards (human-confirmed substance) are never touched."""
    client = _FakeClient()
    client.store["spine_inbox"] = [
        {"id": "old-code/inbox/mtg-42", "project_id": "OLD", "source_ref": "mtg-42",
         "status": "proposed"},
        {"id": "old-code/inbox/mtg-42-framed", "project_id": "OLD2", "source_ref": "mtg-42",
         "status": "framed"},
        {"id": "old-code/inbox/mtg-42-promoted", "project_id": "OLD", "source_ref": "mtg-42",
         "status": "promoted"},
        {"id": "old-code/inbox/mtg-7", "project_id": "OLD", "source_ref": "mtg-7",
         "status": "proposed"},
    ]
    distiller = _distiller({"distillation": "moved", "matched_item_name": None})
    build_inbox_card_from_transcript(
        client, project_id="NEW", project_code="ibx-5153", source_ref="mtg-42",
        transcript="t", estimate=None, distiller=distiller, model="m",
    )
    by_id = {r["id"]: r for r in client.store["spine_inbox"]}
    assert by_id["old-code/inbox/mtg-42"]["status"] == "dismissed"
    assert by_id["old-code/inbox/mtg-42-framed"]["status"] == "dismissed"
    assert by_id["old-code/inbox/mtg-42-promoted"]["status"] == "promoted"  # untouched
    assert by_id["old-code/inbox/mtg-7"]["status"] == "proposed"            # other meeting
    # And the new project's card exists, proposed.
    new_cards = [r for r in client.store["spine_inbox"]
                 if r["project_id"] == "NEW" and r["source_ref"] == "mtg-42"]
    assert len(new_cards) == 1 and new_cards[0]["status"] == "proposed"
