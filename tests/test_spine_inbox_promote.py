"""Frame + promote (Task 3.3) after architecture step 4c: a directed
re-distillation of a proposed inbox card becomes a new live version IN MC-2
(origin='distilled'), and only then is the element's file under ``spine/``
rendered from MC-2. Disk is never read to decide a version.
"""

from pathlib import Path

import pytest

from cp_engine.spine_inbox import promote_card, proposed_card
from cp_engine.substance import parse_substance
from tests._spine_fake import FakeClient


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


def _client(**tables):
    tables.setdefault("spine_inbox", [{"id": _card().id, "status": "framed"}])
    tables.setdefault("spine_substance", [])
    tables.setdefault("spine_steps", [])
    return FakeClient(**tables)


def _promote(client, tmp_path, **kw):
    args = dict(
        framing="lock the two-track thesis", est_item_id="d1", kind="deliverable",
        project_dir=tmp_path, sources=["mtg-42"], distiller=_distiller("body"),
        model="m", client=client, name="Messaging system",
        phase="Phase 0 Discovery", today="2026-06-15",
    )
    args.update(kw)
    card = args.pop("card", _card(args["est_item_id"]))
    return promote_card(card, **args)


def _rows(client, eid="d1"):
    return sorted((r for r in client.store["spine_substance"] if r["est_item_id"] == eid),
                  key=lambda r: r["version_label"])


# ---- first version -----------------------------------------------------------


def test_promote_writes_v1_to_mc2_then_renders_it(tmp_path: Path):
    client = _client()
    path = _promote(client, tmp_path, sources=["mtg-42", "carol-deck"],
                    distiller=_distiller("the directed distilled body"))

    (row,) = _rows(client)
    assert row["id"] == "ibx-5153/d1/v1"
    assert row["origin"] == "distilled"
    assert row["status"] == "live"
    assert row["version_date"] == "2026-06-15"
    assert row["framing"] == "lock the two-track thesis"
    assert row["sources"] == ["mtg-42", "carol-deck"]
    assert row["body"] == "the directed distilled body"
    assert row["phase"] == "Phase 0 Discovery"
    assert row["rel_path"] == "spine/phase-0-discovery/messaging-system.md"

    assert path == tmp_path / "spine" / "phase-0-discovery" / "messaging-system.md"
    item = parse_substance(path)
    assert item.est_item_id == "d1"
    assert item.est_item_kind == "deliverable"
    assert item.live_version().body == "the directed distilled body"


def test_promote_passes_framing_and_raw_to_distiller(tmp_path: Path):
    distiller = _distiller("body")
    _promote(_client(), tmp_path, card=_card(raw="the raw material here"),
             framing="my directing brief", distiller=distiller)
    assert "my directing brief" in distiller.calls[0]
    assert "the raw material here" in distiller.calls[0]


def test_promote_requires_mc2(tmp_path: Path):
    with pytest.raises(ValueError, match="MC-2 first"):
        _promote(None, tmp_path)
    assert not (tmp_path / "spine").exists()


def test_a_failed_mc2_write_renders_nothing(tmp_path: Path):
    client = _client()
    client.fail["upsert"] = {"spine_substance"}
    with pytest.raises(RuntimeError):
        _promote(client, tmp_path)
    assert not list(tmp_path.rglob("*.md"))
    assert client.store["spine_inbox"][0]["status"] == "framed"


# ---- next version --------------------------------------------------------------


def test_second_promote_adds_v2_and_demotes_v1_in_mc2(tmp_path: Path):
    client = _client()
    _promote(client, tmp_path, sources=["a"], distiller=_distiller("body one"),
             today="2026-04-23")
    path = _promote(client, tmp_path, framing="second pass", sources=["a"],
                    distiller=_distiller("body two"))
    v1, v2 = _rows(client)
    assert (v1["status"], v2["status"]) == ("superseded", "live")
    assert v2["body"] == "body two"
    item = parse_substance(path)
    assert [v.label for v in item.versions] == ["v2", "v1"]
    assert item.live_version().body == "body two"


def test_existing_element_keeps_its_path_phase_and_kind(tmp_path: Path):
    """The element's own rows are authoritative: a later promote carrying a
    different name/phase/kind versions the SAME element in the SAME file."""
    client = _client()
    first = _promote(client, tmp_path, name="Alpha", phase="Phase 0 Discovery",
                     sources=[])
    second = _promote(client, tmp_path, name="Beta", phase="Phase 9 Wrong",
                      kind="output", sources=[])
    assert second == first
    assert len(list((tmp_path / "spine").rglob("*.md"))) == 1
    v2 = _rows(client)[1]
    assert v2["phase"] == "Phase 0 Discovery"
    assert v2["est_item_kind"] == "deliverable"
    assert v2["rel_path"] == "spine/phase-0-discovery/alpha.md"


def test_next_label_is_after_mc2s_max_of_any_origin(tmp_path: Path):
    """MC-2 holds an authored v2 for the element: the distill mints v3 (#121)."""
    client = _client(spine_substance=[{
        "id": "ibx-5153/d1/v2", "project_id": "u1", "project_code": "ibx-5153",
        "est_item_id": "d1", "version_label": "v2", "origin": "authored",
        "status": "live",
    }])
    _promote(client, tmp_path)
    labels = {r["version_label"] for r in _rows(client) if r["origin"] == "distilled"}
    assert labels == {"v3"}


