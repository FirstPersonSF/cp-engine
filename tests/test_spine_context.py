from pathlib import Path

import pytest

from cp_engine.spine_context import parse_context

CONTEXT_DIR = Path(__file__).parent / "fixtures" / "spine" / "_context"
CAROL = CONTEXT_DIR / "carol-deck.md"
DECISION = CONTEXT_DIR / "two-track-decision.md"

UUID = "7c9e6f2a-3b1d-4a5e-9f0c-2d8b6a4e1c33"


def test_parse_source_context():
    el = parse_context(CAROL)
    assert el.type == "source"
    assert el.provenance == "client"
    assert el.nature == "framework"
    assert el.links == (UUID,)
    assert el.body.strip()
    assert el.title == "carol-deck"


def test_decision_without_provenance_parses():
    el = parse_context(DECISION)
    assert el.type == "decision"
    assert el.provenance is None
    assert el.nature == "framework"
    assert el.links == (UUID,)


def test_unknown_type_raises(tmp_path):
    p = tmp_path / "gizmo.md"
    p.write_text("---\ntype: gizmo\n---\nbody\n")
    with pytest.raises(ValueError, match="type"):
        parse_context(p)


def test_source_out_of_vocab_provenance_raises(tmp_path):
    p = tmp_path / "bad.md"
    p.write_text("---\ntype: source\nprovenance: rumor\n---\nbody\n")
    with pytest.raises(ValueError, match="provenance"):
        parse_context(p)
