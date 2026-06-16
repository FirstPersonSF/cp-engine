"""Tests for frame + promote (Task 3.3): a directed re-distillation of a
proposed inbox card into a live SubstanceVersion bound to an estimate item.
"""

from pathlib import Path

import pytest

from cp_engine.spine_inbox import promote_card, proposed_card
from cp_engine.substance import parse_substance


class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._op = None
        self._filter = None

    def update(self, values):
        self._op = ("update", values)
        return self

    def eq(self, col, val):
        self._filter = (col, val)
        return self

    def execute(self):
        op, payload = self._op
        rows = self.store.setdefault(self.name, [])
        if op == "update":
            col, val = self._filter
            for x in rows:
                if x.get(col) == val:
                    x.update(payload)
            return type("R", (), {"data": []})()
        return type("R", (), {"data": []})()


class _FakeClient:
    def __init__(self, seed=None):
        self.store = {"spine_inbox": list(seed or [])}

    def table(self, name):
        return _FakeTable(self.store, name)


def _distiller(body):
    calls = []

    def fn(prompt, *, model, api_key=None):
        calls.append(prompt)
        return body

    fn.calls = calls
    return fn


def _card(est_item_id="d1", raw="raw first pass"):
    return proposed_card(
        project_id="u1", project_code="ibx-5153", source_ref="mtg-42",
        raw_distillation=raw, guessed_est_item_id=est_item_id,
        guessed_type="deliverable",
    )


# ---- new file (v1 live) -----------------------------------------------------


def test_promote_creates_v1_live(tmp_path: Path):
    distiller = _distiller("the directed distilled body")
    path = promote_card(
        _card(),
        framing="lock the two-track thesis",
        est_item_id="d1",
        kind="deliverable",
        project_dir=tmp_path,
        sources=["mtg-42", "carol-deck"],
        distiller=distiller,
        model="m",
        name="Messaging system",
        phase="Phase 0 Discovery",
        today="2026-06-15",
    )
    assert path.exists()
    item = parse_substance(path)
    assert item.est_item_id == "d1"
    assert item.est_item_kind == "deliverable"
    assert item.phase == "Phase 0 Discovery"
    live = item.live_version()
    assert live.label == "v1"
    assert live.status == "live"
    assert live.date == "2026-06-15"
    assert live.framing == "lock the two-track thesis"
    assert live.sources == ("mtg-42", "carol-deck")
    assert live.body == "the directed distilled body"


def test_promote_passes_framing_and_raw_to_distiller(tmp_path: Path):
    distiller = _distiller("body")
    promote_card(
        _card(raw="the raw material here"),
        framing="my directing brief",
        est_item_id="d1", kind="deliverable",
        project_dir=tmp_path, sources=[], distiller=distiller, model="m",
        name="Messaging system", phase="P0", today="2026-06-15",
    )
    prompt = distiller.calls[0]
    assert "my directing brief" in prompt
    assert "the raw material here" in prompt


# ---- existing file (next version, demote prior live) ------------------------


def test_promote_adds_version_and_demotes_prior_live(tmp_path: Path):
    # Seed an existing v1-live substance file for the same item.
    promote_card(
        _card(),
        framing="first pass", est_item_id="d1", kind="deliverable",
        project_dir=tmp_path, sources=["a"], distiller=_distiller("body one"),
        model="m", name="Messaging system", phase="P0", today="2026-04-23",
    )
    # Promote a second card → v2 live, v1 demoted.
    path = promote_card(
        _card(), framing="second pass", est_item_id="d1", kind="deliverable",
        project_dir=tmp_path, sources=["b"], distiller=_distiller("body two"),
        model="m", name="Messaging system", phase="P0", today="2026-06-15",
    )
    item = parse_substance(path)
    assert len(item.versions) == 2
    live = item.live_version()
    assert live.label == "v2"
    assert live.body == "body two"
    superseded = [v for v in item.versions if v.status == "superseded"]
    assert len(superseded) == 1
    assert superseded[0].label == "v1"


# ---- card status flips to promoted ------------------------------------------


