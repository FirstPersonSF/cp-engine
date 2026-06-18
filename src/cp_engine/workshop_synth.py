"""Workshop-synthesis pipeline (Piece B).

Drew's post-workshop hand-process, made into a 3-stage pipeline:

1. **Capture** (`capture_worksheet`) — vision-read each filled Miro worksheet
   (a PDF page) FAITHFULLY: transcribe columns / sections / sticky text /
   color-selection / author tags exactly, no interpretation.
2. **Hypotheses** (`worksheet_hypotheses`) — per board, what patterns/learnings
   does it reveal (what was emphasized — green/selected stickies, heavily
   discussed per the transcript)?
3. **Narrative** (`workshop_narrative`) — across ALL per-board hypotheses + the
   transcript, synthesize what the workshop taught: through-lines, tensions,
   decided vs open, the strategic read.

The **whole transcript** is ambient context at every stage. The worksheet (or
the prior stage's output) is always the subject.

Per-board unit: a single Miro PDF export often holds MANY worksheet pages (page
1 = Truth Map, etc.). The orchestrator splits a multi-page PDF into one
single-page PDF per page (via `pypdf`) and captures each board-page separately,
yielding one `<stem>-page<N>-capture.md` per board (or `<stem>-capture.md` for a
genuinely single-page PDF). Native-PDF reading (anthropic SDK document blocks)
preserves the visual layout + color the meaning rests on — no PDF→image step.

The three stage functions are PURE and take an injectable `vision`/`llm`
callable so tests never hit the API. `run_workshop_synth` orchestrates +
writes the reviewable artifacts; it is best-effort per worksheet (one board's
failure is logged + skipped, the rest still run, and the narrative runs over
whatever succeeded).
"""

from __future__ import annotations

import base64
import io
import os
from datetime import date
from pathlib import Path
from typing import Callable

from cp_engine.plan_from_transcript import PlanGenerationError, _call_claude

# Model pinned codebase-wide (do NOT bump).
DEFAULT_MODEL = "claude-opus-4-7"

VisionFn = Callable[[Path, str], str]
LLMFn = Callable[[str], str]


# --------------------------------------------------------------------------- #
# TASK 3 — vision Claude helper (native PDF)
# --------------------------------------------------------------------------- #


def _call_claude_pdf(
    pdf_path: Path,
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    client=None,
) -> str:
    """Single Anthropic call that reads a PDF natively (visual layout + color).

    Builds a content list of `[document(base64 of the PDF), text(prompt)]` so
    Claude reads the board's 2D structure directly. Mirrors `_call_claude`'s
    client construction (timeout=120), API-key resolution, and error wrapping.

    `client` is injectable for tests (default None → build a real Anthropic).
    """
    raw = Path(pdf_path).read_bytes()
    b64 = base64.standard_b64encode(raw).decode("ascii")

    content = [
        {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": "application/pdf",
                "data": b64,
            },
        },
        {"type": "text", "text": prompt},
    ]

    if client is None:
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise PlanGenerationError(
                "anthropic package not installed. Run: pip install 'anthropic>=0.40'"
            ) from exc

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise PlanGenerationError(
                "ANTHROPIC_API_KEY not set. Export it or pass --api-key."
            )
        # Explicit 120s timeout — same hardening as _call_claude.
        client = Anthropic(api_key=key, timeout=120)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=16384,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:  # anthropic SDK raises various subclasses
        detail = str(exc)
        body = getattr(exc, "body", None) or getattr(exc, "response", None)
        if body is not None:
            detail = f"{detail} | body={body!r}"
        raise PlanGenerationError(f"Anthropic PDF call failed: {detail}") from exc

    if not response.content:
        raise PlanGenerationError("Anthropic returned empty content")
    block = response.content[0]
    text = getattr(block, "text", None)
    if not text:
        raise PlanGenerationError(f"Anthropic returned non-text block: {block!r}")
    stop_reason = getattr(response, "stop_reason", None)
    if stop_reason == "max_tokens":
        raise PlanGenerationError(
            "Anthropic response truncated at max_tokens — the worksheet may be "
            f"very dense. Got {len(text)} chars before truncation."
        )
    return text


