def test_authored_rows_render_to_parseable_file(tmp_path):
    rows = [
        {"est_item_id": "_authored/hyp", "est_item_kind": None, "phase": None,
         "binding": "unbound", "layer": "synthesis", "placement": "context",
         "serves": ["wi-1"], "version_label": "v2", "version_date": "2026-06-19",
         "status": "live", "framing": "latest hypothesis", "body": "new body", "sources": [],
         "origin": "authored"},
        {"est_item_id": "_authored/hyp", "est_item_kind": None, "phase": None,
         "binding": "unbound", "layer": "synthesis", "placement": "context",
         "serves": ["wi-1"], "version_label": "v1", "version_date": "2026-06-18",
         "status": "superseded", "framing": "latest hypothesis", "body": "old body", "sources": [],
         "origin": "authored"},
    ]
    from cp_engine.authored_mirror import write_authored_element
    path = write_authored_element(tmp_path, project_code="p", est_item_id="_authored/hyp", rows=rows)
    # The file lives under spine/_authored/
    assert path.name == "hyp.md"
    assert path.parent.name == "_authored"
    # It round-trips through parse_substance
    from cp_engine.substance import parse_substance, render_substance
    item = parse_substance(path)            # must NOT raise
    assert len(item.versions) == 2
    assert item.placement == "context"
    assert item.serves == ("wi-1",)
    # Idempotent: re-rendering the parsed item equals the file content
    assert render_substance(item) == path.read_text()


def test_authored_single_version(tmp_path):
    rows = [{"est_item_id": "_authored/email-janet", "est_item_kind": None, "phase": None,
             "binding": "unbound", "layer": "email", "placement": "context", "serves": [],
             "version_label": "v1", "version_date": "2026-06-19", "status": "live",
             "framing": "Email from Janet", "body": "Hi team", "sources": [], "origin": "authored"}]
    from cp_engine.authored_mirror import write_authored_element
    path = write_authored_element(tmp_path, project_code="p", est_item_id="_authored/email-janet", rows=rows)
    from cp_engine.substance import parse_substance
    item = parse_substance(path)
    assert item.est_item_id == "_authored/email-janet"
    assert len(item.versions) == 1 and item.versions[0].status == "live"
