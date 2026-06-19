from cp_engine.authored_element import (
    authored_est_item_id, build_create_rows, build_version_rows, slugify,
)


def test_slugify_makes_a_safe_slug():
    assert slugify("Email from Janet — 6/19!") == "email-from-janet-6-19"
    assert slugify("") == "untitled"


def test_authored_est_item_id_is_namespaced():
    assert authored_est_item_id("email-from-janet") == "_authored/email-from-janet"


def test_build_create_rows_unbound_email():
    rows = build_create_rows(
        project_id="pid", project_code="ibx-5192", label="Email from Janet",
        type_="email", body="Hi team\n\n- point one", serves=[], now_iso="2026-06-19T00:00:00+00:00",
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == "ibx-5192/_authored/email-from-janet/v1"
    assert r["est_item_id"] == "_authored/email-from-janet"
    assert r["placement"] == "context"
    assert r["layer"] == "email"          # type -> layer for authored context elements
    assert r["origin"] == "authored"
    assert r["status"] == "live"
    assert r["version_label"] == "v1"
    assert r["binding"] == "unbound"        # serves nothing
    assert r["serves"] == []
    assert r["body"] == "Hi team\n\n- point one"
    assert r["project_id"] == "pid"


def test_build_create_rows_bound_to_workitem():
    rows = build_create_rows(
        project_id="pid", project_code="ibx-5192", label="latest hypothesis",
        type_="synthesis", body="we think...", serves=["wi-1"], now_iso="2026-06-19T00:00:00+00:00",
    )
    r = rows[0]
    assert r["serves"] == ["wi-1"]
    assert r["binding"] == "live"           # serves a work-item


def test_build_version_rows_returns_new_live():
    """build_version_rows returns ONLY the new live v2 row — demoting the prior
    live row is the caller's job (a targeted status UPDATE), so no superseded
    row appears in the output."""
    prior = [
        {"id": "ibx-5192/_authored/hyp/v1", "version_label": "v1", "status": "live",
         "body": "old", "version_date": "2026-06-18", "framing": "hyp", "layer": "synthesis",
         "serves": ["wi-1"]},
    ]
    rows = build_version_rows(
        project_id="pid", project_code="ibx-5192",
        est_item_id="_authored/hyp", prior_versions=prior,
        body="new", version_note="sharpened the wedge", now_iso="2026-06-19T00:00:00+00:00",
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["version_label"] == "v2"
    assert r["status"] == "live"
    assert r["body"] == "new"
    assert r["version_note"] == "sharpened the wedge"
    assert r["id"] == "ibx-5192/_authored/hyp/v2"


def test_build_version_rows_tolerates_missing_serves():
    """A prior row with no `serves` key must not crash (None -> [])."""
    prior = [
        {"version_label": "v1", "status": "live", "body": "old",
         "version_date": "2026-06-18", "framing": "hyp", "layer": "note"},
        # ^ deliberately NO `serves` key
    ]
    rows = build_version_rows(
        project_id="pid", project_code="p", est_item_id="_authored/hyp",
        prior_versions=prior, body="new", version_note="n",
        now_iso="2026-06-19T00:00:00+00:00",
    )
    assert len(rows) == 1
    assert rows[0]["version_label"] == "v2"
    assert rows[0]["serves"] == []   # new live; prior had no serves key -> []


def test_build_create_rows_golden_vector():
    """Parity guard: both repos' copies must produce this exact row for this input.
    If you change the builder, update BOTH copies and this golden vector in BOTH."""
    rows = build_create_rows(project_id="P", project_code="c", label="My Label",
                             type_="note", body="b", serves=[], now_iso="2026-01-02T00:00:00+00:00")
    assert rows == [{
        "id": "c/_authored/my-label/v1", "project_id": "P", "project_code": "c",
        "est_item_id": "_authored/my-label", "est_item_kind": None, "phase": None,
        "binding": "unbound", "layer": "note", "placement": "context", "serves": [],
        "version_label": "v1", "version_date": "2026-01-02", "status": "live",
        "framing": "My Label", "body": "b", "sources": [], "origin": "authored",
        "version_note": None, "rel_path": None,
    }]
