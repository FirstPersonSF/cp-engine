from pathlib import Path

from cp_engine.shell import _as_source, parse_element
from cp_engine.shell_sources import match_sources_to_assets


# --- _as_source -----------------------------------------------------------


def test_as_source_preserves_mixed_str_and_dict() -> None:
    link = {"type": "rag_asset", "id": "abc", "title": "T"}
    out = _as_source(["plain.md", link])
    assert out == ("plain.md", {"type": "rag_asset", "id": "abc", "title": "T"})
    # dict stays a dict, string stays a string
    assert isinstance(out[0], str)
    assert isinstance(out[1], dict)


def test_as_source_scalar_becomes_one_tuple_of_str() -> None:
    out = _as_source("solo.md")
    assert out == ("solo.md",)
    assert isinstance(out[0], str)


def test_as_source_none_is_empty() -> None:
    assert _as_source(None) == ()


# --- parse_element round-trip ---------------------------------------------


def test_parse_element_preserves_typed_source_dict(tmp_path: Path) -> None:
    f = tmp_path / "brief-distilled.md"
    f.write_text(
        "---\n"
        "id: ibx-5153/brief/distilled\n"
        "project: ibx-5153\n"
        "layer: Brief\n"
        "title: Distilled brief\n"
        "status: active\n"
        "last_touched: 2026-06-13\n"
        "source:\n"
        "  - synthesis-docs/client_input_brief_distilled.md\n"
        "  - {type: rag_asset, id: abc, title: T}\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )

    el = parse_element(f)

    assert el.source[0] == "synthesis-docs/client_input_brief_distilled.md"
    assert isinstance(el.source[0], str)
    assert el.source[1] == {"type": "rag_asset", "id": "abc", "title": "T"}
    assert isinstance(el.source[1], dict)


# --- match_sources_to_assets ----------------------------------------------


def test_match_replaces_string_ref_with_typed_link() -> None:
    assets = [
        {"id": "asset-1", "title": "client_input_brief_distilled.md"},
    ]
    refs = ("synthesis-docs/client_input_brief_distilled.md", "nope.txt")
    out = match_sources_to_assets(refs, assets)
    assert out[0] == {
        "type": "rag_asset",
        "id": "asset-1",
        "title": "client_input_brief_distilled.md",
    }
    # unmatched ref stays a plain string
    assert out[1] == "nope.txt"


def test_match_passes_through_existing_dict() -> None:
    assets = [{"id": "asset-1", "title": "x.md"}]
    existing = {"type": "rag_asset", "id": "other", "title": "Y"}
    out = match_sources_to_assets((existing,), assets)
    assert out == (existing,)


def test_match_is_case_insensitive_and_ignores_dirs() -> None:
    assets = [{"id": "a", "title": "IBX 5153 - Phase I Working Plan.docx"}]
    refs = ("Reference Materials/Phase 1/ibx 5153 - phase i working plan.docx",)
    out = match_sources_to_assets(refs, assets)
    assert out[0]["type"] == "rag_asset"
    assert out[0]["id"] == "a"


def test_match_determinism_smaller_id_wins() -> None:
    assets = [
        {"id": "z-id", "title": "dup.md"},
        {"id": "a-id", "title": "dup.md"},
    ]
    out = match_sources_to_assets(("dir/dup.md",), assets)
    assert out[0]["id"] == "a-id"
