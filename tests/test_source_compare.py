"""Tests for cp_engine.source_compare (#160) — unit extraction + alignment."""
from __future__ import annotations

import zipfile

from cp_engine.source_compare import compare_files, compare_units, extract_units

_A_NS = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
_W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _slide(texts: list[str]) -> str:
    runs = "".join(f"<a:t>{t}</a:t>" for t in texts)
    return f'<p:sld xmlns:p="urn:x" {_A_NS}><p:txBody>{runs}</p:txBody></p:sld>'


def _pptx(path, slides: list[list[str]]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for i, texts in enumerate(slides, 1):
            zf.writestr(f"ppt/slides/slide{i}.xml", _slide(texts))


def _docx(path, sections: list[tuple[str, str]]) -> None:
    paras = []
    for heading, body in sections:
        paras.append(
            f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            f"<w:r><w:t>{heading}</w:t></w:r></w:p>"
        )
        paras.append(f"<w:p><w:r><w:t>{body}</w:t></w:r></w:p>")
    doc = f'<w:document {_W_NS}><w:body>{"".join(paras)}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", doc)


def test_pptx_units_in_deck_order(tmp_path) -> None:
    p = tmp_path / "deck.pptx"
    # 11 slides so slide10 would sort before slide2 lexicographically.
    _pptx(p, [[f"Slide {i} body text"] for i in range(1, 12)])
    units = extract_units(p)
    assert [u.index for u in units] == list(range(1, 12))
    assert units[9].text == "Slide 10 body text"


def test_docx_units_by_heading(tmp_path) -> None:
    p = tmp_path / "doc.docx"
    _docx(p, [("Intro", "hello world"), ("Approach", "we do things")])
    units = extract_units(p)
    assert [u.label for u in units] == ["Intro", "Approach"]
    assert units[1].text == "we do things"


def test_md_units(tmp_path) -> None:
    p = tmp_path / "doc.md"
    p.write_text("preamble\n\n# One\nalpha\n\n## Two\nbeta\n")
    units = extract_units(p)
    assert [u.label for u in units] == ["(intro)", "One", "Two"]


def test_compare_detects_reorder_edit_cut_new(tmp_path) -> None:
    a = tmp_path / "r3.pptx"
    b = tmp_path / "r3updates.pptx"
    keep = "The platform pillars unite network security and cloud operations across every environment we serve"
    edited_a = "Customer proof points demonstrate measurable business value across industries"
    edited_b = "Customer proof points demonstrate measurable business value across industries and regions worldwide"
    cut = "This slide will be deleted in the revision entirely, goodbye"
    new = "A brand new strategic framing slide the client wrote from scratch overnight"
    _pptx(a, [[keep], [edited_a], [cut]])
    _pptx(b, [[new], [edited_b], [keep], ["tbd"]])  # keep moved 1→3; "tbd" placeholder
    result = compare_files(a, b)

    by_a = {m["a"]["index"]: m for m in result["matched"]}
    assert by_a[1]["b"]["index"] == 3 and by_a[1]["moved"]
    assert by_a[1]["verdict"] == "unchanged"
    assert by_a[2]["verdict"] == "edited"
    assert 0.5 <= by_a[2]["similarity"] < 0.95

    assert [c["index"] for c in result["cut"]] == [3]
    new_indexes = {n["index"] for n in result["new"]}
    assert new_indexes == {1, 4}
    assert any(n.get("placeholder") for n in result["new"] if n["index"] == 4)
    assert result["placeholders_in_b"][0]["index"] == 4
    assert 0 < result["overall_similarity"] < 1


def test_compare_identical_is_dup(tmp_path) -> None:
    a = tmp_path / "a.pptx"
    b = tmp_path / "b.pptx"
    slides = [["Same content on slide one here"], ["And slide two content here"]]
    _pptx(a, slides)
    _pptx(b, slides)
    result = compare_files(a, b)
    assert result["overall_similarity"] == 1.0
    assert all(m["verdict"] == "unchanged" for m in result["matched"])
    assert not result["cut"] and not result["new"]


def test_unsupported_extension_raises(tmp_path) -> None:
    p = tmp_path / "x.pdf"
    p.write_bytes(b"%PDF")
    try:
        extract_units(p)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert ".pdf" in str(exc)


def test_empty_vs_empty_units() -> None:
    result = compare_units([], [])
    assert result["matched"] == [] and result["cut"] == [] and result["new"] == []
