# tests/test_authored_element_importance.py
from cp_engine.authored_element import build_create_rows, build_version_rows


def test_create_defaults_important_false_note_none():
    rows = build_create_rows(
        project_id="pid", project_code="p", label="Fred lens",
        type_="note", body="b", serves=[], now_iso="2026-06-25T00:00:00+00:00",
    )
    assert rows[0]["important"] is False
    assert rows[0]["note"] is None


def test_create_accepts_important_and_note():
    rows = build_create_rows(
        project_id="pid", project_code="p", label="Fred lens",
        type_="note", body="b", serves=[], now_iso="2026-06-25T00:00:00+00:00",
        important=True, note="the fork in the engagement",
    )
    assert rows[0]["important"] is True
    assert rows[0]["note"] == "the fork in the engagement"


def test_version_carries_forward_important_and_note_from_prior():
    prior = [{
        "version_label": "v1", "framing": "Fred lens", "layer": "Note",
        "serves": [], "important": True, "note": "the fork",
    }]
    rows = build_version_rows(
        project_id="pid", project_code="p", est_item_id="_authored/fred-lens",
        prior_versions=prior, body="v2 body", version_note="sharpened",
        now_iso="2026-06-26T00:00:00+00:00",
    )
    assert rows[0]["version_label"] == "v2"
    assert rows[0]["important"] is True
    assert rows[0]["note"] == "the fork"


def test_version_from_pre_migration_prior_defaults_safely():
    prior = [{"version_label": "v1", "framing": "X", "layer": "Note", "serves": []}]
    rows = build_version_rows(project_id="pid", project_code="p",
        est_item_id="_authored/x", prior_versions=prior, body="b",
        version_note="n", now_iso="2026-06-26T00:00:00+00:00")
    assert rows[0]["important"] is False
    assert rows[0]["note"] is None
