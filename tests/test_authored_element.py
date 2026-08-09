from cp_engine.authored_element import (
    authored_est_item_id, build_create_rows, build_version_rows, canon_layer,
    slugify,
)


def test_canon_layer_maps_lowercase_mcp_vocab_to_titlecase():
    """The MCP tool documents a LOWERCASE type vocab; the spine UI (the source of
    truth, ELEMENT_TYPES) stores TitleCase. canon_layer converges them so a kind
    authored either way lands under ONE layer string the UI filters can match."""
    assert canon_layer("email") == "Email"
    assert canon_layer("note") == "Note"
    assert canon_layer("decision") == "Decisions"
    assert canon_layer("source") == "Source material"
    assert canon_layer("brief") == "Brief"
    assert canon_layer("stakeholder") == "Stakeholders"
    assert canon_layer("agreement") == "Agreement"
    assert canon_layer("synthesis") == "Synthesis"
    # #172: `output` folds into Deliverables. It used to map to a layer of its
    # own, but "Output" is not in cp_engine.spine.LAYERS, so the alias minted a
    # layer no reader recognised — spine_stats saw 2 of ibx-5192's 6
    # deliverables and 0 of sap-5174's 2.
    assert canon_layer("output") == "Deliverables"


def test_canon_layer_collapses_spelling_and_case_variants():
    """Case + whitespace are normalized before lookup, so the UI's TitleCase
    forms are idempotent and the SourceMaterial/Source material split collapses."""
    assert canon_layer("Email") == "Email"
    assert canon_layer("Source material") == "Source material"
    assert canon_layer("SourceMaterial") == "Source material"
    assert canon_layer("DECISION") == "Decisions"


def test_canon_layer_passes_unknown_through_unchanged():
    """An unmapped value (a future/custom kind) is returned as-is, not mangled —
    canon only converges KNOWN aliases, it never invents or drops a layer."""
    assert canon_layer("Retrospective") == "Retrospective"  # canonical = idempotent
    assert canon_layer("Moodboard") == "Moodboard"          # genuinely unknown


def test_canon_layer_new_aliases_converge():
    """The 2026-07-03 additions: layers the UI already renders (LAYER_ORDER)
    but the write vocab was missing — incl. the exact divergence found live
    (`ClientFeedback` vs the UI's `Client feedback`)."""
    assert canon_layer("retrospective") == "Retrospective"
    assert canon_layer("research") == "Research"
    assert canon_layer("deliverable") == "Deliverables"
    assert canon_layer("ClientFeedback") == "Client feedback"
    assert canon_layer("client feedback") == "Client feedback"
    assert canon_layer("timeline") == "Timeline"


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
    assert r["layer"] == "Email"          # type -> canonical layer (canon_layer)
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
        "binding": "unbound", "layer": "Note", "placement": "context", "serves": [],
        "version_label": "v1", "version_date": "2026-01-02", "status": "live",
        "framing": "My Label", "body": "b", "sources": [], "origin": "authored",
        "version_note": None, "rel_path": None,
        "important": False, "note": None,
    }]


def test_build_create_rows_carries_sources():
    """A passed `sources` list lands on the row (parity with mc-2's copy, where
    document routing records a structured rag_asset source at create time)."""
    rows = build_create_rows(
        project_id="P", project_code="c", label="Doc", type_="Source material",
        body="b", serves=[], now_iso="2026-01-02T00:00:00+00:00",
        sources=[{"type": "rag_asset", "id": "a1", "title": "Doc"}],
    )
    assert rows[0]["sources"] == [{"type": "rag_asset", "id": "a1", "title": "Doc"}]


def test_build_create_rows_defaults_sources_to_empty():
    rows = build_create_rows(
        project_id="P", project_code="c", label="Doc", type_="note",
        body="b", serves=[], now_iso="2026-01-02T00:00:00+00:00",
    )
    assert rows[0]["sources"] == []
