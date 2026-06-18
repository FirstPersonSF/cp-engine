from cp_engine.spine import row_to_element, load_spine_from_mc2


def test_row_to_element_roundtrips_spine():
    row = {
        "element_id": "p/deliverable/d1", "project_code": "p",
        "layer": "Deliverables", "type": "positioning-narrative",
        "title": "D1", "stage": "revised", "target_date": "2026-06-17",
        "status": "active", "last_touched": "2026-06-13",
        "depends_on": ["p/deliverable/d0"], "serves": ["p/deliverable/d1"],
        "source": [], "target_history": [], "author": "drew",
        "rel_path": "1p/a/p/spine/Deliverables/d1.md",
    }
    el = row_to_element(row)
    assert el.id == "p/deliverable/d1"
    assert el.layer == "Deliverables"
    assert el.stage == "revised"
    assert el.depends_on == ("p/deliverable/d0",)
    assert el.serves == ("p/deliverable/d1",)


def _substance_client(rows, captured):
    """A fake supabase client that records the table name + every `.eq` filter
    and returns `rows` from execute(). Supports chained `.eq` (the live-version
    filter chains project_code + status)."""
    class _T:
        def __init__(self, name):
            captured.setdefault("table", name)
        def select(self, c):
            captured["select"] = c
            return self
        def eq(self, c, v):
            captured.setdefault("eqs", []).append((c, v))
            return self
        def execute(self):
            return type("R", (), {"data": rows})()
    class _C:
        def table(self, n):
            return _T(n)
    return _C()


def test_load_spine_from_mc2_reads_spine_substance():
    """Reads `spine_substance` (not the retired `spine_elements`), filtered to
    the project's live versions, mapping framing→title / body / version_date."""
    captured = {}
    rows = [
        # A context row (migrated legacy element): layer + framing + body.
        {"id": "p/_context/brief", "project_code": "p", "est_item_id": None,
         "layer": "Brief", "placement": "context", "status": "live",
         "version_label": "v1", "version_date": "2026-06-13",
         "framing": "Project brief", "body": "the brief text",
         "serves": [], "sources": [], "rel_path": "1p/a/p/spine/_context/brief.md"},
        # An item row, live version.
        {"id": "p/est-1/d1", "project_code": "p", "est_item_id": "est-1",
         "layer": "Deliverables", "placement": "item", "status": "live",
         "version_label": "v2", "version_date": "2026-06-16",
         "framing": "Positioning narrative", "body": "v2 body",
         "serves": ["est-1"], "sources": [], "rel_path": "1p/a/p/spine/phase/d1.md"},
    ]
    client = _substance_client(rows, captured)
    els = load_spine_from_mc2(client, "p")

    assert captured["table"] == "spine_substance"
    assert "*" not in captured["select"]  # explicit columns only
    # Filters both project_code and live status (one entry per element).
    assert ("project_code", "p") in captured["eqs"]
    assert ("status", "live") in captured["eqs"]

    by_id = {e.id: e for e in els}
    assert set(by_id) == {"p/_context/brief", "p/est-1/d1"}
    brief = by_id["p/_context/brief"]
    assert brief.layer == "Brief"
    assert brief.title == "Project brief"   # framing → title
    assert brief.body == "the brief text"
    assert brief.last_touched == "2026-06-13"  # version_date → last_touched
    d1 = by_id["p/est-1/d1"]
    assert d1.title == "Positioning narrative"
    assert d1.body == "v2 body"
    assert d1.status == "active"            # live → active (Lens vocabulary)
    assert d1.serves == ("est-1",)


def test_load_spine_from_mc2_excludes_superseded_versions():
    """The live-status filter is the dedupe: an item's old (superseded) version
    is filtered out by the query, leaving one entry per element."""
    captured = {}
    # Only the live version comes back (the query's `.eq(status, live)` excludes
    # the superseded one); the helper must not re-introduce it.
    rows = [
        {"id": "p/est-1/d1", "project_code": "p", "est_item_id": "est-1",
         "layer": "Deliverables", "placement": "item", "status": "live",
         "version_label": "v3", "version_date": "2026-06-17",
         "framing": "D1 latest", "body": "v3", "serves": [], "sources": [],
         "rel_path": "x"},
    ]
    els = load_spine_from_mc2(_substance_client(rows, captured), "p")
    assert ("status", "live") in captured["eqs"]
    assert len(els) == 1 and els[0].title == "D1 latest"


def test_substance_row_to_element_defaults_layer_when_null():
    """A native item row with no explicit layer defaults to Deliverables."""
    from cp_engine.spine import substance_row_to_element
    el = substance_row_to_element({
        "id": "p/est-9/x", "project_code": "p", "layer": None,
        "status": "live", "framing": "Untitled item", "body": "",
        "version_date": "2026-06-16", "serves": [], "sources": [],
    })
    assert el.layer == "Deliverables"
    assert el.title == "Untitled item"
    assert el.status == "active"


def test_row_to_element_surfaces_verification_state():
    from cp_engine.spine import row_to_element
    el = row_to_element({
        "element_id": "p/deliverable/x", "layer": "Deliverables",
        "field_states": {"status": "confirmed"},
        "review_flags": [{"field": "status", "was": "active", "now": "dormant", "at": "2026-06-20T00:00:00Z"}],
        "confirmed_by": "drew", "confirmed_at": "2026-06-20T00:00:00Z",
    })
    assert el.field_states == {"status": "confirmed"}
    assert el.review_flags[0]["now"] == "dormant"
    assert el.confirmed_by == "drew"
    assert el.confirmed_at == "2026-06-20T00:00:00Z"


def test_row_to_element_defaults_empty_verification():
    from cp_engine.spine import row_to_element
    el = row_to_element({"element_id": "p/x", "layer": "Brief"})
    assert el.field_states == {}
    assert el.review_flags == ()
    assert el.confirmed_by is None


def test_element_to_row_omits_verification_columns(tmp_path):
    from cp_engine.spine import SpineElement, element_to_row
    el = SpineElement(id="p/x", project="p", layer="Brief", title="T",
                      status="active", last_touched="2026-06-13",
                      path=tmp_path / "x.md", body="")
    row = element_to_row(el, project_id="u1", project_root=tmp_path)
    for k in ("field_states", "review_flags", "confirmed_by", "confirmed_at"):
        assert k not in row   # mirror reconcile owns these, not the pure mapper
