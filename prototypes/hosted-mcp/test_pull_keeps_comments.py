"""A hosted pull must not truncate away the reviewer comments (#297).

`pull_project_source` caps the assembled text at `max_chars` (40k default).
Office reviewer comments are ingested as a `## Comments` block at the END of
the document, so the cap removed exactly the feedback a reader came for —
sap-5174's P&P report was 96k chars with 34 client comments in the tail.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture
def server(monkeypatch):
    for k, v in {
        "MC2_API_BASE": "http://example.invalid",
        "SUPABASE_URL": "http://example.invalid",
        "SUPABASE_ANON_KEY": "x",
    }.items():
        monkeypatch.setenv(k, v)
    import server as mod
    return mod


_BODY = ("Report body paragraph. " * 100).strip()
_COMMENTS = "## Comments\n\n1. **Fredericks, Fred** · 2026-09-09: Stop talking about the system!"


def test_short_text_is_untouched(server):
    text, truncated, kept = server._truncate_keeping_comments(_BODY, 10_000)
    assert (text, truncated, kept) == (_BODY, False, False)


def test_cut_before_the_comments_block_keeps_the_block(server):
    full = f"{_BODY}\n\n{_COMMENTS}"
    cap = 500
    text, truncated, kept = server._truncate_keeping_comments(full, cap)
    assert truncated and kept
    assert text.startswith(_BODY[:cap])
    assert "[… body truncated at 500 chars …]" in text
    assert text.endswith(_COMMENTS)
    assert text.count("## Comments") == 1


def test_cut_inside_the_comments_block_does_not_duplicate_it(server):
    full = f"{_BODY}\n\n{_COMMENTS}"
    cap = len(_BODY) + 20  # the block already starts inside the kept prefix
    text, truncated, kept = server._truncate_keeping_comments(full, cap)
    assert truncated and not kept
    assert text == full[:cap]


def test_no_comments_block_truncates_plainly(server):
    text, truncated, kept = server._truncate_keeping_comments(_BODY, 100)
    assert (text, truncated, kept) == (_BODY[:100], True, False)


def test_comment_count_parses_postgrest_text(server):
    assert server._comment_count({"comment_count": "34"}) == 34
    assert server._comment_count({"comment_count": "0"}) == 0
    assert server._comment_count({"comment_count": None}) == 0
    assert server._comment_count({}) == 0
    assert server._comment_count({"comment_count": "n/a"}) == 0