def test_promote_sets_card_status_promoted(tmp_path: Path):
    card = _card()
    client = _FakeClient(seed=[{"id": card.id, "status": "framed"}])
    promote_card(
        card, framing="brief", est_item_id="d1", kind="deliverable",
        project_dir=tmp_path, sources=[], distiller=_distiller("b"), model="m",
        client=client, name="MS", phase="P0", today="2026-06-15",
    )
    row = next(r for r in client.store["spine_inbox"] if r["id"] == card.id)
    assert row["status"] == "promoted"


# ---- one substance file per est_item_id: route to existing, never split -----


def test_promote_appends_to_existing_file_under_different_slug(tmp_path: Path):
    # First item d1 lands a file at the "Alpha" slug.
    first = promote_card(
        _card(est_item_id="d1"), framing="f1", est_item_id="d1",
        kind="deliverable", project_dir=tmp_path, sources=[],
        distiller=_distiller("b1"), model="m",
        name="Alpha", phase="P0", today="2026-04-23",
    )
    # A DIFFERENT name/slug but SAME est_item_id must APPEND a version to the
    # existing file — promoting into the existing item is exactly the intent.
    second = promote_card(
        _card(est_item_id="d1"), framing="f2", est_item_id="d1",
        kind="deliverable", project_dir=tmp_path, sources=[],
        distiller=_distiller("b2"), model="m",
        name="Beta different name", phase="P0", today="2026-06-15",
    )
    # Same file, no second file created.
    assert second.resolve() == first.resolve()
    spine_files = list(tmp_path.glob("spine/*/*.md"))
    assert len(spine_files) == 1
    item = parse_substance(second)
    assert len(item.versions) == 2
    live = item.live_version()
    assert live.label == "v2"
    assert live.body == "b2"
    superseded = [v for v in item.versions if v.status == "superseded"]
    assert len(superseded) == 1 and superseded[0].label == "v1"


def test_promote_preserves_existing_binding_metadata(tmp_path: Path):
    # Existing file declares its own phase; a promote with different phase args
    # must not overwrite the file's authoritative binding metadata.
    first = promote_card(
        _card(est_item_id="d1"), framing="f1", est_item_id="d1",
        kind="deliverable", project_dir=tmp_path, sources=[],
        distiller=_distiller("b1"), model="m",
        name="Alpha", phase="Phase 0 Discovery", today="2026-04-23",
    )
    promote_card(
        _card(est_item_id="d1"), framing="f2", est_item_id="d1",
        kind="output", project_dir=tmp_path, sources=[],
        distiller=_distiller("b2"), model="m",
        name="Beta", phase="Phase 9 Wrong", today="2026-06-15",
    )
    item = parse_substance(first)
    assert item.est_item_id == "d1"
    assert item.est_item_kind == "deliverable"
    assert item.phase == "Phase 0 Discovery"


def test_promote_raises_on_two_existing_files_binding_same_id(tmp_path: Path):
    # Manufacture a genuinely-corrupt state: TWO existing substance files both
    # binding the same est_item_id. promote must refuse (invariant violation).
    from cp_engine.substance import (
        SubstanceVersion,
        WorkItemSubstance,
        render_substance,
    )

    p0 = tmp_path / "spine" / "p0"
    p0.mkdir(parents=True, exist_ok=True)
    for slug in ("alpha", "beta"):
        path = p0 / f"{slug}.md"
        item = WorkItemSubstance(
            est_item_id="d1", est_item_kind="deliverable", phase="P0",
            binding="live",
            versions=(SubstanceVersion(
                label="v1", date="2026-06-15", status="live",
                framing="f", sources=(), body="b",
            ),),
            path=path,
        )
        path.write_text(render_substance(item))

    with pytest.raises(ValueError, match="d1"):
        promote_card(
            _card(est_item_id="d1"), framing="f2", est_item_id="d1",
            kind="deliverable", project_dir=tmp_path, sources=[],
            distiller=_distiller("b2"), model="m",
            name="Gamma", phase="P0", today="2026-06-15",
        )
