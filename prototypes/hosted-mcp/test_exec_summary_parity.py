"""Exec Summary parsing: hosted uses the engine's slicer, stamp and threshold
(architecture plan step 1c; inventory H7, H21, H26, E10–E12).

The region slicer had 4 copies, the `· updated` stamp regex 6, the 14-day
threshold 4 with 3 comparison forms. Hosted's are now the engine's
`render.slice_exec_summary_region` / `EXEC_SUMMARY_START|END`,
`exec_summary_draft.stamp_date` and `exec_summary_draft.STALE_AFTER_DAYS`
(stale at >= 14 days — the form hosted already used). These pin that hosted
OUTPUT is unchanged across the cases where the copies could differ.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cp_engine import exec_summary_draft, render  # noqa: E402


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "http://example.invalid")
    mp.setenv("SUPABASE_ANON_KEY", "x")
    import server as mod

    yield mod
    mp.undo()


START, END = render.EXEC_SUMMARY_START, render.EXEC_SUMMARY_END

_BODIES = {
    "plain": f"# x\n\n{START}\n## Exec Summary  ·  updated 2026-09-15\n\n**Status:** ok\n{END}\n",
    "padded": f"{START}\n\n   \n## Exec Summary\n\n**Status:** ok   \n\n{END}",
    "empty": f"{START}{END}",
    "no_end": f"{START}\n## Exec Summary\n",
    "no_start": f"## Exec Summary\n{END}\n",
    "end_before_start": f"{END}\n{START}\nbody\n{END}",
    "none": "# just a cp.md\n",
}


def _old_hosted_slice(text: str) -> str | None:
    """The deleted hosted copy, verbatim, as the reference for 'unchanged'."""
    start = text.find("<!-- cp-engine:start exec-summary -->")
    end = text.find("<!-- cp-engine:end exec-summary -->", start + 1) if start >= 0 else -1
    if start < 0 or end < 0:
        return None
    return text[start + len("<!-- cp-engine:start exec-summary -->"): end].strip()


@pytest.mark.parametrize("name", sorted(_BODIES))
def test_extract_exec_summary_output_is_unchanged(server, tmp_path, name):
    cp_md = tmp_path / "cp.md"
    cp_md.write_text(_BODIES[name], encoding="utf-8")
    text, note = server.extract_exec_summary(cp_md)
    assert text == _old_hosted_slice(_BODIES[name])
    assert (note is None) is (text is not None)


def test_markers_are_the_engines(server):
    assert server._EXEC_START == render.EXEC_SUMMARY_START
    assert server._EXEC_END == render.EXEC_SUMMARY_END


def test_stale_threshold_is_the_engines(server):
    assert server._EXEC_STALE_GUARD_DAYS == exec_summary_draft.STALE_AFTER_DAYS == 14


@pytest.mark.parametrize("heading,expected", [
    ("## Exec Summary  ·  updated 2026-09-15", date(2026, 9, 15)),
    ("## Exec Summary · updated 2026-09-15  ·  drafted by cp", date(2026, 9, 15)),
    ("## Exec Summary", None),
    ("## Exec Summary · updated 2026-13-45", None),
    ("### Exec Summary · updated 2026-09-15", None),
])
def test_current_exec_stamp_reads_the_engine_stamp(server, monkeypatch, tmp_path, heading, expected):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / "cp.md").write_text(f"{START}\n{heading}\n\n**Status:** x\n{END}\n", encoding="utf-8")
    monkeypatch.setattr(server, "tree_available", lambda: (True, ""))
    monkeypatch.setattr(server, "caller_is_team_member", lambda: (True, ""))
    monkeypatch.setattr(server, "tree_root", lambda: tmp_path)
    monkeypatch.setattr(server, "find_project_dir", lambda root, code: proj)
    got = server._current_exec_stamp("p")
    assert got == expected
    region = render.slice_exec_summary_region((proj / "cp.md").read_text())
    assert got == exec_summary_draft.stamp_date(region)


def test_the_hosted_copies_are_gone(server):
    src = (Path(__file__).resolve().parent / "server.py").read_text(encoding="utf-8")
    assert "Exec Summary\\s*·\\s*updated" not in src, "a stamp regex copy is back"
    assert '"<!-- cp-engine:start exec-summary -->"' not in src
