"""Tests for cp_engine.summary — master-CP one-line summary derivation.

The master-CP one-liner is derived from the project cp.md's engine-managed
``exec-summary`` region (replacing the old ``## Quick Resume`` source):

1. The ``**Status:**`` field value (a one-phrase field), if authored.
2. Else the first real ``Where it stands`` bullet.
3. Else the first non-placeholder paragraph of ``## Current Work``.
4. Else None.
"""

from __future__ import annotations

from pathlib import Path

from cp_engine.summary import derive_from_project_cp


def _write(tmp_path: Path, body: str) -> Path:
    cp = tmp_path / "cp.md"
    cp.write_text(body, encoding="utf-8")
    return cp


def test_derive_reads_exec_summary_status(tmp_path: Path) -> None:
    """An authored Status phrase becomes the master-CP one-liner."""
    body = """\
<!-- cp-engine:start exec-summary -->
## Exec Summary  ·  updated 2026-06-30

**Last session:** 2026-06-28
**Objective:** Ship the activation playbooks.
**Status:** Round 3 with Rena, awaiting feedback.

**Where it stands:**
- Pop-up Round 3 shared.

**Next up:**
- _<concrete near-term moves>_

**Blockers:**
- None

**Updates:**
- 2026-06-30 — wrapped sprint.
<!-- cp-engine:end exec-summary -->

## Current Work

Some current-work paragraph that should NOT win.
"""
    out = derive_from_project_cp(_write(tmp_path, body))
    assert out == "Round 3 with Rena, awaiting feedback."


def test_derive_falls_back_to_where_bullet(tmp_path: Path) -> None:
    """Placeholder Status falls through to the first real Where-it-stands bullet."""
    body = """\
<!-- cp-engine:start exec-summary -->
## Exec Summary  ·  updated 2026-06-30

**Last session:** _<date>_
**Objective:** _<one line>_
**Status:** _<current state in a phrase>_

**Where it stands:**
- Pop-up Round 3 shared with Rena.
- Second bullet of reality.

**Next up:**
- _<concrete near-term moves>_

**Blockers:**
- _<or "None">_

**Updates:**
- 2026-06-30 — migrated from Quick Resume
<!-- cp-engine:end exec-summary -->

## Current Work

This current-work paragraph should NOT win.
"""
    out = derive_from_project_cp(_write(tmp_path, body))
    assert out == "Pop-up Round 3 shared with Rena."


def test_derive_falls_back_to_current_work_section(tmp_path: Path) -> None:
    """Unauthored exec-summary (all placeholder) falls through to Current Work."""
    body = """\
<!-- cp-engine:start exec-summary -->
## Exec Summary  ·  updated 2026-06-30

**Last session:** _<date>_
**Objective:** _<one line>_
**Status:** _<current state in a phrase>_

**Where it stands:**
- _<2-4 dense bullets of current reality>_

**Next up:**
- _<concrete near-term moves>_

**Blockers:**
- _<or "None">_

**Updates:**
- 2026-06-30 — migrated from Quick Resume
<!-- cp-engine:end exec-summary -->

## Current Work

Real current-work paragraph that wins as last fallback.
"""
    out = derive_from_project_cp(_write(tmp_path, body))
    assert out == "Real current-work paragraph that wins as last fallback."


def test_derive_returns_none_when_all_placeholder(tmp_path: Path) -> None:
    """Fresh region + placeholder Current Work → None (empty master-CP cell)."""
    body = """\
<!-- cp-engine:start exec-summary -->
## Exec Summary  ·  updated 2026-06-30

**Last session:** _<date>_
**Objective:** _<one line>_
**Status:** _<current state in a phrase>_

**Where it stands:**
- _<2-4 dense bullets of current reality>_

**Next up:**
- _<concrete near-term moves>_

**Blockers:**
- _<or "None">_

**Updates:**
- 2026-06-30 — migrated from Quick Resume
<!-- cp-engine:end exec-summary -->

## Current Work

_<2-10 paragraphs of substantive notes.>_
"""
    out = derive_from_project_cp(_write(tmp_path, body))
    assert out is None
