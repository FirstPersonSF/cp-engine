"""Size guard for binaries a tenant tracks in git.

PDFs are tracked (2026-11-10): a document someone shares should reach every
clone, not sit on one laptop when its workstream has no Dropbox folder. A
`.gitignore` cannot filter by size, so the two commands that sweep untracked
files into a commit — `cxp session commit` and `cxp capture-session` — leave
any PDF over the limit out and say so. GitHub refuses files over 100 MB and
warns from 50 MB; 20 MB keeps a clone light.
"""

from __future__ import annotations

from pathlib import Path

MAX_TRACKED_PDF_BYTES = 20 * 1024 * 1024


def too_large_to_track(path: Path) -> bool:
    """True for a PDF on disk larger than `MAX_TRACKED_PDF_BYTES`."""
    if path.suffix.lower() != ".pdf":
        return False
    try:
        return path.is_file() and path.stat().st_size > MAX_TRACKED_PDF_BYTES
    except OSError:
        return False


def limit_label() -> str:
    return f"{MAX_TRACKED_PDF_BYTES // (1024 * 1024)} MB"
