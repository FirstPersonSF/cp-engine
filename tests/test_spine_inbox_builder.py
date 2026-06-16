"""Tests for the proposed-card builder (Task 3.2).

A fake supabase client + injected fake distiller keep these off the network.
"""

import json
from pathlib import Path

from cp_engine.estimate import Estimate, EstimateItem, EstimatePhase
from cp_engine.spine_inbox import build_inbox_card_from_transcript


# ---- fakes -----------------------------------------------------------------


class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._op = None

    def upsert(self, rows, on_conflict=None):
        self._op = ("upsert", rows)
        return self

    def select(self, cols):
        assert "*" not in cols, "never select('*')"
        self._op = ("select", cols)
        return self

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
        return type("R", (), {"data": list(rows)})()


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
