"""Tests for frame + promote (Task 3.3): a directed re-distillation of a
proposed inbox card into a live SubstanceVersion bound to an estimate item.
"""

from pathlib import Path

import pytest

from cp_engine.spine_inbox import (
    _iter_substance_files,
    promote_card,
    proposed_card,
)
from cp_engine.substance import (
    SubstanceVersion,
    WorkItemSubstance,
    parse_substance,
    render_substance,
)


def _write_item(spine_root: Path, subdir: str, slug: str, *, est_item_id="d1"):
    d = spine_root / subdir
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{slug}.md"
    item = WorkItemSubstance(
        est_item_id=est_item_id, est_item_kind="deliverable", phase="P0",
        binding="live",
        versions=(SubstanceVersion(
            label="v1", date="2026-06-15", status="live",
            framing="f", sources=(), body="b",
        ),),
        path=path,
    )
    path.write_text(render_substance(item))
    return path


def test_iter_substance_files_skips_authored_mirror(tmp_path: Path):
    """A generated authored mirror file (spine/_authored/<slug>.md) is DB-owned
    and must NEVER be yielded as a real disk substance candidate — otherwise the
    promote path could route into / count it. Real phase-dir files still yield."""
    spine_root = tmp_path / "spine"
    real = _write_item(spine_root, "phase-0", "messaging-system")
    _write_item(spine_root, "_authored", "note-1")        # DB-owned mirror
    _write_item(spine_root, "_context", "carol")          # project context
    snap = spine_root / "phase-0" / "messaging-system.snapshots"
    snap.mkdir(parents=True, exist_ok=True)
    _write_item(snap.parent, "messaging-system.snapshots", "frozen")

    yielded = list(_iter_substance_files(spine_root))
    assert yielded == [real]


class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._op = None
        self._filters = []
        self._limit = None

    def update(self, values):
        self._op = ("update", values)
        return self

    def select(self, cols):
        self._op = ("select", cols)
        return self

    def upsert(self, rows, on_conflict=None):
        self._op = ("upsert", list(rows))
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        op, payload = self._op
        rows = self.store.setdefault(self.name, [])
        if op == "update":
            for x in rows:
                if all(x.get(c) == v for c, v in self._filters):
                    x.update(payload)
            return type("R", (), {"data": []})()
        if op == "select":
            hits = [x for x in rows
                    if all(x.get(c) == v for c, v in self._filters)]
            if self._limit is not None:
                hits = hits[: self._limit]
            return type("R", (), {"data": hits})()
        if op == "upsert":
            by_id = {x.get("id"): i for i, x in enumerate(rows)}
            for new in payload:
                i = by_id.get(new.get("id"))
                if i is None:
                    rows.append(dict(new))
                else:
                    rows[i].update(new)
            return type("R", (), {"data": []})()
        return type("R", (), {"data": []})()


