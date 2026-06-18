"""Tests for the workshop-synthesis pipeline (Piece B).

The three stage functions and the vision helper are PURE + injectable — no test
here hits the real Anthropic API. Each test passes a fake `vision`/`llm`
callable (or a fake Anthropic `client`) and asserts the prompt/transcript/inputs
reach the right place.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from cp_engine import workshop_synth as ws


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]
        self.stop_reason = "end_turn"


class _FakeMessages:
    def __init__(self, recorder: dict) -> None:
        self._recorder = recorder

    def create(self, **kwargs):
        self._recorder["kwargs"] = kwargs
        return _FakeResponse("CALL-OUTPUT")


class _FakeClient:
    """Stands in for an `anthropic.Anthropic` instance."""

    def __init__(self) -> None:
        self.recorder: dict = {}
        self.messages = _FakeMessages(self.recorder)


# --------------------------------------------------------------------------- #
# Anthropic call helper + prompt-cache content layout
# --------------------------------------------------------------------------- #


def test_call_messages_passes_content_and_returns_text() -> None:
    """`_call_messages` forwards a prebuilt content list and returns the text."""
    client = _FakeClient()
    content = [{"type": "text", "text": "HELLO"}]
    out = ws._call_messages(content, client=client)

    assert out == "CALL-OUTPUT"
    msgs = client.recorder["kwargs"]["messages"]
    assert len(msgs) == 1
    assert msgs[0]["content"] == content


def test_transcript_block_is_cached_and_identical() -> None:
    """The transcript block carries `cache_control: ephemeral` and is a pure
    fn of the transcript text — byte-identical across calls (cache-prefix req)."""
    block = ws._transcript_block("THE TRANSCRIPT")
    assert block["cache_control"] == {"type": "ephemeral"}
    assert block["type"] == "text"
    assert "THE TRANSCRIPT" in block["text"]
    # Identical bytes for the same input → stable cache prefix.
    assert ws._transcript_block("THE TRANSCRIPT") == block


def test_capture_content_layout(tmp_path: Path) -> None:
    """Capture content = [cached transcript, per-page PDF, capture instruction].

    Transcript is block 0 (the cached prefix, same position as the text stages);
    the per-page PDF differs per call and sits AFTER the cache marker."""
    raw = b"%PDF-1.4 tiny bytes"
    b64 = base64.standard_b64encode(raw).decode("ascii")
    content = ws._capture_content(b64, "THE TRANSCRIPT")

    assert len(content) == 3
    # Block 0 = cached transcript.
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert "THE TRANSCRIPT" in content[0]["text"]
    # Block 1 = the PDF (NOT cached — differs per page).
    assert content[1] == {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": b64,
        },
    }
    # Block 2 = the capture instruction.
    assert content[2]["type"] == "text"
    assert "cache_control" not in content[2]
    assert "faithful" in content[2]["text"].lower()


def test_transcript_block_position_stable_across_stages() -> None:
    """Block 0 is the cached transcript across capture/hypotheses/narrative so
    the cached prefix is byte-identical (and shareable) across all calls."""
    t = "SHARED TRANSCRIPT"
    cap = ws._capture_content("ZmFrZQ==", t)
    hyp = ws._hypotheses_content("CAP", t)
    nar = ws._narrative_content(["H1"], t)
    assert cap[0] == hyp[0] == nar[0]
    assert cap[0]["cache_control"] == {"type": "ephemeral"}


# --------------------------------------------------------------------------- #
# TASK 4 — stage functions
# --------------------------------------------------------------------------- #


def _flatten(content: list[dict]) -> str:
    """Concatenate all text blocks' text — for substring assertions over the
    content-list shape the injectable seams now receive."""
    return "\n".join(b.get("text", "") for b in content if b.get("type") == "text")


def _recording_vision(store: dict):
    def _vision(pdf_path: Path, content: list[dict]) -> str:
        store["pdf_path"] = pdf_path
        store["content"] = content
        return "CAPTURE-TEXT"

    return _vision


def _recording_llm(store: dict):
    def _llm(content: list[dict]) -> str:
        store["content"] = content
        return "LLM-OUTPUT"

    return _llm


def test_capture_worksheet_passes_faithful_prompt_and_transcript(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "truth-map.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    store: dict = {}

    out = ws.capture_worksheet(
        pdf, "THE WHOLE TRANSCRIPT TEXT", vision=_recording_vision(store)
    )

    assert out == "CAPTURE-TEXT"
    assert store["pdf_path"] == pdf
    content = store["content"]
    text = _flatten(content)
    # Faithful-capture instructions present.
    assert "faithful" in text.lower()
    assert "do not interpret" in text.lower() or "transcribe it exactly" in text.lower()
    assert "color" in text.lower()
    # Transcript reached the vision call — in its own cached block.
    assert "THE WHOLE TRANSCRIPT TEXT" in text
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert "THE WHOLE TRANSCRIPT TEXT" in content[0]["text"]
    # The board PDF is present too.
    assert any(b.get("type") == "document" for b in content)


def test_worksheet_hypotheses_passes_capture_and_transcript() -> None:
    store: dict = {}
    out = ws.worksheet_hypotheses(
        "FAITHFUL CAPTURE BODY",
        "THE WHOLE TRANSCRIPT TEXT",
        llm=_recording_llm(store),
    )

    assert out == "LLM-OUTPUT"
    content = store["content"]
    text = _flatten(content)
    assert "FAITHFUL CAPTURE BODY" in text
    assert "THE WHOLE TRANSCRIPT TEXT" in text
    assert "hypotheses" in text.lower()
    # Transcript is its own cached block.
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert "THE WHOLE TRANSCRIPT TEXT" in content[0]["text"]


def test_workshop_narrative_passes_all_hypotheses_and_transcript() -> None:
    store: dict = {}
    out = ws.workshop_narrative(
        ["HYPO BOARD ONE", "HYPO BOARD TWO"],
        "THE WHOLE TRANSCRIPT TEXT",
        llm=_recording_llm(store),
    )

    assert out == "LLM-OUTPUT"
    content = store["content"]
    text = _flatten(content)
    assert "HYPO BOARD ONE" in text
    assert "HYPO BOARD TWO" in text
    assert "THE WHOLE TRANSCRIPT TEXT" in text
    assert "synthesi" in text.lower()
    # Transcript is its own cached block.
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert "THE WHOLE TRANSCRIPT TEXT" in content[0]["text"]


# --------------------------------------------------------------------------- #
# TASK 5 — run_workshop_synth orchestration
# --------------------------------------------------------------------------- #


def _make_pdf(path: Path) -> None:
    # A minimal single-page PDF so the per-page splitter sees exactly one page.
    path.write_bytes(_ONE_PAGE_PDF)


# A genuinely valid 1-page PDF (so pypdf can read page count = 1).
_ONE_PAGE_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"xref\n0 4\n"
    b"0000000000 65535 f \n"
    b"0000000009 00000 n \n"
    b"0000000052 00000 n \n"
    b"0000000101 00000 n \n"
    b"trailer<</Size 4/Root 1 0 R>>\n"
    b"startxref\n164\n%%EOF\n"
)


def test_run_workshop_synth_writes_artifacts(tmp_path: Path) -> None:
    pdf_a = tmp_path / "board-a.pdf"
    pdf_b = tmp_path / "board-b.pdf"
    _make_pdf(pdf_a)
    _make_pdf(pdf_b)
    transcript = tmp_path / "workshop.txt"
    transcript.write_text("FULL TRANSCRIPT", encoding="utf-8")
    out_dir = tmp_path / "out"

    narrative_inputs: dict = {}

    def _vision(p: Path, content: list[dict]) -> str:
        return f"CAPTURE for {p.name}"

    def _llm(content: list[dict]) -> str:
        text = _flatten(content)
        # Stage 3 narrative mentions "synthesi"; stage 2 mentions hypotheses.
        if "synthesi" in text.lower():
            narrative_inputs["text"] = text
            return "NARRATIVE"
        return "HYPOTHESES"

    result = ws.run_workshop_synth(
        worksheet_pdfs=[pdf_a, pdf_b],
        transcript_path=transcript,
        out_dir=out_dir,
        vision=_vision,
        llm=_llm,
    )

    # 2 captures + 2 hypotheses + 1 narrative
    assert len(result["captures"]) == 2
    assert len(result["hypotheses"]) == 2
    assert result["narrative"] == out_dir / "workshop-synthesis.md"
    assert result["skipped"] == []

    for p in result["captures"] + result["hypotheses"] + [result["narrative"]]:
        assert Path(p).exists()

    # single-page PDFs → <stem>-capture.md (no -pageN suffix)
    assert (out_dir / "board-a-capture.md").exists()
    assert (out_dir / "board-b-capture.md").exists()

    # out_dir holds ONLY .md artifacts — no leaked intermediate split PDFs.
    assert sorted(p.name for p in out_dir.glob("*.pdf")) == []

    # The narrative saw BOTH hypotheses bodies.
    assert "HYPOTHESES" in narrative_inputs["text"]
    # The transcript reaches the narrative.
    assert "FULL TRANSCRIPT" in narrative_inputs["text"]


def test_run_workshop_synth_skips_failed_worksheet(tmp_path: Path) -> None:
    pdf_good = tmp_path / "good.pdf"
    pdf_bad = tmp_path / "bad.pdf"
    _make_pdf(pdf_good)
    _make_pdf(pdf_bad)
    transcript = tmp_path / "t.txt"
    transcript.write_text("T", encoding="utf-8")
    out_dir = tmp_path / "out"

    def _vision(p: Path, content: list[dict]) -> str:
        if p.read_bytes() == _ONE_PAGE_PDF and "bad" in str(p):
            raise RuntimeError("vision boom")
        return f"CAP {p.name}"

    def _llm(content: list[dict]) -> str:
        return "NARRATIVE" if "synthesi" in _flatten(content).lower() else "HYPO"

    result = ws.run_workshop_synth(
        worksheet_pdfs=[pdf_good, pdf_bad],
        transcript_path=transcript,
        out_dir=out_dir,
        vision=_vision,
        llm=_llm,
    )

    # good completes, bad is skipped, narrative still runs.
    assert len(result["captures"]) == 1
    assert len(result["hypotheses"]) == 1
    assert result["narrative"] is not None
    assert Path(result["narrative"]).exists()
    assert len(result["skipped"]) == 1
    assert "bad.pdf" in str(result["skipped"][0])


def test_run_workshop_synth_all_failed(tmp_path: Path) -> None:
    """Degenerate path: EVERY capture raises → narrative is None, all boards
    skipped, no narrative file written, no crash."""
    pdf_a = tmp_path / "a.pdf"
    pdf_b = tmp_path / "b.pdf"
    _make_pdf(pdf_a)
    _make_pdf(pdf_b)
    transcript = tmp_path / "t.txt"
    transcript.write_text("T", encoding="utf-8")
    out_dir = tmp_path / "out"

    def _vision(p: Path, content: list[dict]) -> str:
        raise RuntimeError("vision always boom")

    llm_called: dict = {"count": 0}

    def _llm(content: list[dict]) -> str:
        llm_called["count"] += 1
        return "SHOULD-NOT-RUN"

    result = ws.run_workshop_synth(
        worksheet_pdfs=[pdf_a, pdf_b],
        transcript_path=transcript,
        out_dir=out_dir,
        vision=_vision,
        llm=_llm,
    )

    assert result["narrative"] is None
    assert result["captures"] == []
    assert result["hypotheses"] == []
    assert len(result["skipped"]) == 2
    assert all("boom" in s for s in result["skipped"])
    # No narrative file written; narrative stage never ran.
    assert not (out_dir / "workshop-synthesis.md").exists()
    assert llm_called["count"] == 0


def test_run_workshop_synth_multipage_splits_per_page(
    tmp_path: Path, monkeypatch
) -> None:
    """A 3-page PDF yields one capture per page named <stem>-pageN-capture.md."""
    pdf = tmp_path / "multi.pdf"

    # Build a real 3-page PDF with pypdf so the splitter sees 3 pages.
    from pypdf import PdfReader, PdfWriter
    import io

    src = PdfReader(io.BytesIO(_ONE_PAGE_PDF))
    writer = PdfWriter()
    for _ in range(3):
        writer.add_page(src.pages[0])
    with pdf.open("wb") as fh:
        writer.write(fh)

    transcript = tmp_path / "t.txt"
    transcript.write_text("T", encoding="utf-8")
    out_dir = tmp_path / "out"

    def _vision(p: Path, content: list[dict]) -> str:
        return "CAP"

    def _llm(content: list[dict]) -> str:
        return "NARRATIVE" if "synthesi" in _flatten(content).lower() else "HYPO"

    result = ws.run_workshop_synth(
        worksheet_pdfs=[pdf],
        transcript_path=transcript,
        out_dir=out_dir,
        vision=_vision,
        llm=_llm,
    )

    assert len(result["captures"]) == 3
    assert (out_dir / "multi-page1-capture.md").exists()
    assert (out_dir / "multi-page2-capture.md").exists()
    assert (out_dir / "multi-page3-capture.md").exists()
    # The intermediate split PDFs did NOT leak into out_dir.
    assert list(out_dir.glob("*.pdf")) == []


# --------------------------------------------------------------------------- #
# CLI — cp workshop-synth resolving defaults
# --------------------------------------------------------------------------- #


def test_cli_workshop_synth_resolves_defaults(tmp_path: Path, monkeypatch) -> None:
    from click.testing import CliRunner

    from cp_engine import cli as cli_mod

    project_dir = tmp_path / "1p" / "infoblox" / "ibx-5153-ai-campaign"
    worksheets = project_dir / "workshop-worksheets"
    worksheets.mkdir(parents=True)
    (worksheets / "board.pdf").write_bytes(_ONE_PAGE_PDF)

    transcripts = project_dir / "meeting-transcripts"
    transcripts.mkdir()
    older = transcripts / "2026-06-10 Old.txt"
    newer = transcripts / "2026-06-17 Workshop.txt"
    older.write_text("old", encoding="utf-8")
    newer.write_text("new", encoding="utf-8")
    # Make `newer` the newest by mtime.
    import os
    import time

    os.utime(older, (time.time() - 1000, time.time() - 1000))

    # Fake config + resolution.
    class _Config:
        root = tmp_path

    monkeypatch.setattr(cli_mod, "_load_config_or_die", lambda: _Config())
    monkeypatch.setattr(
        "cp_engine.spine.find_spine_dir", lambda root, code: project_dir
    )

    captured: dict = {}

    def _fake_run(**kwargs):
        captured.update(kwargs)
        narr = kwargs["out_dir"] / "workshop-synthesis.md"
        return {
            "captures": [],
            "hypotheses": [],
            "narrative": narr,
            "skipped": [],
        }

    monkeypatch.setattr(ws, "run_workshop_synth", _fake_run)

    runner = CliRunner()
    res = runner.invoke(cli_mod.main, ["workshop-synth", "ibx-5153"])

    assert res.exit_code == 0, res.output
    assert captured["worksheet_pdfs"] == [worksheets / "board.pdf"]
    assert captured["transcript_path"] == newer
    assert captured["out_dir"].parent == project_dir / "workshop-synthesis"
