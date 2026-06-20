from pathlib import Path

from cp_engine.spine_recover import (
    RecoveryAction,
    classify_element,
    load_legacy_elements,
    match_source_asset,
    plan_element,
    recover,
    recovered_rows,
    redistill_body,
    source_basename,
)


class _El:  # minimal stand-in for SpineElement (plan_element reads only these)
    def __init__(self, layer, title, body, source=(), serves=()):
        self.layer, self.title, self.body = layer, title, body
        self.source, self.serves = source, serves


def test_plan_source_backed_element_marks_redistill():
    el = _El("SourceMaterial", "Infoblox Platform v6 deck", "old body",
             source=("Reference Materials/Infoblox_Platform_v6_Synthesized_CB.pptx",))
    assets = [{"id": "a1", "title": "Infoblox_Platform_v6_Synthesized_CB.pptx"}]
    action = plan_element(el, assets)
    assert action.mode == "redistill"
    assert action.asset_id == "a1"
    assert action.layer == "SourceMaterial"
    assert action.label == "Infoblox Platform v6 deck"


def test_plan_synthesis_element_marks_carry():
    el = _El("Decisions", "DECISION: two-track", "the decision body")
    action = plan_element(el, [])
    assert action.mode == "carry"
    assert action.body == "the decision body"   # verbatim
    assert action.layer == "Decisions"
    assert action.label == "DECISION: two-track"


def test_plan_source_backed_without_asset_match_carries():
    el = _El("SourceMaterial", "X", "body", source=("Missing.pptx",))
    action = plan_element(el, [{"id": "a1", "title": "Other.docx"}])
    assert action.mode == "carry"   # no confident source → carry over


def test_plan_carries_serves_cross_links():
    el = _El("Decisions", "D", "b", serves=("ibx-5153/deliverable/foundation-pp-doc",))
    action = plan_element(el, [])
    assert action.serves == ("ibx-5153/deliverable/foundation-pp-doc",)


def test_classify_source_backed_layers():
    # SourceMaterial/ClientFeedback/Research/Brief/Agreement are source-backed
    # (when the element actually has a source to re-distill from).
    for layer in ("SourceMaterial", "ClientFeedback", "Research", "Brief", "Agreement"):
        assert classify_element(layer, has_source=True) == "source-backed"


def test_classify_synthesis_layers():
    # Decisions/Synthesis/Retrospective/Timeline/Stakeholders are synthesis.
    for layer in ("Decisions", "Synthesis", "Retrospective", "Timeline", "Stakeholders"):
        assert classify_element(layer, has_source=False) == "synthesis"


def test_source_backed_layer_without_source_falls_back_to_carry():
    # A source-backed LAYER but no `source:` field → can't re-distill → carry over.
    assert classify_element("SourceMaterial", has_source=False) == "synthesis"


def test_source_basename_strips_dir_prefix():
    assert source_basename("Reference Materials/Infoblox_Platform_v6_Synthesized_CB.pptx") \
        == "Infoblox_Platform_v6_Synthesized_CB.pptx"
    assert source_basename("Our AI Story.docx") == "Our AI Story.docx"


def test_match_source_asset_by_basename():
    assets = [
        {"id": "a1", "title": "Infoblox_Platform_v6_Synthesized_CB.pptx"},
        {"id": "a2", "title": "Our AI Story.docx"},
    ]
    m = match_source_asset(("Reference Materials/Infoblox_Platform_v6_Synthesized_CB.pptx",), assets)
    assert m == {"id": "a1", "title": "Infoblox_Platform_v6_Synthesized_CB.pptx"}


def test_match_returns_none_when_no_basename_match():
    assert match_source_asset(("Unknown File.pdf",), [{"id": "a1", "title": "Other.docx"}]) is None


def test_match_skips_typed_rag_asset_dict_sources():
    assets = [{"id": "a9", "title": "Whatever.docx"}]
    m = match_source_asset(({"type": "rag_asset", "id": "a9"},), assets)
    assert m == {"id": "a9", "title": "Whatever.docx"}


