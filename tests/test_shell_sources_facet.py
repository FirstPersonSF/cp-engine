"""Tests for the 'Source documents' facet of `cp shell`.

`fetch_project_assets` resolves a project's rag_assets via shell_elements,
`render_source_documents` renders them with linked-by element suffixes.
"""
from pathlib import Path

from cp_engine.shell import ShellElement, render_source_documents
from cp_engine.shell_sources import fetch_project_assets


# --- fake supabase client (two-table) ------------------------------------

def _fake_client(*, shell_rows, asset_rows, raise_on=None):
    """Build a fake client mirroring the chained-builder style.

    `shell_rows` answers the shell_elements project_id resolve; `asset_rows`
    answers the rag_assets query. `raise_on` (a table name) makes that table
    raise on .execute() to exercise the best-effort guard.
    """
    class _Q:
        def __init__(self, name):
            self._name = name
            self._rows = shell_rows if name == "shell_elements" else asset_rows

        def select(self, c):
            return self

        def eq(self, c, v):
            return self

        def limit(self, n):
            return self

        def execute(self):
            if raise_on == self._name:
                raise RuntimeError("boom")
            return type("R", (), {"data": self._rows})()

    class _C:
        def table(self, name):
            return _Q(name)

    return _C()


def test_fetch_resolves_project_id_then_queries_assets():
    client = _fake_client(
        shell_rows=[{"project_id": "uuid-1"}],
        asset_rows=[
            {"id": "a1", "title": "brief.pdf", "source_type": "pdf", "scope": "project"},
            {"id": "a2", "title": "logo.png", "source_type": "png", "scope": "account"},
        ],
    )
    out = fetch_project_assets(client, "ibx-5153")
    assert [a["id"] for a in out] == ["a1", "a2"]
    assert out[0]["title"] == "brief.pdf"


def test_fetch_returns_empty_when_no_shell_row():
    client = _fake_client(shell_rows=[], asset_rows=[{"id": "a1"}])
    assert fetch_project_assets(client, "ibx-5153") == []


def test_fetch_returns_empty_on_client_error():
    client = _fake_client(
        shell_rows=[{"project_id": "uuid-1"}],
        asset_rows=[{"id": "a1"}],
        raise_on="rag_assets",
    )
    assert fetch_project_assets(client, "ibx-5153") == []


# --- render --------------------------------------------------------------

def _el(title, source=()):
    return ShellElement(
        id=f"ibx-5153/brief/{title}",
        project="ibx-5153",
        layer="Brief",
        title=title,
        status="active",
        last_touched="2026-06-13",
        path=Path("x"),
        body="",
        source=source,
    )


def test_render_empty_assets_returns_empty_string():
    assert render_source_documents([], ()) == ""


def test_render_shows_linked_by_suffix():
    assets = [
        {"id": "a1", "title": "brief.pdf", "source_type": "pdf", "scope": "project"},
    ]
    el = _el("April brief", source=({"type": "rag_asset", "id": "a1", "title": "brief.pdf"},))
    out = render_source_documents(assets, (el,))
    assert "Source documents (1)" in out
    assert "brief.pdf" in out
    assert "·pdf" in out
    assert "[project]" in out
    assert "← April brief" in out


def test_render_links_live_from_plain_string_source_ref():
    # The real-world case: an element's source is a plain-string file ref (NOT a
    # pre-typed dict). The facet must match it to the asset LIVE by basename and
    # show the linked-by suffix — frontmatter is never mutated.
    assets = [
        {"id": "a1", "title": "client_input_brief_distilled.md",
         "source_type": "doc", "scope": "project"},
    ]
    el = _el("Client input brief (distilled)",
             source=("synthesis-docs/client_input_brief_distilled.md",))
    out = render_source_documents(assets, (el,))
    assert "← Client input brief (distilled)" in out


def test_render_linked_assets_sort_first():
    assets = [
        {"id": "a1", "title": "aaa.txt", "source_type": "txt", "scope": "project"},
        {"id": "a2", "title": "zzz.pdf", "source_type": "pdf", "scope": "project"},
    ]
    # only the second asset is linked
    el = _el("Distilled", source=({"type": "rag_asset", "id": "a2", "title": "zzz.pdf"},))
    out = render_source_documents(assets, (el,))
    assert out.index("zzz.pdf") < out.index("aaa.txt")


def test_render_caps_long_lists_with_true_total_in_header():
    assets = [
        {"id": f"a{i}", "title": f"doc-{i:03d}.pdf", "source_type": "pdf", "scope": "project"}
        for i in range(40)
    ]
    out = render_source_documents(assets, ())
    # Header carries the TRUE total, not the capped count.
    assert "Source documents (40)" in out
    assert "…and 15 more" in out
    # The capped body shows 25 bullets.
    assert out.count("\n  • ") == 25