def test_card_flips_promoted_unless_deferred(tmp_path: Path):
    client = _client()
    _promote(client, tmp_path)
    assert client.store["spine_inbox"][0]["status"] == "promoted"
    client = _client()
    _promote(client, tmp_path / "other", flip_card=False)
    assert client.store["spine_inbox"][0]["status"] == "framed"


def test_a_hand_edit_in_the_rendered_file_is_quarantined_not_kept(tmp_path: Path):
    (tmp_path / ".cp-engine.toml").write_text("[tenant]\nname='t'\n")
    proj = tmp_path / "ws"
    client = _client()
    path = _promote(client, proj, sources=["a"], distiller=_distiller("one"))
    path.write_text(path.read_text().replace("one", "HAND"))
    _promote(client, proj, sources=["a"], distiller=_distiller("two"))
    assert "HAND" not in path.read_text()
    assert all("HAND" not in (r.get("body") or "") for r in client.store["spine_substance"])
    q = list((tmp_path / "exceptions" / "region-edits").glob("*.md"))
    assert len(q) == 1 and "HAND" in q[0].read_text()


# ---- issue #44: create-don't-version on source divergence --------------------


def _seed_bound(client, tmp_path, *, sources=("mtg-1",)):
    return _promote(client, tmp_path, framing="Planning the interview blocks",
                    kind="activity", sources=list(sources),
                    distiller=_distiller("planning body"),
                    name="1:1 stakeholder interviews", phase="discovery-alignment",
                    today="2026-06-24")


def _promote_paul(client, tmp_path, **kw):
    args = dict(framing="Interview with Paul Wu", kind="activity", sources=["mtg-2"],
                distiller=_distiller("paul wu body"),
                name="1:1 stakeholder interviews", phase="discovery-alignment",
                today="2026-07-09")
    args.update(kw)
    return _promote(client, tmp_path, **args)


def test_divergent_sources_creates_new_serving_element(tmp_path: Path):
    client = _client()
    first = _seed_bound(client, tmp_path)
    client.store["spine_inbox"][0]["status"] = "framed"
    path = _promote_paul(client, tmp_path)

    assert path == tmp_path / "spine" / "_authored" / "interview-with-paul-wu.md"
    item = parse_substance(path)
    assert item.est_item_id == "_authored/interview-with-paul-wu"
    assert item.serves == ("d1",)
    assert item.binding == "live"
    assert item.placement == "context"
    assert item.layer == "Activity"
    assert item.live_version().body == "paul wu body"

    # The bound element is untouched: one distilled version, planning body.
    (bound,) = _rows(client, "d1")
    assert bound["body"] == "planning body" and bound["status"] == "live"
    assert parse_substance(first).live_version().body == "planning body"

    (row,) = _rows(client, "_authored/interview-with-paul-wu")
    assert row["id"] == "ibx-5153/_authored/interview-with-paul-wu/v1"
    assert row["origin"] == "authored"
    assert row["serves"] == ["d1"]
    assert row["sources"] == ["mtg-2"]
    assert client.store["spine_inbox"][0]["status"] == "promoted"


def test_divergent_sources_flip_card_false_defers_flip(tmp_path: Path):
    client = _client()
    _seed_bound(client, tmp_path)
    client.store["spine_inbox"][0]["status"] = "framed"
    _promote_paul(client, tmp_path, flip_card=False)
    assert client.store["spine_inbox"][0]["status"] == "framed"
    assert _rows(client, "_authored/interview-with-paul-wu")


def test_divergent_sources_slug_collision_suffixes(tmp_path: Path):
    client = _client()
    _seed_bound(client, tmp_path)
    client.store["spine_substance"].append({
        "id": "ibx-5153/_authored/interview-with-paul-wu/v1", "project_id": "u1",
        "project_code": "ibx-5153", "est_item_id": "_authored/interview-with-paul-wu",
        "version_label": "v1", "status": "live", "origin": "authored",
    })
    authored_dir = tmp_path / "spine" / "_authored"
    authored_dir.mkdir(parents=True, exist_ok=True)
    (authored_dir / "interview-with-paul-wu-2.md").write_text("occupied")
    path = _promote_paul(client, tmp_path, sources=["mtg-9"])
    assert path.name == "interview-with-paul-wu-3.md"
    assert parse_substance(path).est_item_id == "_authored/interview-with-paul-wu-3"
    assert (authored_dir / "interview-with-paul-wu-2.md").read_text() == "occupied"


@pytest.mark.parametrize("prior,incoming", [
    (("mtg-1",), []),                      # no incoming sources
    ((), ["mtg-2"]),                       # live version has none
    (("mtg-1", "deck-a"), ["deck-a", "mtg-1"]),   # same set, any order
])
def test_non_divergent_sources_version(tmp_path: Path, prior, incoming):
    client = _client()
    _seed_bound(client, tmp_path, sources=prior)
    _promote(client, tmp_path, framing="re-frame", kind="activity",
             sources=incoming, distiller=_distiller("v2 body"),
             name="1:1 stakeholder interviews", phase="discovery-alignment")
    assert [r["status"] for r in _rows(client)] == ["superseded", "live"]
    assert not [r for r in client.store["spine_substance"] if r["origin"] == "authored"]


def test_created_element_never_becomes_the_versioning_target(tmp_path: Path):
    client = _client()
    first = _seed_bound(client, tmp_path)
    _promote_paul(client, tmp_path)
    path = _promote(client, tmp_path, framing="planning re-distill", kind="activity",
                    sources=["mtg-1"], distiller=_distiller("planning v2"),
                    name="1:1 stakeholder interviews", phase="discovery-alignment")
    assert path == first
    assert parse_substance(path).live_version().label == "v2"