# --------------------------------------------------------------------------- #
# Prompt builders
# --------------------------------------------------------------------------- #


def _transcript_block(transcript: str) -> str:
    return (
        "\n\n--- WORKSHOP TRANSCRIPT (context only) ---\n"
        f"{transcript}\n"
        "--- END TRANSCRIPT ---\n"
    )


def _capture_prompt(transcript: str) -> str:
    return (
        "You are reading a completed workshop worksheet (a Miro board exported "
        "to PDF). Transcribe it EXACTLY and faithfully — do NOT interpret, "
        "summarize, or add. Preserve: every column and its header, every "
        "section/sub-section header, every sticky note's full text, the "
        "COLOR/selection state of each sticky (e.g. green = chosen/emphasized "
        "vs blue = standard), and any author tags/initials. Reproduce the "
        "board's structure in markdown so a reader knows which item sits in "
        "which column/section and which were emphasized. The workshop "
        "transcript is provided for context only — the WORKSHEET is what you "
        "transcribe."
        + _transcript_block(transcript)
    )


def _hypotheses_prompt(capture_text: str, transcript: str) -> str:
    return (
        "Here is a faithful reading of ONE workshop worksheet, plus the full "
        "workshop transcript. What patterns and learnings does THIS board "
        "reveal? Produce a set of hypotheses — what the board suggests, what "
        "was emphasized (the selected/green stickies, and what got heavily "
        "discussed per the transcript), notable tensions or signals. These are "
        "the learnings from this one board.\n\n"
        "--- WORKSHEET CAPTURE (the subject) ---\n"
        f"{capture_text}\n"
        "--- END CAPTURE ---"
        + _transcript_block(transcript)
    )


def _narrative_prompt(all_hypotheses: list[str], transcript: str) -> str:
    joined = "\n\n".join(
        f"=== WORKSHEET {i + 1} HYPOTHESES ===\n{h}"
        for i, h in enumerate(all_hypotheses)
    )
    return (
        "Across ALL these per-worksheet hypotheses and the full workshop "
        "transcript, synthesize what we learned from the workshop — the "
        "through-lines that span boards, the tensions surfaced, what's decided "
        "vs still open, and the strategic read. This is the overall "
        "synthesis.\n\n"
        "--- PER-WORKSHEET HYPOTHESES (the subject) ---\n"
        f"{joined}\n"
        "--- END HYPOTHESES ---"
        + _transcript_block(transcript)
    )


# --------------------------------------------------------------------------- #
# TASK 4 — three pure stage functions
# --------------------------------------------------------------------------- #


def capture_worksheet(
    pdf_path: Path,
    transcript: str,
    *,
    vision: VisionFn | None = None,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
) -> str:
    """Stage 1 — faithful vision capture of ONE worksheet PDF (page)."""
    prompt = _capture_prompt(transcript)
    if vision is not None:
        return vision(pdf_path, prompt)
    return _call_claude_pdf(pdf_path, prompt, model=model, api_key=api_key)


def worksheet_hypotheses(
    capture_text: str,
    transcript: str,
    *,
    llm: LLMFn | None = None,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
) -> str:
    """Stage 2 — patterns/learnings from one board's capture + the transcript."""
    prompt = _hypotheses_prompt(capture_text, transcript)
    if llm is not None:
        return llm(prompt)
    return _call_claude(prompt, model=model, api_key=api_key)


def workshop_narrative(
    all_hypotheses: list[str],
    transcript: str,
    *,
    llm: LLMFn | None = None,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
) -> str:
    """Stage 3 — cross-workshop synthesis over all per-board hypotheses."""
    prompt = _narrative_prompt(all_hypotheses, transcript)
    if llm is not None:
        return llm(prompt)
    return _call_claude(prompt, model=model, api_key=api_key)


# --------------------------------------------------------------------------- #
# Per-page PDF split
# --------------------------------------------------------------------------- #


