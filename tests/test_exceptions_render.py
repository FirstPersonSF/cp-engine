"""Tests for `render_exceptions_readme` and `count_exceptions_in_window`."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

from cp_engine.render import (
    count_exceptions_in_window,
    render_exceptions_readme,
)


def make_exception(
    exceptions_dir: Path,
    *,
    when: datetime,
    repo: str,
    user: str,
    suffix: str = "",
) -> Path:
    """Create a fake exception file with the standard filename shape."""
    exceptions_dir.mkdir(parents=True, exist_ok=True)
    base = (
        f"{when.strftime('%Y-%m-%d')}-{repo}-"
        f"{when.strftime('%H%M')}-{user}{suffix}"
    )
    path = exceptions_dir / f"{base}.md"
    path.write_text(f"## Session: {when.isoformat()}, {user}\n\nbody\n", encoding="utf-8")
    # Force mtime to match the named time so mtime-fallback also works.
    epoch = when.timestamp()
    os.utime(path, (epoch, epoch))
    return path


# ──────────────────────────────────────────────────────────────────────
#  render_exceptions_readme
# ──────────────────────────────────────────────────────────────────────


def test_empty_exceptions_dir_renders_with_none_marker(tmp_path: Path) -> None:
    body = render_exceptions_readme(tmp_path)
    assert "<!-- cp-engine:start exceptions-list -->" in body
    assert "<!-- cp-engine:end exceptions-list -->" in body
    assert "_(none yet)_" in body


def test_exceptions_dir_missing_renders_with_none_marker(tmp_path: Path) -> None:
    """No exceptions/ subdir at all → still renders cleanly."""
    body = render_exceptions_readme(tmp_path)
    assert "_(none yet)_" in body


def test_renders_recent_exceptions_newest_first(tmp_path: Path) -> None:
    when = datetime(2026, 5, 9, 14, 30)
    exceptions = tmp_path / "exceptions"
    make_exception(exceptions, when=when - timedelta(hours=2), repo="lib-a", user="drew")
    make_exception(exceptions, when=when - timedelta(hours=1), repo="lib-b", user="tony")
    make_exception(exceptions, when=when, repo="lib-c", user="drew")

    body = render_exceptions_readme(tmp_path, now=when + timedelta(minutes=5))

    # Three entries, newest first
    lines = [
        line
        for line in body.splitlines()
        if line.startswith("- ")
    ]
    assert len(lines) == 3
    assert "lib-c" in lines[0]
    assert "lib-b" in lines[1]
    assert "lib-a" in lines[2]


def test_excludes_old_exceptions_beyond_window(tmp_path: Path) -> None:
    when = datetime(2026, 5, 9, 14, 30)
    exceptions = tmp_path / "exceptions"
    make_exception(exceptions, when=when - timedelta(days=1), repo="recent", user="drew")
    make_exception(exceptions, when=when - timedelta(days=45), repo="old", user="drew")

    body = render_exceptions_readme(tmp_path, now=when, days=30)
    assert "recent" in body
    assert "old" not in body


def test_skips_readme_md(tmp_path: Path) -> None:
    """Don't recurse the README into the list."""
    exceptions = tmp_path / "exceptions"
    exceptions.mkdir()
    (exceptions / "README.md").write_text("seed\n", encoding="utf-8")
    make_exception(
        exceptions,
        when=datetime(2026, 5, 9, 14, 30),
        repo="lib-a",
        user="drew",
    )

    body = render_exceptions_readme(tmp_path, now=datetime(2026, 5, 9, 15, 0))
    # The body should mention lib-a once — not the README itself.
    assert body.count("- 2026-05-09") == 1


def test_filename_with_counter_suffix_still_parses(tmp_path: Path) -> None:
    exceptions = tmp_path / "exceptions"
    when = datetime(2026, 5, 9, 14, 30)
    make_exception(exceptions, when=when, repo="lib-a", user="drew")
    make_exception(exceptions, when=when, repo="lib-a", user="drew", suffix="-2")

    body = render_exceptions_readme(tmp_path, now=when + timedelta(minutes=5))
    # Two list entries (each row contains the repo name in the inline backtick
    # and twice in the filename links).
    list_lines = [line for line in body.splitlines() if line.startswith("- ")]
    assert len(list_lines) == 2


def test_user_with_hyphenated_slug_renders_with_spaces(tmp_path: Path) -> None:
    exceptions = tmp_path / "exceptions"
    make_exception(
        exceptions,
        when=datetime(2026, 5, 9, 14, 30),
        repo="lib-a",
        user="drew-fiero",
    )

    body = render_exceptions_readme(tmp_path, now=datetime(2026, 5, 9, 15, 0))
    assert "(Drew Fiero)" in body


# ──────────────────────────────────────────────────────────────────────
#  count_exceptions_in_window
# ──────────────────────────────────────────────────────────────────────


def test_count_zero_when_empty(tmp_path: Path) -> None:
    assert count_exceptions_in_window(tmp_path, now=datetime(2026, 5, 9, 12, 0)) == 0


def test_count_only_includes_recent(tmp_path: Path) -> None:
    when = datetime(2026, 5, 9, 14, 30)
    exceptions = tmp_path / "exceptions"
    make_exception(exceptions, when=when - timedelta(days=1), repo="a", user="drew")
    make_exception(exceptions, when=when - timedelta(days=2), repo="b", user="tony")
    make_exception(exceptions, when=when - timedelta(days=10), repo="c", user="drew")

    assert count_exceptions_in_window(tmp_path, now=when, days=7) == 2
    assert count_exceptions_in_window(tmp_path, now=when, days=30) == 3