def test_match_empty_sources_returns_none():
    assert match_source_asset((), [{"id": "a1", "title": "X.docx"}]) is None


# ---- load_legacy_elements ---------------------------------------------------

def test_load_legacy_elements_returns_only_legacy_layer_files(tmp_path: Path):
    spine = tmp_path / "spine"

    # A LEGACY element in a capitalized LAYER dir → should load.
    legacy = spine / "Decisions" / "two-track.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        "---\n"
        "id: ibx-5153/decisions/two-track\n"
        "project: ibx-5153\n"
        "layer: Decisions\n"
        'title: "Two-track AI story"\n'
        "status: active\n"
        "last_touched: 2026-05-27\n"
        "---\n"
        "The two-track decision body.\n",
        encoding="utf-8",
    )

    # A NEW-substance file in a lowercase phase dir → NOT a LAYER → skipped.
    new = spine / "phase-0" / "messaging.md"
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_text(
        "---\n"
        "est_item_id: 11111111-2222-3333-4444-555555555555\n"
        "est_item_kind: deliverable\n"
        "phase: phase-0\n"
        "binding: live\n"
        "---\n"
        "## v1 — 2026-06-19 · live\n\nmessaging substance\n",
        encoding="utf-8",
    )

    els = load_legacy_elements(tmp_path)

    assert len(els) == 1
    assert els[0].layer == "Decisions"
    assert els[0].title == "Two-track AI story"
    assert els[0].id == "ibx-5153/decisions/two-track"


# ---- redistill_body ---------------------------------------------------------

def test_redistill_body_calls_distiller_with_framing_and_source():
    captured = {}
    def fake_distiller(prompt):
        captured["prompt"] = prompt
        return "fresh distilled body"
    action = RecoveryAction(
        mode="redistill", layer="SourceMaterial", label="Infoblox v6 deck",
        body="", serves=(), asset_id="a1", source_basename="Infoblox_v6.pptx",
    )
    out = redistill_body(action, asset_text="...51-slide deck text about IQ...", distiller=fake_distiller)
    assert out == "fresh distilled body"
    assert "Infoblox v6 deck" in captured["prompt"]          # the framing/label
    assert "51-slide deck text about IQ" in captured["prompt"]  # the source text


def test_redistill_body_strips_distiller_output():
    action = RecoveryAction(mode="redistill", layer="Research", label="X", body="",
                            serves=(), asset_id="a1", source_basename="x.pdf")
    out = redistill_body(action, asset_text="src", distiller=lambda p: "  body with spaces  \n")
    assert out == "body with spaces"


# ---- recovered_rows ---------------------------------------------------------

def _action(layer, label, body="b", serves=()):
    return RecoveryAction(mode="carry", layer=layer, label=label, body=body, serves=tuple(serves))


def test_recovered_rows_maps_actions_to_authored_rows():
    actions = [_action("Decisions", "Two-track story", body="the decision", serves=("ibx-5153/deliverable/foundation-pp-doc",))]
    bodies = ["the decision"]
    rows = recovered_rows(actions, project_id="pid", project_code="ibx-5153",
                          bodies=bodies, now_iso="2026-06-19T00:00:00+00:00")
    assert len(rows) == 1
    r = rows[0]
    assert r["origin"] == "authored"
    assert r["placement"] == "context"
    assert r["layer"] == "Decisions"
    assert r["project_code"] == "ibx-5153"
    assert r["est_item_id"] == "_authored/two-track-story"
    assert r["body"] == "the decision"
    assert r["serves"] == ["ibx-5153/deliverable/foundation-pp-doc"]
    assert r["status"] == "live"
    assert r["version_label"] == "v1"


def test_recovered_rows_disambiguates_colliding_slugs():
    # two DIFFERENT elements whose labels slugify the same must get distinct ids
    actions = [_action("Decisions", "Two-track story"), _action("Synthesis", "Two-track story")]
    bodies = ["b1", "b2"]
    rows = recovered_rows(actions, project_id="pid", project_code="ibx-5153",
                          bodies=bodies, now_iso="2026-06-19T00:00:00+00:00")
    ids = {r["est_item_id"] for r in rows}
    assert len(ids) == 2   # NO collision — two distinct est_item_ids
    # the row ids are also distinct (no clobber on upsert)
    assert len({r["id"] for r in rows}) == 2


