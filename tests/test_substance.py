from pathlib import Path

import pytest

from cp_engine.substance import (
    SubstanceVersion,
    WorkItemSubstance,
    add_version,
    derive_placement,
    is_skipped_spine_dir,
    parse_substance,
    render_substance,
)


@pytest.mark.parametrize(
    "parts, expected",
    [
        (("_context", "carol.md"), True),
        (("_authored", "note-1.md"), True),
        (("Phase0", "pos.snapshots", "frozen.md"), True),
        (("pos.snapshots",), True),
        (("Phase 0", "foo.md"), False),
        (("phase-0-message-strategy", "messaging-system.md"), False),
    ],
)
def test_is_skipped_spine_dir(parts, expected):
    assert is_skipped_spine_dir(parts) is expected

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "spine"
    / "phase-0-discovery"
    / "pp-report.md"
)


# --- Task 1.1: parse -------------------------------------------------------


def test_parse_substance_frontmatter():
    item = parse_substance(FIXTURE)
    assert item.est_item_id == "7c9e6f2a-3b1d-4a5e-9f0c-2d8b6a4e1c33"
    assert item.est_item_kind == "deliverable"
    assert item.binding == "live"
    assert item.phase == "Phase 0 Discovery & Alignment"


def test_parse_substance_versions():
    item = parse_substance(FIXTURE)
    assert len(item.versions) == 2

    v3 = item.versions[0]
    assert v3.label == "v3"
    assert v3.status == "live"
    assert v3.date == "2026-06-11"
    assert v3.framing.startswith("two-track")
    assert v3.sources == ("janet-5-27-transcript", "carol-deck#p12-18")
    assert "two-track story" in v3.body
    assert "framing:" not in v3.body
    assert "sources:" not in v3.body
    assert "## v3" not in v3.body

    v2 = item.versions[1]
    assert v2.label == "v2"
    assert v2.status == "superseded"


def test_live_version_returns_the_one_live():
    item = parse_substance(FIXTURE)
    live = item.live_version()
    assert live.label == "v3"
    assert live.status == "live"


def test_two_live_versions_raises(tmp_path):
    content = (
        "---\n"
        "est_item_id: abc-123\n"
        "est_item_kind: deliverable\n"
        "binding: live\n"
        "---\n"
        "## v2 — 2026-06-11 · live\n"
        "framing: first\n"
        "sources:\n"
        "  - a\n"
        "\n"
        "body two\n"
        "\n"
        "## v1 — 2026-04-23 · live\n"
        "framing: second\n"
        "sources:\n"
        "  - b\n"
        "\n"
        "body one\n"
    )
    p = tmp_path / "twolive.md"
    p.write_text(content)
    with pytest.raises(ValueError, match="exactly one"):
        parse_substance(p)


def test_missing_required_key_raises(tmp_path):
    content = (
        "---\n"
        "est_item_kind: deliverable\n"
        "---\n"
        "## v1 — 2026-04-23 · live\n"
        "framing: x\n"
        "sources:\n"
        "  - a\n"
        "\n"
        "body\n"
    )
    p = tmp_path / "missing.md"
    p.write_text(content)
    with pytest.raises(ValueError, match="est_item_id"):
        parse_substance(p)


# --- Task 1.2: serialize + add_version -------------------------------------


def test_render_substance_round_trips():
    item = parse_substance(FIXTURE)
    rendered = render_substance(item)
    original = FIXTURE.read_text()
    assert rendered.rstrip("\n") == original.rstrip("\n")


# --- Review fixes: C1/C2/I1-I3/M1 ------------------------------------------


def test_framing_with_colons_parsed_as_literal(tmp_path):
    """C1: framing prose with YAML-hostile chars must parse verbatim."""
    framing = "the thesis: two-track AI story — IQ vs MCP, 3:1 odds #campaign"
    content = (
        "---\n"
        "est_item_id: abc-123\n"
        "est_item_kind: deliverable\n"
        "binding: live\n"
        "---\n"
        "## v1 — 2026-06-11 · live\n"
        f"framing: {framing}\n"
        "sources:\n"
        "  - a\n"
        "\n"
        "body\n"
    )
    p = tmp_path / "colon.md"
    p.write_text(content)
    item = parse_substance(p)
    assert item.versions[0].framing == framing
    assert item.versions[0].sources == ("a",)


