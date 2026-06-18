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
        return _FakeResponse("VISION-OUTPUT")


class _FakeClient:
    """Stands in for an `anthropic.Anthropic` instance."""

    def __init__(self) -> None:
        self.recorder: dict = {}
        self.messages = _FakeMessages(self.recorder)


# --------------------------------------------------------------------------- #
# TASK 3 — _call_claude_pdf
# --------------------------------------------------------------------------- #


def test_call_claude_pdf_block_shape(tmp_path: Path) -> None:
    """The PDF call sends `[document(base64 of pdf bytes), text(prompt)]` and
    returns the response's text."""
    pdf = tmp_path / "tiny.pdf"
    raw = b"%PDF-1.4 tiny bytes"
    pdf.write_bytes(raw)

    client = _FakeClient()
    out = ws._call_claude_pdf(pdf, "READ THIS BOARD", client=client)

    assert out == "VISION-OUTPUT"
    msgs = client.recorder["kwargs"]["messages"]
    assert len(msgs) == 1
    content = msgs[0]["content"]
    assert content[0] == {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(raw).decode("ascii"),
        },
    }
    assert content[1] == {"type": "text", "text": "READ THIS BOARD"}


# --------------------------------------------------------------------------- #
# TASK 4 — stage functions
# --------------------------------------------------------------------------- #


def _recording_vision(store: dict):
    def _vision(pdf_path: Path, prompt: str) -> str:
        store["pdf_path"] = pdf_path
        store["prompt"] = prompt
        return "CAPTURE-TEXT"

    return _vision


def _recording_llm(store: dict):
    def _llm(prompt: str) -> str:
        store["prompt"] = prompt
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
    prompt = store["prompt"]
    # Faithful-capture instructions present.
    assert "faithful" in prompt.lower()
    assert "do not interpret" in prompt.lower() or "transcribe it exactly" in prompt.lower()
    assert "color" in prompt.lower()
    # Transcript reached the vision call as context.
    assert "THE WHOLE TRANSCRIPT TEXT" in prompt


def test_worksheet_hypotheses_passes_capture_and_transcript() -> None:
    store: dict = {}
    out = ws.worksheet_hypotheses(
        "FAITHFUL CAPTURE BODY",
        "THE WHOLE TRANSCRIPT TEXT",
        llm=_recording_llm(store),
    )

    assert out == "LLM-OUTPUT"
    prompt = store["prompt"]
    assert "FAITHFUL CAPTURE BODY" in prompt
    assert "THE WHOLE TRANSCRIPT TEXT" in prompt
    assert "hypotheses" in prompt.lower()


def test_workshop_narrative_passes_all_hypotheses_and_transcript() -> None:
    store: dict = {}
    out = ws.workshop_narrative(
        ["HYPO BOARD ONE", "HYPO BOARD TWO"],
        "THE WHOLE TRANSCRIPT TEXT",
        llm=_recording_llm(store),
    )

    assert out == "LLM-OUTPUT"
    prompt = store["prompt"]
    assert "HYPO BOARD ONE" in prompt
    assert "HYPO BOARD TWO" in prompt
    assert "THE WHOLE TRANSCRIPT TEXT" in prompt
    assert "synthesi" in prompt.lower()


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

    captures: list[str] = []
    narrative_inputs: dict = {}

    def _vision(p: Path, prompt: str) -> str:
        return f"CAPTURE for {p.name}"

    def _llm(prompt: str) -> str:
        # Stage 3 narrative prompt mentions "synthesi"; stage 2 mentions hypotheses.
        if "synthesi" in prompt.lower():
            narrative_inputs["prompt"] = prompt
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

    # The narrative saw BOTH hypotheses bodies.
    assert "HYPOTHESES" in narrative_inputs["prompt"]
    # The transcript reaches the narrative.
    assert "FULL TRANSCRIPT" in narrative_inputs["prompt"]


def test_run_workshop_synth_skips_failed_worksheet(tmp_path: Path) -> None:
    pdf_good = tmp_path / "good.pdf"
    pdf_bad = tmp_path / "bad.pdf"
    _make_pdf(pdf_good)
    _make_pdf(pdf_bad)
    transcript = tmp_path / "t.txt"
    transcript.write_text("T", encoding="utf-8")
    out_dir = tmp_path / "out"

    def _vision(p: Path, prompt: str) -> str:
        if p.read_bytes() == _ONE_PAGE_PDF and "bad" in str(p):
            raise RuntimeError("vision boom")
        return f"CAP {p.name}"

    narrative_seen: dict = {}

    def _llm(prompt: str) -> str:
        if "synthesi" in prompt.lower():
            narrative_seen["prompt"] = prompt
            return "NARRATIVE"
        return "HYPO"

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

    def _vision(p: Path, prompt: str) -> str:
        return "CAP"

    def _llm(prompt: str) -> str:
        return "NARRATIVE" if "synthesi" in prompt.lower() else "HYPO"

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
