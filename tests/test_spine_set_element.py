from cp_engine.authored_element import build_version_rows


def test_version_retains_important_and_note_from_prior_row():
    """The bug Task 3 fixes: a prior row carrying important/note must produce a
    new live version that RETAINS them. _SEL selecting these columns is what
    makes the prior dict carry the keys this asserts on."""
    prior = [{
        "version_label": "v1", "framing": "Fred lens", "layer": "Note",
        "serves": [], "important": True, "note": "the fork",
    }]
    rows = build_version_rows(
        project_id="pid", project_code="p", est_item_id="_authored/fred-lens",
        prior_versions=prior, body="v2 body", version_note="sharpened",
        now_iso="2026-06-26T00:00:00+00:00",
    )
    live = next(r for r in rows if r["status"] == "live")
    assert live["important"] is True
    assert live["note"] == "the fork"