def test_recovered_rows_uses_provided_body_not_action_body():
    # the body passed in `bodies` (re-distilled) is used, not action.body
    actions = [_action("Research", "X", body="OLD")]
    rows = recovered_rows(actions, project_id="pid", project_code="ibx-5153",
                          bodies=["FRESH redistilled"], now_iso="2026-06-19T00:00:00+00:00")
    assert rows[0]["body"] == "FRESH redistilled"


# ---- recover() orchestrator -------------------------------------------------

NOW = "2026-06-19T00:00:00+00:00"


class _FakeTable:
    """Minimal Supabase-table stand-in: supports list_sources' read
    (`select().eq().eq().order().execute()` on rag_assets) and the upsert path."""

    def __init__(self, store, name):
        self.store, self.name = store, name
        self._op = None
        self._filters = []

    def select(self, cols):
        assert "*" not in cols, "never select('*')"
        self._op = "select"
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def order(self, *args, **kwargs):
        return self  # ordering is irrelevant to the fake's correctness

    def upsert(self, rows, on_conflict=None):
        self.store.setdefault("_upserts", []).append(list(rows))
        bucket = self.store.setdefault(self.name, [])
        for r in rows:
            match = next((x for x in bucket if x["id"] == r["id"]), None)
            if match is not None:
                match.update(r)
            else:
                bucket.append(dict(r))
        self._op = "upsert"
        return self

    def execute(self):
        if self._op == "upsert":
            return type("R", (), {"data": []})()
        rows = self.store.get(self.name, [])
        sel = [x for x in rows if all(x.get(c) == v for c, v in self._filters)]
        return type("R", (), {"data": sel})()


class _FakeClient:
    def __init__(self, assets=None, project_id="pid"):
        self.store = {}
        if assets is not None:
            # Default project_id onto seeded assets so list_sources'
            # .eq("project_id", ...) filter finds them (tests pass "pid").
            self.store["rag_assets"] = [
                {"project_id": project_id, **a} for a in assets
            ]

    def table(self, name):
        return _FakeTable(self.store, name)

    @property
    def upserts(self):
        return self.store.get("_upserts", [])


def _write_legacy(project_dir: Path, layer: str, name: str, *, title, body,
                  source=None, status="active", serves=()):
    d = project_dir / "spine" / layer
    d.mkdir(parents=True, exist_ok=True)
    fm = [
        "---",
        f"id: ibx-5153/{layer.lower()}/{name}",
        "project: ibx-5153",
        f"layer: {layer}",
        f'title: "{title}"',
        f"status: {status}",
        "last_touched: 2026-05-27",
    ]
    if source:
        fm.append("source:")
        for s in source:
            fm.append(f"  - {s}")
    if serves:
        fm.append("serves:")
        for s in serves:
            fm.append(f"  - {s}")
    fm.append("---")
    (d / f"{name}.md").write_text("\n".join(fm) + f"\n{body}\n", encoding="utf-8")


def _seed_two_elements(project_dir: Path):
    # A source-backed SourceMaterial element (has a `source:` matching a seeded asset).
    _write_legacy(
        project_dir, "SourceMaterial", "v6-deck",
        title="Infoblox Platform v6 deck", body="stale deck summary",
        source=("Reference Materials/Infoblox_Platform_v6.pptx",),
    )
    # A synthesis Decisions element (carries verbatim).
    _write_legacy(
        project_dir, "Decisions", "two-track",
        title="Two-track AI story", body="the durable decision body",
        serves=("ibx-5153/deliverable/foundation-pp-doc",),
    )


