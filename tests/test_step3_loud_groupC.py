"""Architecture plan step 3 — fail loudly, group C (MC-2 / sources / ingest /
spine IO).

Each test pins ONE swallow that used to give its caller no signal: the
failure must now arrive in what the caller gets (a returned field, an
out-list, a raise). Every test here was run against the pre-step-3 modules
and FAILED there (see the step-3 report for the control runs).
"""

from __future__ import annotations

import sys
import types
from datetime import date
from pathlib import Path

import pytest

# ── shared fakes ─────────────────────────────────────────────────────────────


class _Boom(Exception):
    pass


class _Query:
    """Chainable PostgREST-ish query; `execute` raises when the table is in
    `fail`, else returns the seeded rows."""

    def __init__(self, name, rows, fail):
        self.name, self.rows, self.fail = name, rows, fail

    def __getattr__(self, _attr):
        return lambda *a, **k: self

    def execute(self):
        if self.name in self.fail:
            raise _Boom(f"{self.name} unreadable")
        return types.SimpleNamespace(data=list(self.rows.get(self.name, [])))


class _Client:
    def __init__(self, rows=None, fail=()):
        self.rows, self.fail = rows or {}, set(fail)

    def table(self, name):
        return _Query(name, self.rows, self.fail)


# ── asset_ingest ─────────────────────────────────────────────────────────────


def test_dedup_precheck_failure_is_collected_not_only_printed():
    from cp_engine import asset_ingest as ai

    errs: list[str] = []
    out = ai._existing_dup_at_other_path(
        _Client(fail={"rag_assets"}), "p1", "hash", "/tmp/x", errors=errs
    )
    assert out is False  # still fails open
    assert errs and "rag_assets unreadable" in errs[0]


def test_ingest_cache_skip_check_failure_is_collected():
    from cp_engine import asset_ingest as ai

    ref = types.SimpleNamespace(change_token="tok", source="drive", id="f1")
    errs: list[str] = []
    assert ai._unchanged_since_last_ingest(
        _Client(fail={"rag_assets"}), "p1", ref, errors=errs
    ) is False
    assert errs and errs[0].startswith("f1:")


def test_existing_titles_read_failure_is_marked_on_the_empty_set():
    from cp_engine import asset_ingest as ai

    titles = ai._existing_source_titles(_Client(fail={"rag_assets"}), "project_id", "p1")
    assert titles == set()
    assert "rag_assets unreadable" in (getattr(titles, "error", None) or "")


def test_source_link_lookup_failure_lands_in_failures():
    from cp_engine import asset_ingest as ai

    class _Dbx:
        def get_shareable_link(self, path, team_only=False):
            raise RuntimeError("refusing to mint a public link")

    ref = ai.FileRef(source="dropbox", id="d1", name="deck.pptx",
                     mime_type=None, size=None, modified=None, path="/a/deck.pptx")
    failures: list = []
    assert ai._source_url(ref, None, _Dbx(), failures=failures) is None
    assert failures and failures[0][0] == "deck.pptx"
    assert "refusing to mint" in failures[0][1]


# ── ingest.execute_plan ──────────────────────────────────────────────────────


def test_commitment_items_dropped_without_a_client_are_reported(tmp_path):
    from cp_engine.ingest import execute_plan

    sprint = tmp_path / "sprints" / "2026-W40" / "ggl-5168.md"
    sprint.parent.mkdir(parents=True)
    sprint.write_text("# ggl-5168\n")
    plan = {"projects": {"ggl-5168": {"set-milestone": [
        {"deliverable": "Deck", "date": "2026-10-01", "owner": "Drew",
         "confidence": "high"},
    ]}}}
    result = execute_plan(plan, tenant_root=tmp_path, today=date(2026, 9, 30),
                          week_iso="2026-W40", supabase=None)
    assert result.errors == []  # a file-only ingest still succeeds…
    assert any("NOT written" in w for w in result.warnings)  # …but says so
    assert "warnings" in result.to_dict()


# ── plan_from_transcript ─────────────────────────────────────────────────────


def test_attribution_pass_failure_is_not_an_empty_clean_result(monkeypatch):
    import cp_engine.attribution as attribution
    from cp_engine.plan_from_transcript import apply_attribution_checks

    def boom(*a, **k):
        raise RuntimeError("roster exploded")

    monkeypatch.setattr(attribution, "check_plan_attribution", boom)
    out = apply_attribution_checks(
        {"projects": {}}, config=types.SimpleNamespace(root=None), transcript=""
    )
    assert out != {}
    assert "roster exploded" in out["error"]


def test_action_item_routing_failure_rides_the_stats(monkeypatch):
    import cp_engine.ingest_routing as routing
    from cp_engine import plan_from_transcript as pft

    def boom(*a, **k):
        raise RuntimeError("router down")

    monkeypatch.setattr(routing, "route_action_items", boom)
    plan: dict = {"projects": {}}
    stats = pft._route_action_items(
        [{"text": "send deck"}], plan=plan, project_code="ggl-5168",
        co_tagged=[], roster=[], config=types.SimpleNamespace(root=None),
    )
    assert stats["kept"] == 1
    assert "router down" in stats["error"]