def _split_pdf_pages(pdf_path: Path, work_dir: Path) -> list[tuple[Path, str]]:
    """Split a PDF into one single-page PDF per page.

    Returns a list of `(page_pdf_path, label)`. A single-page PDF yields one
    entry with an empty label (artifact named `<stem>-capture.md`); a
    multi-page PDF yields `(<stem>-pageN.pdf, "page<N>")` per page (artifact
    named `<stem>-pageN-capture.md`).
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(pdf_path))
    n = len(reader.pages)
    stem = pdf_path.stem

    if n <= 1:
        return [(pdf_path, "")]

    out: list[tuple[Path, str]] = []
    for i in range(n):
        writer = PdfWriter()
        writer.add_page(reader.pages[i])
        buf = io.BytesIO()
        writer.write(buf)
        page_path = work_dir / f"{stem}-page{i + 1}.pdf"
        page_path.write_bytes(buf.getvalue())
        out.append((page_path, f"page{i + 1}"))
    return out


def _artifact_stem(pdf_stem: str, label: str) -> str:
    return f"{pdf_stem}-{label}" if label else pdf_stem


def _header(kind: str, code_or_subject: str) -> str:
    return (
        f"<!-- generated-by: cp workshop-synth ({kind}) -->\n"
        f"<!-- workshop: {code_or_subject} · {date.today().isoformat()} -->\n\n"
    )


# --------------------------------------------------------------------------- #
# TASK 5 — orchestration
# --------------------------------------------------------------------------- #


def run_workshop_synth(
    *,
    worksheet_pdfs: list[Path],
    transcript_path: Path,
    out_dir: Path,
    model: str = DEFAULT_MODEL,
    api_key: str | None = None,
    vision: VisionFn | None = None,
    llm: LLMFn | None = None,
) -> dict:
    """Run the 3-stage pipeline and write the reviewable artifacts.

    For each board-page across all worksheet PDFs: Stage 1 capture → write
    `<stem>[-pageN]-capture.md`; Stage 2 hypotheses → write
    `<stem>[-pageN]-hypotheses.md`. Then Stage 3 narrative over all collected
    hypotheses → `workshop-synthesis.md`.

    Best-effort per board: if one board's capture/hypotheses raises, it's noted
    in `skipped` and skipped; the rest still run and the narrative runs over
    whatever succeeded.

    Returns `{captures: [paths], hypotheses: [paths], narrative: path|None,
    skipped: [str]}`.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    transcript = Path(transcript_path).read_text(encoding="utf-8")

    subject = out_dir.name  # e.g. the workshop-date dir

    captures: list[Path] = []
    hypotheses_paths: list[Path] = []
    hypotheses_texts: list[str] = []
    skipped: list[str] = []

    for pdf in worksheet_pdfs:
        pdf = Path(pdf)
        try:
            board_pages = _split_pdf_pages(pdf, out_dir)
        except Exception as exc:  # noqa: BLE001 — corrupt/unreadable PDF
            skipped.append(f"{pdf} (split failed: {exc})")
            continue

        for page_pdf, label in board_pages:
            astem = _artifact_stem(pdf.stem, label)
            try:
                capture_text = capture_worksheet(
                    page_pdf, transcript, vision=vision, model=model, api_key=api_key
                )
                hypo_text = worksheet_hypotheses(
                    capture_text, transcript, llm=llm, model=model, api_key=api_key
                )
            except Exception as exc:  # noqa: BLE001 — LLM/transport per board
                skipped.append(f"{page_pdf} ({exc})")
                continue

            cap_path = out_dir / f"{astem}-capture.md"
            cap_path.write_text(
                _header("stage 1 capture", subject) + capture_text + "\n",
                encoding="utf-8",
            )
            captures.append(cap_path)

            hyp_path = out_dir / f"{astem}-hypotheses.md"
            hyp_path.write_text(
                _header("stage 2 hypotheses", subject) + hypo_text + "\n",
                encoding="utf-8",
            )
            hypotheses_paths.append(hyp_path)
            hypotheses_texts.append(hypo_text)

    narrative_path: Path | None = None
    if hypotheses_texts:
        narrative_text = workshop_narrative(
            hypotheses_texts, transcript, llm=llm, model=model, api_key=api_key
        )
        narrative_path = out_dir / "workshop-synthesis.md"
        narrative_path.write_text(
            _header("stage 3 narrative", subject) + narrative_text + "\n",
            encoding="utf-8",
        )

    return {
        "captures": captures,
        "hypotheses": hypotheses_paths,
        "narrative": narrative_path,
        "skipped": skipped,
    }
