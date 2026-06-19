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


def _row(label, status):
    return {"est_item_id": "_authored/hyp", "est_item_kind": None, "phase": None,
            "binding": "unbound", "layer": "synthesis", "placement": "context",
            "serves": [], "version_label": label, "version_date": "2026-06-19",
            "status": status, "framing": "f", "body": "b", "sources": [],
            "origin": "authored"}


def test_write_authored_raises_on_zero_live(tmp_path):
    """A malformed DB state with no live version must fail loud (caught by
    sync's best-effort wrapper) instead of writing a corrupt mirror."""
    import pytest
    from cp_engine.authored_mirror import write_authored_element
    rows = [_row("v2", "superseded"), _row("v1", "superseded")]
    with pytest.raises(ValueError, match="0 live"):
        write_authored_element(tmp_path, project_code="p",
                               est_item_id="_authored/hyp", rows=rows)
    assert not (tmp_path / "spine" / "_authored" / "hyp.md").exists()


def test_write_authored_raises_on_two_live(tmp_path):
    import pytest
    from cp_engine.authored_mirror import write_authored_element
    rows = [_row("v2", "live"), _row("v1", "live")]
    with pytest.raises(ValueError, match="2 live"):
        write_authored_element(tmp_path, project_code="p",
                               est_item_id="_authored/hyp", rows=rows)