# ── project_sources: comments are never a silent zero (#298 shape) ───────────


def _comments_client():
    return _Client(rows={"rag_assets": [{
        "id": "a1", "title": "Brief.docx", "source_provider": "dropbox",
        "source_file_id": "f1", "source_path": "/x/Brief.docx",
    }]})


def test_unreadable_office_comments_are_an_error_not_zero(monkeypatch, tmp_path):
    from cp_engine import project_sources as ps

    def fake_download(file_ref, dest):
        p = Path(dest) / "Brief.docx"
        p.write_bytes(b"not a zip at all")
        return p

    monkeypatch.setattr(ps, "download_file", fake_download)
    out = ps.pull_document_comments(_comments_client(), "p1", "Brief.docx", tmp_path)
    assert "error" in out, out
    assert "comment_count" not in out


def test_drive_comment_read_failure_is_an_error_not_zero(monkeypatch, tmp_path):
    from cp_engine import project_sources as ps

    class _Conn:
        def __init__(self, *a, **k):
            pass

        def _authenticate(self):
            raise PermissionError("drive auth revoked")

    mod = types.ModuleType("cloud_storage.google_drive_connector")
    mod.GoogleDriveConnector = _Conn
    monkeypatch.setitem(sys.modules, "cloud_storage.google_drive_connector", mod)
    client = _Client(rows={"rag_assets": [{
        "id": "a1", "title": "Brief", "source_provider": "drive",
        "source_file_id": "g1", "source_path": None,
    }]})
    out = ps.pull_document_comments(client, "p1", "Brief", tmp_path)
    assert "error" in out and "drive auth revoked" in out["error"], out


def test_list_spine_reports_an_unreadable_done_map(monkeypatch):
    from cp_engine import project_sources as ps

    row = {"est_item_id": "_authored/brief", "framing": "Brief", "layer": "Inputs",
           "binding": "live", "status": "live", "serves": [], "body": "x",
           "project_id": "p1", "version_label": "v1", "archived": False}
    monkeypatch.setattr(ps, "fetch_project_done_map",
                        lambda *a, **k: (_ for _ in ()).throw(_Boom("no estimator")))
    warnings: list[str] = []
    out = ps.list_spine(_Client(rows={"spine_substance": [row]}), "p1",
                        warnings=warnings)
    assert out and out[0]["done"] is None
    assert warnings and "no estimator" in warnings[0]


# ── spine_inbox: never pick a version label from a partial read ──────────────


def test_element_rows_read_failure_raises():
    # Step 4c: promote reads the element's versions from MC-2 alone (the
    # disk file is a rendered view), so a failed read must raise, not
    # degrade to "no versions" and re-mint v1 over an existing version.
    from cp_engine.spine_inbox import _element_rows

    with pytest.raises(RuntimeError, match="refusing to pick a version label"):
        _element_rows(_Client(fail={"spine_substance"}), "p1", "_authored/x")


# ── dates_loop / propose passes / tag resolve ────────────────────────────────


def test_partners_channel_lookup_failure_reaches_result_errors():
    from cp_engine.dates_loop import _partners_channel

    errors: list[str] = []
    assert _partners_channel(_Client(fail={"app_config"}), errors) is None
    assert errors and "rollup NOT posted" in errors[0]


def test_tag_resolve_says_when_the_index_was_unreadable():
    from cp_engine.tag_resolve import resolve_tags

    errors: list[str] = []
    out = resolve_tags(_Client(fail={"projects"}), ["ggl-5168"], errors=errors)
    assert out[0]["matched"] is False
    assert errors and "parse-only" in errors[0]


# ── mc2_db / commitments ─────────────────────────────────────────────────────


def test_sprint_identity_read_failure_raises_instead_of_not_found(monkeypatch):
    from cp_engine import mc2_db

    monkeypatch.setattr(mc2_db, "_resolve_project_id", lambda c, code: "p1")
    with pytest.raises(_Boom):
        mc2_db.project_sprint_identity(_Client(fail={"projects"}), "ggl-5168")
    # Not found is still None.
    assert mc2_db.project_sprint_identity(_Client(), "ggl-5168") is None


def test_canonical_spine_code_fallback_is_reported():
    from cp_engine.mc2_db import canonical_spine_code

    warnings: list[str] = []
    code = canonical_spine_code(
        _Client(fail={"spine_substance", "projects"}), "p1", "ggl-5168",
        warnings=warnings,
    )
    assert code == "ggl-5168"
    assert any("fell back" in w for w in warnings)


def test_owner_roster_failure_is_reported_by_write_commitment(monkeypatch):
    from cp_engine import commitments

    monkeypatch.setattr(commitments, "_PEOPLE_CACHE", None)
    client = _Client(rows={"commitments": []}, fail={"entities"})
    warnings: list[str] = []
    out = commitments.write_commitment(
        client, owner={"id": "p1", "code": "ggl-5168"}, description="d",
        cp_hash="h1", source_kind="meeting_ingest", owner_name="drew",
        warnings=warnings,
    )
    assert out == "inserted"
    assert warnings and "roster unreadable" in warnings[0]
