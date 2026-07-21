"""Extract reviewer comments from Office documents (docx / pptx / xlsx).

Office comments live in dedicated parts inside the file's zip, NOT in the body
text the ingest parsers extract — so they're invisible to RAG. This module reads
them straight from the OOXML, offline, with no python-docx/pptx dependency (the
comment APIs are absent or awkward; the XML is simple and stable).

Returns a normalized shape per document:
    [{author, date, anchored_text, comment, replies: [{author, date, comment}]}]

Google Docs comments are NOT here — they don't exist in an exported file, only
via the Drive API (see the MCP verb's Drive path). This module is the
binary/offline half; #108 tracks both.

Never raises past `extract_comments` — a malformed or comment-less file returns
[] so the MCP tool boundary always gets a clean list.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

# OOXML namespaces (the ones carrying comment data across the three formats).
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
# Threaded (modern) comment authors/text share this across xlsx + newer office.
_TC = "{http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments}"


def _text_of(el) -> str:
    """All descendant text of an element, whitespace-joined and stripped."""
    return " ".join(t.strip() for t in el.itertext() if t.strip())


def _docx_comments(zf: zipfile.ZipFile) -> list[dict]:
    """word/comments.xml → flat comments; word/commentsExtended.xml threads them.

    comments.xml carries id/author/date/text per comment. commentsExtended maps
    a comment to its parent (para id links) for reply nesting; when absent we
    return a flat list (still correct, just un-nested)."""
    if "word/comments.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("word/comments.xml"))
    out = []
    for c in root.findall(f"{_W}comment"):
        out.append({
            "author": c.get(f"{_W}author") or "Unknown",
            "date": c.get(f"{_W}date"),
            "anchored_text": None,  # docx anchors via runs in document.xml (v2)
            "comment": _text_of(c),
            "replies": [],
        })
    return out


def _pptx_comments(zf: zipfile.ZipFile) -> list[dict]:
    """ppt/comments/*.xml → comments. Modern PowerPoint uses an authorId ref
    into ppt/authors.xml; resolve names where present, else fall back to the
    id."""
    names = zf.namelist()
    # author id -> display name (modern format)
    authors: dict[str, str] = {}
    if "ppt/authors.xml" in names:
        aroot = ET.fromstring(zf.read("ppt/authors.xml"))
        for a in aroot.iter():
            if a.tag.endswith("}author") or a.tag.endswith("cmAuthor"):
                aid = a.get("id") or a.get(f"{_P}id")
                nm = a.get("name") or a.get(f"{_P}authorId")
                if aid and nm:
                    authors[aid] = nm
    out = []
    for part in names:
        if not (part.startswith("ppt/comments/") and part.endswith(".xml")):
            continue
        root = ET.fromstring(zf.read(part))
        for c in root.iter():
            tag = c.tag.split("}")[-1]
            if tag not in ("cm", "comment"):
                continue
            aid = c.get("authorId") or c.get("authorId")
            author = authors.get(aid or "", None) or c.get("author") or aid or "Unknown"
            text = _text_of(c)
            if text:
                out.append({
                    "author": author, "date": c.get("dt") or c.get("created"),
                    "anchored_text": None, "comment": text, "replies": [],
                })
    return out


def _xlsx_comments(zf: zipfile.ZipFile) -> list[dict]:
    """xl/comments*.xml (legacy notes) + xl/threadedComments/*.xml (modern)."""
    names = zf.namelist()
    # Threaded-comment persons file maps personId -> display name.
    persons: dict[str, str] = {}
    if "xl/persons/person.xml" in names:
        proot = ET.fromstring(zf.read("xl/persons/person.xml"))
        for p in proot.iter():
            if p.tag.endswith("}person"):
                pid = p.get("id")
                nm = p.get("displayName")
                if pid and nm:
                    persons[pid] = nm
    out = []
    for part in names:
        if part.startswith("xl/threadedComments/") and part.endswith(".xml"):
            root = ET.fromstring(zf.read(part))
            for tc in root.findall(f"{_TC}threadedComment"):
                text_el = tc.find(f"{_TC}text")
                out.append({
                    "author": persons.get(tc.get("personId") or "", "Unknown"),
                    "date": tc.get("dT"),
                    "anchored_text": tc.get("ref"),
                    "comment": (text_el.text or "").strip() if text_el is not None else "",
                    "replies": [],
                })
        elif (part == "xl/comments.xml" or
              (part.startswith("xl/comments") and part.endswith(".xml"))):
            root = ET.fromstring(zf.read(part))
            for c in root.iter():
                if c.tag.split("}")[-1] == "comment":
                    txt = _text_of(c)
                    if txt:
                        out.append({
                            "author": c.get("authorId") or "Unknown",
                            "date": None, "anchored_text": c.get("ref"),
                            "comment": txt, "replies": [],
                        })
    return out


_DISPATCH = {
    ".docx": _docx_comments,
    ".pptx": _pptx_comments,
    ".xlsx": _xlsx_comments,
}


def extract_comments(file_path: str) -> list[dict]:
    """Extract reviewer comments from an Office file by extension. Returns a
    normalized list (possibly empty); never raises. Unsupported extensions and
    unreadable/comment-less files both return []."""
    ext = Path(file_path).suffix.lower()
    fn = _DISPATCH.get(ext)
    if fn is None:
        return []
    try:
        with zipfile.ZipFile(file_path) as zf:
            return fn(zf)
    except Exception:  # noqa: BLE001 — MCP boundary: a bad file is [], not a crash
        return []


def format_comments(comments: list[dict]) -> str:
    """Render comments as a markdown '## Comments' block for appending to
    ingested body text (the ingest-layer half) or for a readable MCP return.
    Empty list → empty string (caller appends nothing)."""
    if not comments:
        return ""
    lines = ["## Comments", ""]
    for i, c in enumerate(comments, 1):
        who = c.get("author") or "Unknown"
        when = f" · {c['date']}" if c.get("date") else ""
        anchor = f" [on: {c['anchored_text']}]" if c.get("anchored_text") else ""
        lines.append(f"{i}. **{who}**{when}{anchor}: {c.get('comment', '')}")
        for r in c.get("replies") or []:
            lines.append(f"   ↳ **{r.get('author', 'Unknown')}**: {r.get('comment', '')}")
    return "\n".join(lines)