class _FakeClient:
    def __init__(self, seed=None, substance=None):
        self.store = {
            "spine_inbox": list(seed or []),
            "spine_substance": list(substance or []),
        }

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
    # Promote a second card with the SAME sources (a true re-distill of the
    # same artifact) → v2 live, v1 demoted. (Divergent sources would take the
    # create-don't-version path instead — see the issue-#44 tests below.)
    path = promote_card(
        _card(), framing="second pass", est_item_id="d1", kind="deliverable",
        project_dir=tmp_path, sources=["a"], distiller=_distiller("body two"),
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


# ---- issue #44: create-don't-version on source divergence --------------------


def _seed_bound_card(tmp_path: Path, *, sources=("mtg-1",), today="2026-06-24"):
    """First promote: bind a v1-live substance file to work item d1."""
    return promote_card(
        _card(), framing="Planning the interview blocks",
        est_item_id="d1", kind="activity", project_dir=tmp_path,
        sources=list(sources), distiller=_distiller("planning body"),
        model="m", name="1:1 stakeholder interviews",
        phase="discovery-alignment", today=today,
    )


def test_divergent_sources_creates_new_serving_element(tmp_path: Path):
    """A promote whose sources differ from the bound card's live sources is a
    DIFFERENT artifact serving the same work item: it must land as a NEW
    authored element (serves=[d1]) and leave the bound card untouched."""
    first = _seed_bound_card(tmp_path, sources=("mtg-1",))
    client = _FakeClient(seed=[{"id": _card().id, "status": "framed"}])

    path = promote_card(
        _card(), framing="Interview with Paul Wu",
        est_item_id="d1", kind="activity", project_dir=tmp_path,
        sources=["mtg-2"], distiller=_distiller("paul wu body"),
        model="m", client=client, name="1:1 stakeholder interviews",
        phase="discovery-alignment", today="2026-07-09",
    )

    # New element mirrors under spine/_authored/ with the authored identity.
    assert path == tmp_path / "spine" / "_authored" / "interview-with-paul-wu.md"
    item = parse_substance(path)
    assert item.est_item_id == "_authored/interview-with-paul-wu"
    assert item.serves == ("d1",)
    assert item.binding == "live"          # serves non-empty → live
    assert item.placement == "context"     # authored elements are context
    assert item.layer == "Activity"        # canon_layer(kind)
    live = item.live_version()
    assert live.label == "v1" and live.status == "live"
    assert live.framing == "Interview with Paul Wu"
    assert live.sources == ("mtg-2",)
    assert live.body == "paul wu body"

    # The bound card is UNTOUCHED — still its own v1 live, planning body.
    original = parse_substance(first)
    assert original.est_item_id == "d1"
    assert len(original.versions) == 1
    assert original.live_version().body == "planning body"
    assert original.live_version().sources == ("mtg-1",)

    # DB rows: v1-live authored rows matching the shared create path's shape.
    rows = client.store["spine_substance"]
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "ibx-5153/_authored/interview-with-paul-wu/v1"
    assert row["est_item_id"] == "_authored/interview-with-paul-wu"
    assert row["origin"] == "authored"
    assert row["serves"] == ["d1"]
    assert row["binding"] == "live"
    assert row["placement"] == "context"
    assert row["layer"] == "Activity"
    assert row["status"] == "live"
    assert row["version_label"] == "v1"
    assert row["sources"] == ["mtg-2"]

    # The card still flipped to promoted (flip_card defaults True).
    inbox = next(r for r in client.store["spine_inbox"]
                 if r["id"] == _card().id)
    assert inbox["status"] == "promoted"


def test_divergent_sources_flip_card_false_defers_flip(tmp_path: Path):
    """The webhook's deferred-flip contract holds on the create path too."""
    _seed_bound_card(tmp_path)
    client = _FakeClient(seed=[{"id": _card().id, "status": "framed"}])
    promote_card(
        _card(), framing="Interview with Paul Wu",
        est_item_id="d1", kind="activity", project_dir=tmp_path,
        sources=["mtg-2"], distiller=_distiller("b"), model="m",
        client=client, flip_card=False,
        name="1:1 stakeholder interviews", phase="discovery-alignment",
        today="2026-07-09",
    )
    inbox = next(r for r in client.store["spine_inbox"]
                 if r["id"] == _card().id)
    assert inbox["status"] == "framed"          # NOT flipped
    assert len(client.store["spine_substance"]) == 1   # rows still written


def test_divergent_sources_slug_collision_suffixes(tmp_path: Path):
    """An existing `_authored/<slug>` (file or DB rows) forces `-2`, `-3`, …."""
    _seed_bound_card(tmp_path)
    # Occupy the base slug in the DB, and the -2 slug on disk.
    taken_rows = [{
        "id": "ibx-5153/_authored/interview-with-paul-wu/v1",
        "project_id": "u1",
        "est_item_id": "_authored/interview-with-paul-wu",
    }]
    authored_dir = tmp_path / "spine" / "_authored"
    authored_dir.mkdir(parents=True, exist_ok=True)
    (authored_dir / "interview-with-paul-wu-2.md").write_text("occupied")
    client = _FakeClient(seed=[{"id": _card().id, "status": "framed"}],
                         substance=taken_rows)

    path = promote_card(
        _card(), framing="Interview with Paul Wu",
        est_item_id="d1", kind="activity", project_dir=tmp_path,
        sources=["mtg-9"], distiller=_distiller("third paul wu"), model="m",
        client=client, name="1:1 stakeholder interviews",
        phase="discovery-alignment", today="2026-07-09",
    )
    assert path.name == "interview-with-paul-wu-3.md"
    item = parse_substance(path)
    assert item.est_item_id == "_authored/interview-with-paul-wu-3"
    assert item.serves == ("d1",)
    # The pre-existing rows/files are untouched.
    assert (authored_dir / "interview-with-paul-wu-2.md").read_text() == "occupied"
    assert any(r["est_item_id"] == "_authored/interview-with-paul-wu"
               for r in client.store["spine_substance"])


def test_empty_incoming_sources_still_versions(tmp_path: Path):
    """No incoming sources → nothing to compare → version as before."""
    _seed_bound_card(tmp_path, sources=("mtg-1",))
    path = promote_card(
        _card(), framing="re-frame", est_item_id="d1", kind="activity",
        project_dir=tmp_path, sources=[], distiller=_distiller("v2 body"),
        model="m", name="1:1 stakeholder interviews",
        phase="discovery-alignment", today="2026-07-09",
    )
    item = parse_substance(path)
    assert item.live_version().label == "v2"
    assert len(item.versions) == 2


def test_empty_prior_sources_still_versions(tmp_path: Path):
    """Live version has no sources → nothing to compare → version as before."""
    _seed_bound_card(tmp_path, sources=())
    path = promote_card(
        _card(), framing="re-frame", est_item_id="d1", kind="activity",
        project_dir=tmp_path, sources=["mtg-2"], distiller=_distiller("v2 body"),
        model="m", name="1:1 stakeholder interviews",
        phase="discovery-alignment", today="2026-07-09",
    )
    item = parse_substance(path)
    assert item.live_version().label == "v2"
    assert len(item.versions) == 2


def test_same_sources_repromote_versions(tmp_path: Path):
    """Same source set (a true re-distill, e.g. the observed v5→v6) versions."""
    _seed_bound_card(tmp_path, sources=("mtg-1", "deck-a"))
    path = promote_card(
        _card(), framing="tighter pass", est_item_id="d1", kind="activity",
        project_dir=tmp_path, sources=["deck-a", "mtg-1"],  # order-insensitive
        distiller=_distiller("v2 body"), model="m",
        name="1:1 stakeholder interviews", phase="discovery-alignment",
        today="2026-07-09",
    )
    item = parse_substance(path)
    assert item.live_version().label == "v2"
    assert item.live_version().body == "v2 body"


def test_divergent_sources_without_client_raises(tmp_path: Path):
    """The create path needs MC-2 (authored rows are DB-owned) — a divergent
    promote without a client must fail LOUD, never silently strand or version."""
    first = _seed_bound_card(tmp_path, sources=("mtg-1",))
    with pytest.raises(ValueError, match="authored"):
        promote_card(
            _card(), framing="Interview with Paul Wu",
            est_item_id="d1", kind="activity", project_dir=tmp_path,
            sources=["mtg-2"], distiller=_distiller("b"), model="m",
            client=None, name="1:1 stakeholder interviews",
            phase="discovery-alignment", today="2026-07-09",
        )
    # And the bound card was not touched.
    assert parse_substance(first).live_version().body == "planning body"


def test_created_element_is_invisible_to_promote_targeting(tmp_path: Path):
    """The new element lives under spine/_authored/ (skipped by
    _iter_substance_files), so a LATER promote to the same work item still
    targets the original bound file — never the authored mirror."""
    first = _seed_bound_card(tmp_path, sources=("mtg-1",))
    client = _FakeClient(seed=[{"id": _card().id, "status": "framed"}])
    promote_card(
        _card(), framing="Interview with Paul Wu",
        est_item_id="d1", kind="activity", project_dir=tmp_path,
        sources=["mtg-2"], distiller=_distiller("b"), model="m",
        client=client, name="1:1 stakeholder interviews",
        phase="discovery-alignment", today="2026-07-09",
    )
    # Same-source re-promote of the ORIGINAL card versions the original file.
    path = promote_card(
        _card(), framing="planning re-distill", est_item_id="d1",
        kind="activity", project_dir=tmp_path, sources=["mtg-1"],
        distiller=_distiller("planning v2"), model="m",
        name="1:1 stakeholder interviews", phase="discovery-alignment",
        today="2026-07-09",
    )
    assert path == first
    assert parse_substance(path).live_version().label == "v2"