def test_render_round_trips_programmatic_special_chars(tmp_path):
    """C2 + M4: programmatic objects with special chars survive render->parse."""
    v = SubstanceVersion(
        label="v1",
        date="2026-06-15",
        status="live",
        framing="key: value — and a #hash, 'quoted'",
        sources=("src:with-colon", "plain-source"),
        body="A buildable body paragraph.",
    )
    item = WorkItemSubstance(
        est_item_id="abc-123",
        est_item_kind="deliverable",
        phase="Phase: tricky",
        binding="live",
        versions=(v,),
        path=tmp_path / "prog.md",
    )
    out = tmp_path / "prog.md"
    out.write_text(render_substance(item))
    reparsed = parse_substance(out)
    rv = reparsed.versions[0]
    assert rv.framing == "key: value — and a #hash, 'quoted'"
    assert rv.sources == ("src:with-colon", "plain-source")
    assert reparsed.phase == "Phase: tricky"
    assert reparsed.est_item_id == "abc-123"


def test_subheading_in_body_not_split_as_version(tmp_path):
    """I1: a `## ` subheading inside a body must stay in the body."""
    content = (
        "---\n"
        "est_item_id: abc-123\n"
        "est_item_kind: deliverable\n"
        "binding: live\n"
        "---\n"
        "## v1 — 2026-06-11 · live\n"
        "framing: x\n"
        "sources:\n"
        "  - a\n"
        "\n"
        "Intro paragraph.\n"
        "\n"
        "## Key risks\n"
        "\n"
        "Risk one.\n"
    )
    p = tmp_path / "subhead.md"
    p.write_text(content)
    item = parse_substance(p)
    assert len(item.versions) == 1
    assert "## Key risks" in item.versions[0].body
    assert "Risk one." in item.versions[0].body


def test_unknown_frontmatter_key_preserved(tmp_path):
    """I2: extra frontmatter keys survive parse->render."""
    content = (
        "---\n"
        "est_item_id: abc-123\n"
        "est_item_kind: deliverable\n"
        "binding: live\n"
        "mc2_row_id: 999\n"
        "---\n"
        "## v1 — 2026-06-11 · live\n"
        "framing: x\n"
        "sources:\n"
        "  - a\n"
        "\n"
        "body\n"
    )
    p = tmp_path / "extra.md"
    p.write_text(content)
    item = parse_substance(p)
    rendered = render_substance(item)
    assert "mc2_row_id: 999" in rendered
    # round trip stable
    p.write_text(rendered)
    reparsed = parse_substance(p)
    assert render_substance(reparsed) == rendered


def test_preamble_before_first_version_raises(tmp_path):
    """I3: non-whitespace text before the first version header is an error."""
    content = (
        "---\n"
        "est_item_id: abc-123\n"
        "est_item_kind: deliverable\n"
        "binding: live\n"
        "---\n"
        "stray preamble text\n"
        "\n"
        "## v1 — 2026-06-11 · live\n"
        "framing: x\n"
        "sources:\n"
        "  - a\n"
        "\n"
        "body\n"
    )
    p = tmp_path / "preamble.md"
    p.write_text(content)
    with pytest.raises(ValueError, match=str(p)):
        parse_substance(p)


def test_whitespace_preamble_ok(tmp_path):
    """I3: pure-whitespace preamble before the first version is fine."""
    content = (
        "---\n"
        "est_item_id: abc-123\n"
        "est_item_kind: deliverable\n"
        "binding: live\n"
        "---\n"
        "\n"
        "\n"
        "## v1 — 2026-06-11 · live\n"
        "framing: x\n"
        "sources:\n"
        "  - a\n"
        "\n"
        "body\n"
    )
    p = tmp_path / "ws.md"
    p.write_text(content)
    item = parse_substance(p)
    assert len(item.versions) == 1


