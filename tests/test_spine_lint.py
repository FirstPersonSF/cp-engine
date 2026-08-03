# tests/test_spine_lint.py — warn-only spine hygiene checks (#69)
from cp_engine.spine_lint import lint_cp_placeholders, lint_spine_rows


def _row(eid="w1", *, framing=None, layer="Note", binding="live",
         serves=("x",), important=False, body="b", sources=("s",)):
    return {"est_item_id": eid, "framing": framing or eid, "layer": layer,
            "binding": binding, "serves": list(serves),
            "important": important, "body": body, "sources": list(sources)}


def test_clean_spine_yields_nothing():
    rows = [_row(),
            _row("w2", important=True),                      # important + bound
            _row("w3", binding="unbound", serves=())]        # floating, not important
    assert lint_spine_rows(rows) == []


def test_important_unbound_serving_nothing_flags():
    row = _row("_authored/q", framing="Guiding question",
               important=True, binding="unbound", serves=())
    out = lint_spine_rows([row])
    assert len(out) == 1
    assert "important-but-floating" in out[0]
    assert "Guiding question" in out[0] and "_authored/q" in out[0]


def test_agreement_attach_instruction_without_source_flags():
    row = _row("_authored/sow", framing="SOW", layer="Agreement",
               body="Signed document: sow/final.md (attach as source)",
               sources=())
    out = lint_spine_rows([row])
    assert len(out) == 1
    assert "unexecuted attach-instruction" in out[0]
    # The verb moved to the hosted server (#143) — the hint names the connector
    # so the reader doesn't hunt for it on stdio.
    assert "add_element_source" in out[0]
    assert "cp-hosted" in out[0]


def test_agreement_with_source_or_without_instruction_is_quiet():
    with_source = _row(layer="Agreement", body="attach as source",
                       sources=({"type": "rag_asset", "id": "a1"},))
    no_instruction = _row(layer="Agreement", body="human terms", sources=())
    non_agreement = _row(layer="Note", body="attach as source", sources=())
    assert lint_spine_rows([with_source, no_instruction, non_agreement]) == []


def test_attach_instruction_phrasing_variants():
    for body in ("Attach as source", "attached as source",
                 "attach as a source"):
        row = _row(layer="Agreement", body=body, sources=())
        assert lint_spine_rows([row]), body


def test_one_row_can_flag_twice():
    row = _row(layer="Agreement", body="attach as source", sources=(),
               important=True, binding="unbound", serves=())
    assert len(lint_spine_rows([row])) == 2


def test_placeholders_collapse_to_one_warning():
    text = ("# cp\n"
            "- _<one real line about the project>_\n"
            "- real content\n"
            "- _<who owns what>_\n")
    out = lint_cp_placeholders(text)
    assert len(out) == 1
    assert "2 template bullet(s)" in out[0]
    assert "one real line about the project" in out[0]
    assert "+1 more" in out[0]


def test_no_placeholders_no_warning():
    assert lint_cp_placeholders("# cp\n- real line\n") == []
    assert lint_cp_placeholders("") == []


# ── spec-v04 lifecycle checks (#149) ─────────────────────────────────────


def _lrow(eid, **kw):
    base = {"est_item_id": eid, "framing": kw.pop("framing", eid),
            "layer": "Synthesis", "binding": "unbound", "serves": [],
            "important": False, "body": "", "sources": [],
            "version_date": "2026-07-01"}
    base.update(kw)
    return base


def _edge(kind, frm, to):
    return {"kind": kind, "from_item_id": frm, "to_item_id": to}


def test_lifecycle_clean_canon_and_seal():
    from cp_engine.spine_lint import lint_lifecycle
    rows = [_lrow("brief"), _lrow("a"), _lrow("b"), _lrow("sealed")]
    rels = [_edge("canon_of", "a", "brief"), _edge("canon_of", "b", "brief"),
            _edge("absorbed_by", "sealed", "deliv")]
    assert lint_lifecycle(rows, rels) == []


def test_lifecycle_canon_oversized():
    from cp_engine.spine_lint import lint_lifecycle
    rows = [_lrow(f"m{i}") for i in range(8)]
    rels = [_edge("canon_of", f"m{i}", "brief") for i in range(8)]
    out = lint_lifecycle(rows, rels)
    assert len(out) == 1 and "canon oversized" in out[0]


def test_lifecycle_absorbed_but_serving():
    from cp_engine.spine_lint import lint_lifecycle
    rows = [_lrow("sealed", binding="live", serves=["work-item"])]
    rels = [_edge("absorbed_by", "sealed", "deliv")]
    out = lint_lifecycle(rows, rels)
    assert len(out) == 1 and "absorbed-but-serving" in out[0]


def test_lifecycle_stale_canon_member_absorbed_and_superseded():
    from cp_engine.spine_lint import lint_lifecycle
    rows = [_lrow("old"), _lrow("gone"), _lrow("new")]
    rels = [_edge("canon_of", "old", "brief"),
            _edge("canon_of", "gone", "brief"),
            _edge("supersedes", "new", "old"),
            _edge("absorbed_by", "gone", "deliv")]
    out = lint_lifecycle(rows, rels)
    assert len(out) == 2
    assert all("stale canon member" in w for w in out)