def test_recover_dry_run_returns_report_and_writes_nothing(tmp_path):
    assets = [{"id": "a1", "title": "Infoblox_Platform_v6.pptx",
               "source_type": "deck", "status": "active", "created_at": "2026-05-01"}]
    client = _FakeClient(assets)
    _seed_two_elements(tmp_path)

    report, rows = recover(
        client=client, project_id="pid", company_id="cid", project_dir=tmp_path,
        canonical_code="ibx-5153", now_iso=NOW,
        distiller=lambda p: "fresh", pull_text=lambda a: "deck text",
        apply=False,
    )

    assert client.upserts == []  # DRY RUN — nothing written
    modes = {r["label"]: r["mode"] for r in report}
    assert modes["Infoblox Platform v6 deck"] == "redistill"  # matched + distiller+pull
    assert modes["Two-track AI story"] == "carry"             # synthesis
    assert len(rows) == 2
    assert all(r["project_code"] == "ibx-5153" for r in rows)


def test_recover_apply_upserts_authored_rows(tmp_path):
    assets = [{"id": "a1", "title": "Infoblox_Platform_v6.pptx",
               "source_type": "deck", "status": "active", "created_at": "2026-05-01"}]
    client = _FakeClient(assets)
    _seed_two_elements(tmp_path)

    report, rows = recover(
        client=client, project_id="pid", company_id="cid", project_dir=tmp_path,
        canonical_code="ibx-5153", now_iso=NOW,
        distiller=lambda p: "fresh", pull_text=lambda a: "deck text",
        apply=True,
    )

    assert len(client.upserts) == 1  # one upsert call
    written = client.store["spine_substance"]
    assert len(written) == 2
    assert all(r["origin"] == "authored" for r in written)
    assert all(r["project_code"] == "ibx-5153" for r in written)


def test_recover_redistill_uses_pulled_asset_text(tmp_path):
    assets = [{"id": "a1", "title": "Infoblox_Platform_v6.pptx",
               "source_type": "deck", "status": "active", "created_at": "2026-05-01"}]
    client = _FakeClient(assets)
    _seed_two_elements(tmp_path)

    captured = {}

    def distiller(prompt):
        captured["prompt"] = prompt
        return "REDISTILLED BODY"

    report, rows = recover(
        client=client, project_id="pid", company_id="cid", project_dir=tmp_path,
        canonical_code="ibx-5153", now_iso=NOW,
        distiller=distiller, pull_text=lambda a: "fifty-one slide deck text",
        apply=False,
    )

    assert "fifty-one slide deck text" in captured["prompt"]
    redistilled = next(r for r in rows if r["framing"] == "Infoblox Platform v6 deck")
    assert redistilled["body"] == "REDISTILLED BODY"


def test_recover_filters_null_title_assets(tmp_path):
    assets = [
        {"id": "a1", "title": None, "source_type": "deck",
         "status": "active", "created_at": "2026-05-02"},
        {"id": "a2", "title": "Infoblox_Platform_v6.pptx", "source_type": "deck",
         "status": "active", "created_at": "2026-05-01"},
    ]
    client = _FakeClient(assets)
    _seed_two_elements(tmp_path)

    # Must not crash on the null-title row.
    report, rows = recover(
        client=client, project_id="pid", company_id="cid", project_dir=tmp_path,
        canonical_code="ibx-5153", now_iso=NOW,
        distiller=lambda p: "x", pull_text=lambda a: "t", apply=False,
    )
    assert len(rows) == 2


def test_recover_without_distiller_carries_everything(tmp_path):
    assets = [{"id": "a1", "title": "Infoblox_Platform_v6.pptx",
               "source_type": "deck", "status": "active", "created_at": "2026-05-01"}]
    client = _FakeClient(assets)
    _seed_two_elements(tmp_path)

    report, rows = recover(
        client=client, project_id="pid", company_id="cid", project_dir=tmp_path,
        canonical_code="ibx-5153", now_iso=NOW,
        distiller=None, pull_text=None, apply=False,
    )
    assert all(r["mode"] == "carry" for r in report)
    # the source-backed element falls back to its verbatim body
    deck = next(r for r in rows if r["framing"] == "Infoblox Platform v6 deck")
    assert deck["body"] == "stale deck summary"
