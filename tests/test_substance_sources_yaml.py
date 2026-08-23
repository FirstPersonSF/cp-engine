"""`sources:` round-trips for mapping-shaped entries (#216).

MC-2's `spine_substance.sources` holds `{id, title, type}` mappings. The
renderer used to prefix only the FIRST line of a dumped mapping with "- ",
leaving `title:`/`type:` at column 0 — YAML then rejected the block with
"mapping values are not allowed here", and 17 tenant files were silently
unparseable.
"""

from __future__ import annotations

import yaml

from cp_engine.substance import (
    _yaml_list_item,
    parse_substance,
    render_substance,
)

SRC = {
    "id": "3e380d1b-83bd-41a8-9e74-c317245cd1ce",
    "title": "Kacey-Flygare-transcript.txt",
    "type": "rag_asset",
}


def test_mapping_item_indents_continuation_lines():
    block = "sources:\n" + _yaml_list_item(SRC)
    parsed = yaml.safe_load(block)
    assert parsed == {"sources": [SRC]}


def test_mapping_item_has_no_column_zero_lines():
    """The exact defect: continuation lines must not start at column 0."""
    rendered = _yaml_list_item(SRC)
    lines = rendered.split("\n")
    assert len(lines) == 3
    assert all(ln.startswith("  ") for ln in lines), rendered


def test_plain_string_source_still_works():
    block = "sources:\n" + _yaml_list_item("some-plain-source")
    assert yaml.safe_load(block) == {"sources": ["some-plain-source"]}


def test_mixed_string_and_mapping_sources():
    block = "sources:\n" + _yaml_list_item(SRC) + "\n" + _yaml_list_item("plain")
    assert yaml.safe_load(block) == {"sources": [SRC, "plain"]}


def _write(tmp_path, sources_block: str):
    p = tmp_path / "el.md"
    p.write_text(
        "---\n"
        "est_item_id: e94d0a03-427d-4f26-b237-d9b732b0e402\n"
        "est_item_kind: context\n"
        "binding: live\n"
        "layer: Research\n"
        "---\n"
        "## v1 — 2026-07-13 · live\n"
        "framing: 1:1 Stakeholder Interviews\n"
        f"{sources_block}"
        "\n"
        "Body text.\n"
    )
    return p


def test_parser_keeps_mappings_as_mappings(tmp_path):
    """`str(s)` used to flatten a source dict to a Python repr string."""
    p = _write(
        tmp_path,
        "sources:\n"
        "  - id: 3e380d1b-83bd-41a8-9e74-c317245cd1ce\n"
        "    title: Kacey-Flygare-transcript.txt\n"
        "    type: rag_asset\n",
    )
    item = parse_substance(p)
    (src,) = item.versions[0].sources
    assert isinstance(src, dict)
    assert src["title"] == "Kacey-Flygare-transcript.txt"


def test_render_reparse_round_trip_is_lossless(tmp_path):
    p = _write(
        tmp_path,
        "sources:\n"
        "  - id: 3e380d1b-83bd-41a8-9e74-c317245cd1ce\n"
        "    title: Kacey-Flygare-transcript.txt\n"
        "    type: rag_asset\n",
    )
    item = parse_substance(p)
    out = render_substance(item)

    p2 = tmp_path / "el2.md"
    p2.write_text(out)
    item2 = parse_substance(p2)

    assert item2.versions[0].sources == item.versions[0].sources
    # Stable: a second render changes nothing.
    assert render_substance(item2).rstrip("\n") == out.rstrip("\n")


def test_rendered_output_is_parseable_yaml(tmp_path):
    """The regression that mattered: the writer must not emit what the
    parser rejects."""
    p = _write(
        tmp_path,
        "sources:\n"
        "  - id: abc\n"
        "    title: 'Tricky: colons, and \"quotes\"'\n"
        "    type: rag_asset\n",
    )
    item = parse_substance(p)
    p2 = tmp_path / "el2.md"
    p2.write_text(render_substance(item))
    reparsed = parse_substance(p2)  # must not raise
    assert reparsed.versions[0].sources == item.versions[0].sources
