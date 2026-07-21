# tests/test_doc_comments.py — #108 offline Office comment extraction.
import zipfile

import cp_engine.doc_comments as dc
import cp_engine.mcp_server as srv


def _zip(tmp_path, name, parts: dict) -> str:
    """Write a minimal OOXML-shaped zip with the given {arcname: xml} parts."""
    p = tmp_path / name
    with zipfile.ZipFile(p, "w") as zf:
        for arc, content in parts.items():
            zf.writestr(arc, content)
    return str(p)


# ── docx ─────────────────────────────────────────────────────────────────────

_DOCX_COMMENTS = """<?xml version="1.0"?>
<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:comment w:id="1" w:author="Scott" w:date="2026-07-18T10:00:00Z">
    <w:p><w:r><w:t>Don't overclaim — we don't close the loop autonomously.</w:t></w:r></w:p>
  </w:comment>
  <w:comment w:id="2" w:author="Cricket" w:date="2026-07-18T11:00:00Z">
    <w:p><w:r><w:t>"we define the standards" is too much — say "help develop".</w:t></w:r></w:p>
  </w:comment>
</w:comments>"""


def test_docx_comments_extracted_with_authors(tmp_path):
    path = _zip(tmp_path, "story.docx", {"word/comments.xml": _DOCX_COMMENTS})
    out = dc.extract_comments(path)
    assert [c["author"] for c in out] == ["Scott", "Cricket"]
    assert "overclaim" in out[0]["comment"]
    assert out[0]["date"] == "2026-07-18T10:00:00Z"


def test_docx_no_comments_part_returns_empty(tmp_path):
    path = _zip(tmp_path, "plain.docx", {"word/document.xml": "<x/>"})
    assert dc.extract_comments(path) == []


# ── xlsx (threaded) ──────────────────────────────────────────────────────────

_XLSX_PERSON = """<?xml version="1.0"?>
<personList xmlns="http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments">
  <person displayName="Carol" id="p1"/>
</personList>"""
_XLSX_TC = """<?xml version="1.0"?>
<ThreadedComments xmlns="http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments">
  <threadedComment ref="B2" dT="2026-07-01T09:00:00Z" personId="p1">
    <text>Align the pillars to the AI story.</text>
  </threadedComment>
</ThreadedComments>"""


def test_xlsx_threaded_comment_resolves_person_name(tmp_path):
    path = _zip(tmp_path, "book.xlsx", {
        "xl/persons/person.xml": _XLSX_PERSON,
        "xl/threadedComments/threadedComment1.xml": _XLSX_TC,
    })
    out = dc.extract_comments(path)
    assert len(out) == 1
    assert out[0]["author"] == "Carol"
    assert out[0]["anchored_text"] == "B2"
    assert "pillars" in out[0]["comment"]


# ── pptx ─────────────────────────────────────────────────────────────────────

_PPTX_COMMENT = """<?xml version="1.0"?>
<p:cmLst xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cm authorId="0" dt="2026-07-05T12:00:00Z"><p:text>Give IQ more weight.</p:text></p:cm>
</p:cmLst>"""


def test_pptx_comment_extracted(tmp_path):
    path = _zip(tmp_path, "deck.pptx", {"ppt/comments/comment1.xml": _PPTX_COMMENT})
    out = dc.extract_comments(path)
    assert len(out) == 1
    assert "IQ" in out[0]["comment"]


# ── dispatch + robustness ────────────────────────────────────────────────────

def test_unsupported_extension_returns_empty(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("hi")
    assert dc.extract_comments(str(p)) == []


def test_corrupt_zip_returns_empty(tmp_path):
    p = tmp_path / "broken.docx"
    p.write_bytes(b"not a zip")
    assert dc.extract_comments(str(p)) == []


def test_format_comments_renders_markdown():
    out = dc.format_comments([
        {"author": "Scott", "date": "2026-07-18", "anchored_text": None,
         "comment": "Don't overclaim.", "replies": [
             {"author": "Drew", "comment": "Agreed — fixing."}]},
    ])
    assert "## Comments" in out
    assert "**Scott**" in out
    assert "↳ **Drew**" in out


def test_format_comments_empty_is_blank():
    assert dc.format_comments([]) == ""


# ── MCP verb boundary ────────────────────────────────────────────────────────

def test_pull_document_comments_delegates(monkeypatch):
    monkeypatch.setattr(srv, "_resolve", lambda code: (object(), "pid", "cid"))
    captured = {}

    def _fake(client, pid, title, dest):
        captured["args"] = (pid, title)
        return {"title": title, "provider": "drive", "comment_count": 2,
                "comments": [{"author": "Scott"}, {"author": "Cricket"}]}
    monkeypatch.setattr("cp_engine.project_sources.pull_document_comments", _fake)

    out = srv.pull_document_comments("ibx-5153", "Our AI Story")
    assert out["comment_count"] == 2
    assert captured["args"][0] == "pid"


def test_pull_document_comments_unknown_project(monkeypatch):
    monkeypatch.setattr(srv, "_resolve", lambda code: None)
    out = srv.pull_document_comments("ghost", "x")
    assert "not found" in out["error"]


def test_pull_document_comments_never_raises(monkeypatch):
    def _boom(code):
        raise RuntimeError("resolver down")
    monkeypatch.setattr(srv, "_resolve", _boom)
    assert "error" in srv.pull_document_comments("x", "y")
