"""Workshop synthesis verb: workshop-synth.

Split from cli.py (arch-phase-4, #33). Shared helpers stay in
cp_engine.cli and are called via the module attribute so test
monkeypatches of `cp_engine.cli.<helper>` keep working.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

import cp_engine.cli as _cli


@click.command("workshop-synth")
@click.argument("code")
@click.option(
    "--worksheets",
    "worksheets",
    multiple=True,
    type=click.Path(exists=True, path_type=Path),
    help=(
        "Worksheet PDF path(s), or a directory of PDFs. Repeatable. "
        "Default: <project>/workshop-worksheets/ if it exists."
    ),
)
@click.option(
    "--transcript",
    "transcript",
    type=click.Path(exists=True, path_type=Path),
    help=(
        "Workshop transcript .txt. Default: the newest "
        "<project>/meeting-transcripts/*.txt."
    ),
)
@click.option(
    "--out",
    "out",
    type=click.Path(path_type=Path),
    help="Output dir. Default: <project>/workshop-synthesis/<today>/.",
)
@click.option("--model", default="claude-opus-4-7", show_default=True)
def workshop_synth_cmd(
    code: str,
    worksheets: tuple[Path, ...],
    transcript: Path | None,
    out: Path | None,
    model: str,
) -> None:
    """Synthesize a strategy workshop: vision-read each worksheet → per-board
    hypotheses → cross-workshop narrative, with the whole transcript as ambient
    context at every stage. Writes reviewable artifacts to the output dir."""
    from datetime import date

    from cp_engine import workshop_synth
    from cp_engine.spine import find_spine_dir

    config = _cli._load_config_or_die()
    project_dir = find_spine_dir(config.root, code)

    # Resolve worksheet PDFs: explicit paths/dirs, else conventional dir.
    pdfs: list[Path] = []
    sources = list(worksheets)
    if not sources:
        conv = project_dir / "workshop-worksheets"
        if not conv.is_dir():
            click.echo(
                "No --worksheets given and no workshop-worksheets/ dir found "
                f"at {conv}. Pass --worksheets <path-or-dir>.",
                err=True,
            )
            sys.exit(1)
        sources = [conv]
    for src in sources:
        src = Path(src)
        if src.is_dir():
            pdfs.extend(sorted(src.glob("*.pdf")))
        else:
            pdfs.append(src)
    if not pdfs:
        click.echo("No worksheet PDFs found.", err=True)
        sys.exit(1)

    # Resolve transcript: explicit, else newest meeting-transcripts/*.txt.
    if transcript is None:
        tdir = project_dir / "meeting-transcripts"
        txts = sorted(tdir.glob("*.txt")) if tdir.is_dir() else []
        if not txts:
            click.echo(
                "No --transcript given and no meeting-transcripts/*.txt found "
                f"at {tdir}. Pass --transcript <path>.",
                err=True,
            )
            sys.exit(1)
        transcript = max(txts, key=lambda p: p.stat().st_mtime)

    # Resolve output dir.
    if out is None:
        out = project_dir / "workshop-synthesis" / date.today().isoformat()

    # Up-front feedback: the vision/LLM calls take minutes and cost money, so
    # show what's being processed before going quiet.
    click.echo(
        f"Synthesizing {code}: {len(pdfs)} worksheet PDF(s), "
        f"transcript {transcript.name} → {out}"
    )
    for pdf in pdfs:
        click.echo(f"  worksheet: {pdf.name}")
    click.echo("Running vision + synthesis (this can take several minutes)…")

    try:
        result = workshop_synth.run_workshop_synth(
            worksheet_pdfs=pdfs,
            transcript_path=transcript,
            out_dir=out,
            model=model,
        )
    except Exception as exc:  # noqa: BLE001 — LLM/transport/IO failure
        click.echo(
            f"Workshop synthesis failed (is ANTHROPIC_API_KEY set?): {exc}",
            err=True,
        )
        sys.exit(1)

    click.echo(f"Transcript: {transcript}")
    click.echo(f"Captures ({len(result['captures'])}):")
    for p in result["captures"]:
        click.echo(f"  {p}")
    click.echo(f"Hypotheses ({len(result['hypotheses'])}):")
    for p in result["hypotheses"]:
        click.echo(f"  {p}")
    if result["narrative"]:
        click.echo(f"Narrative: {result['narrative']}")
    else:
        click.echo("Narrative: (not written — no worksheets succeeded)")
    if result["skipped"]:
        click.echo(f"Skipped ({len(result['skipped'])}):", err=True)
        for s in result["skipped"]:
            click.echo(f"  {s}", err=True)