# --- Task 3.2: layer + placement + serves ----------------------------------


def test_parse_layer_and_placement_item(tmp_path):
    content = (
        "---\n"
        "est_item_id: abc-123\n"
        "est_item_kind: deliverable\n"
        "phase: Message Strategy & Campaign Framing\n"
        "binding: live\n"
        "layer: Deliverables\n"
        "placement: item\n"
        "---\n"
        "## v1 — 2026-06-12 · live\n"
        "framing: x\n"
        "sources:\n"
        "  - a\n"
        "\n"
        "body\n"
    )
    p = tmp_path / "layer.md"
    p.write_text(content)
    item = parse_substance(p)
    assert item.layer == "Deliverables"
    assert item.placement == "item"
    assert item.serves == ()
    # round-trips
    assert render_substance(parse_substance(p)) == render_substance(item)
    p.write_text(render_substance(item))
    reparsed = parse_substance(p)
    assert reparsed.layer == "Deliverables"
    assert reparsed.placement == "item"


def test_parse_placement_context_with_serves(tmp_path):
    content = (
        "---\n"
        "est_item_id: _authored/ctx-1\n"
        "est_item_kind: deliverable\n"
        "binding: live\n"
        "layer: Decisions\n"
        "placement: context\n"
        "serves:\n"
        "  - abc-123\n"
        "  - def-456\n"
        "---\n"
        "## v1 — 2026-06-12 · live\n"
        "framing: x\n"
        "sources:\n"
        "  - a\n"
        "\n"
        "body\n"
    )
    p = tmp_path / "ctx.md"
    p.write_text(content)
    item = parse_substance(p)
    assert item.placement == "context"
    assert item.serves == ("abc-123", "def-456")
    assert isinstance(item.serves, tuple)
    assert item.layer == "Decisions"


def test_defaults_when_new_fields_absent(tmp_path):
    item = parse_substance(FIXTURE)
    assert item.placement == "item"
    assert item.layer is None
    assert item.serves == ()
    assert item.archived is False


def test_archived_true_parses_and_round_trips(tmp_path):
    """A UI-archived element carries `archived: true` in frontmatter; it must
    parse to True, emit the `archived: true` line (the True side of the emit
    conditional — the default-False omit is covered by the round-trip fixture),
    and round-trip idempotently (render → re-parse → re-render is stable)."""
    content = (
        "---\n"
        "est_item_id: _authored/ctx-1\n"
        "est_item_kind: deliverable\n"
        "binding: live\n"
        "layer: Decisions\n"
        "placement: context\n"
        "serves:\n"
        "- abc-123\n"
        "archived: true\n"
        "---\n"
        "## v1 — 2026-06-12 · live\n"
        "framing: x\n"
        "sources:\n"
        "  - a\n"
        "\n"
        "body\n"
    )
    p = tmp_path / "archived.md"
    p.write_text(content)
    item = parse_substance(p)
    assert item.archived is True
    rendered = render_substance(item)
    assert "archived: true" in rendered
    # Idempotent: parsing the rendered output and re-rendering is byte-stable.
    p.write_text(rendered)
    assert render_substance(parse_substance(p)) == rendered


def test_new_fields_not_duplicated_in_extra(tmp_path):
    """layer/placement/serves are real fields now, NOT in extra — render must
    not double-emit them."""
    # Authored key so the DERIVED placement is `context` — the case that
    # actually emits a `placement:` line (an item-placement row omits it).
    content = (
        "---\n"
        "est_item_id: _authored/abc-123\n"
        "est_item_kind: deliverable\n"
        "binding: live\n"
        "layer: Deliverables\n"
        "placement: context\n"
        "serves:\n"
        "  - abc-123\n"
        "---\n"
        "## v1 — 2026-06-12 · live\n"
        "framing: x\n"
        "sources:\n"
        "  - a\n"
        "\n"
        "body\n"
    )
    p = tmp_path / "noextra.md"
    p.write_text(content)
    item = parse_substance(p)
    assert "layer" not in item.extra
    assert "placement" not in item.extra
    assert "serves" not in item.extra
    rendered = render_substance(item)
    # each new key appears exactly once in the frontmatter
    assert rendered.count("layer:") == 1
    assert rendered.count("placement:") == 1
    assert rendered.count("serves:") == 1


