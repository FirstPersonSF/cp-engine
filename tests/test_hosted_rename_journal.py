# tests/test_hosted_rename_journal.py — issue #165: renaming a spine element
# leaves an audit trail.
#
# `set_spine_element` could already retitle (it takes `framing`), but wrote no
# journal entry, so a rename destroyed its own evidence: nothing on the row
# remembers the old title once the UPDATE lands, and "why doesn't this slug
# match its title?" becomes unanswerable.
#
# Deliberately NOT tested here: slug rewriting. est_item_id is the lineage key
# that nine columns across seven tables join on without FKs (mig 117), so it
# stays frozen — see _journal_rename's docstring.
#
# Same harness as test_hosted_commitments_batch: the decorated verbs are MCP
# Tool objects, so this targets the module-level helper the verb is built from.
import importlib.util
import os
from pathlib import Path

import pytest

pytest.importorskip("jwt")
pytest.importorskip("mcp")
pytest.importorskip("supabase")

_SERVER_PATH = (
    Path(__file__).resolve().parents[1] / "prototypes" / "hosted-mcp" / "server.py"
)


@pytest.fixture(scope="module")
def srv():
    os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
    os.environ.setdefault("SUPABASE_ANON_KEY", "anon-key-for-tests")
    spec = importlib.util.spec_from_file_location("hosted_mcp_server", _SERVER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCOPE = {"id": "proj-1", "kind": "project"}


def test_records_both_halves_of_the_transition(srv, monkeypatch):
    """The old title is the half that is otherwise unrecoverable."""
    seen = {}

    def fake_upsert(client, project_id, est_item_id, title, step_date):
        seen.update(project_id=project_id, est_item_id=est_item_id, title=title)
        return {"id": "step-1", "title": title}

    monkeypatch.setattr(srv, "upsert_auto_step", fake_upsert)
    out = srv._journal_rename(
        None, SCOPE, "_authored/rep-enablement-deck",
        "Rep-enablement deck", "SRS field deck r4 package",
    )
    assert out["journaled"] is True
    assert out["prior_framing"] == "Rep-enablement deck"
    assert "Rep-enablement deck" in seen["title"]
    assert "SRS field deck r4 package" in seen["title"]
    assert seen["est_item_id"] == "_authored/rep-enablement-deck"
    assert seen["project_id"] == "proj-1"


def test_no_framing_argument_is_not_a_rename(srv, monkeypatch):
    monkeypatch.setattr(srv, "upsert_auto_step", lambda *a, **k: pytest.fail("called"))
    out = srv._journal_rename(None, SCOPE, "_authored/x", "Same title", None)
    assert out == {"journaled": False, "skipped": "no framing change"}


def test_setting_the_same_title_is_not_a_rename(srv, monkeypatch):
    """A no-op write must not accrete journal noise."""
    monkeypatch.setattr(srv, "upsert_auto_step", lambda *a, **k: pytest.fail("called"))
    out = srv._journal_rename(None, SCOPE, "_authored/x", "Same title", "Same title")
    assert out["journaled"] is False
    # Whitespace-only difference is still the same title.
    assert srv._journal_rename(
        None, SCOPE, "_authored/x", "Same title", "  Same title  "
    )["journaled"] is False


def test_first_title_on_an_untitled_card_is_a_fill_in_not_a_rename(srv, monkeypatch):
    """Nothing was lost, so there is nothing to preserve."""
    monkeypatch.setattr(srv, "upsert_auto_step", lambda *a, **k: pytest.fail("called"))
    out = srv._journal_rename(None, SCOPE, "_authored/x", "", "A real title")
    assert out == {"journaled": False, "skipped": "element had no prior title"}


def test_a_journal_failure_never_raises(srv, monkeypatch):
    """The rename has already committed by the time this runs — reporting the
    miss is the contract, propagating it is not."""

    def boom(*a, **k):
        raise RuntimeError("steps table unreachable")

    monkeypatch.setattr(srv, "upsert_auto_step", boom)
    out = srv._journal_rename(None, SCOPE, "_authored/x", "Old", "New")
    assert out["journaled"] is False
    assert "RuntimeError" in out["error"]
    assert "steps table unreachable" in out["error"]


def test_the_slug_is_never_part_of_the_rename(srv, monkeypatch):
    """#165 as filed proposed slug aliasing; the schema says otherwise. The
    helper takes est_item_id as an INPUT and never returns a new one."""
    monkeypatch.setattr(
        srv, "upsert_auto_step", lambda *a, **k: {"id": "s"},
    )
    out = srv._journal_rename(
        None, SCOPE, "_authored/original-slug", "Old name", "Completely New Name",
    )
    assert "est_item_id" not in out
    assert "alias" not in out
