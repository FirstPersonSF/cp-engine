from pathlib import Path

import pytest

from cp_engine.substance import (
    SubstanceVersion,
    WorkItemSubstance,
    add_version,
    parse_substance,
    render_substance,
)

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