def test_new_fields_round_trip_with_serves(tmp_path):
    content = (
        "---\n"
        "est_item_id: _authored/ctx-1\n"
        "est_item_kind: deliverable\n"
        "binding: live\n"
        "layer: Decisions\n"
        "placement: context\n"
        "serves:\n"
        "  - abc-123\n"
        "---\n"
        "## v1 — 2026-06-12 · live\n"
        "framing: x\n"
        "sources:\n"
        "  - a\n"
        "\n"
        "body\n"
    )
    p = tmp_path / "rt.md"
    p.write_text(content)
    item = parse_substance(p)
    rendered = render_substance(item)
    p.write_text(rendered)
    reparsed = parse_substance(p)
    assert render_substance(reparsed) == rendered
    assert reparsed.serves == ("abc-123",)
    assert reparsed.placement == "context"
    assert reparsed.layer == "Decisions"


def test_add_version_demotes_prior_live():
    item = parse_substance(FIXTURE)
    new = SubstanceVersion(
        label="v4",
        date="2026-06-15",
        status="live",
        framing="post-workshop refinement",
        sources=("workshop-6-11-transcript",),
        body="The post-workshop pass tightens the proof points under each verb.",
    )
    updated = add_version(item, new)

    assert updated.versions[0].label == "v4"
    assert updated.versions[0].status == "live"
    # exactly one live afterward
    lives = [v for v in updated.versions if v.status == "live"]
    assert len(lives) == 1
    assert lives[0].label == "v4"
    # prior live (v3) is now superseded
    v3 = next(v for v in updated.versions if v.label == "v3")
    assert v3.status == "superseded"
    assert updated.live_version().label == "v4"


# --- #182: placement is DERIVED from the key shape, never authored ----------


def test_derive_placement_from_key_shape():
    """An `_authored/*` element has no estimate slot by construction and
    belongs on the context shelf; a uuid-keyed element IS a slot."""
    assert derive_placement("_authored/inputs-briefing") == "context"
    assert derive_placement("_authored/janet-noe-client-lead") == "context"
    assert derive_placement("5fca0b9c-b1ae-4622-9b39-e187e63ff9ab") == "item"
    assert derive_placement("d1") == "item"


def test_derive_placement_does_not_match_a_lookalike_prefix():
    """Only the `_authored/` PATH prefix counts — a slug that merely starts
    with the word must not be swept into the context shelf."""
    assert derive_placement("_authored-notes") == "item"
    assert derive_placement("authored/thing") == "item"


def test_frontmatter_placement_is_ignored_in_favour_of_the_key(tmp_path):
    """A `placement:` line that contradicts the key loses.

    This is the whole point of the change: placement stops being an
    independently-editable field. `e94d0a03` is the real row this models — a
    uuid-keyed element carrying `placement: context` on disk.
    """
    content = (
        "---\n"
        "est_item_id: e94d0a03-427d-4f26-b237-d9b732b0e402\n"
        "est_item_kind: activity\n"
        "binding: unbound\n"
        "layer: Activity\n"
        "placement: context\n"        # contradicts the uuid key
        "---\n"
        "## v1 — 2026-07-09 · live\n"
        "framing: 1:1 Stakeholder Interviews\n"
        "\n"
        "body\n"
    )
    p = tmp_path / "e94d0a03.md"
    p.write_text(content)
    item = parse_substance(p)
    assert item.placement == "item"
    # ...and it does NOT leak into extra, which would double-emit on render.
    assert "placement" not in item.extra
