"""#314 — distill fidelity: a distilled body must come from its source.

The instance: ``_authored/carol-s-our-ai-story-narrative-prose`` v1 on
ibx-5153, written by ``cxp spine-recover`` 2026-06-19. Its body was a
people-first HR memo; the documents it cited are an infrastructure narrative
(DNS as control point). Nothing flagged it for seven weeks.

The texts below are SHORT paraphrases of both — enough vocabulary to carry the
shape (a common-word memo against a technical narrative) without copying
client material into the repo.
"""
from pathlib import Path

import pytest

from cp_engine import distill_fidelity as df
from cp_engine.spine_recover import recover

# A stand-in for Carol's actual "Our AI Story": an infrastructure narrative.
SOURCE = """
Our AI Story. DNS is the control point for every AI workload on the network.
Every agent, every model call and every data flow starts with a DNS query, so
the resolver sees AI traffic first. Infoblox turns that control point into
policy: discover shadow AI services, enforce security policy at the resolver,
and block data exfiltration before a connection opens. Chapter one, context:
AI expands the attack surface faster than security teams can inventory it.
Chapter two, solution: the DDI foundation — DNS, DHCP and IP address
management — gives a single source of network truth, and threat intelligence
on the resolver stops malicious domains. Chapter three, proof: customers cut
time to detect shadow AI from weeks to hours with DNS security analytics.
""" * 3

# The v1 body's shape: plausible, fluent, and drawn from none of the above.
FABRICATED = """-distillation
Our AI memo (board-approved). It says we will use AI to augment our team,
not replace them. Every AI deployment must show how it gives employees
better tools, not pink slips. Tone is reassuring and people-first. The board
approved this in March; leaders were very clear that AI is about making
work more humane. A twelve-agent customer service pilot will measure
employee satisfaction before any wider rollout across departments.
"""

# A faithful distillation of SOURCE: paraphrased, but its phrases are the
# source's phrases.
FAITHFUL = """
DNS is the control point for AI on the network: every agent and model call
begins with a DNS query, so the resolver sees AI traffic first. The story
runs in three chapters — AI expands the attack surface; the DDI foundation
(DNS, DHCP, IP address management) is the single source of network truth,
with threat intelligence on the resolver to block malicious domains and data
exfiltration; and proof that DNS security analytics cut time to detect
shadow AI from weeks to hours.
"""


# ---- the scorer ------------------------------------------------------------


def test_fabricated_body_is_low():
    """Judged on phrases, not words: against the real 175k-character sources
    the Carol v1 shares 74% of its single terms and 1.4% of its phrases."""
    a = df.assess(FABRICATED, SOURCE)
    assert a["low"] is True, a
    assert a["score"] < df.FIDELITY_THRESHOLD
    assert a["term_score"] > a["score"]
    assert "board" in a["unmatched"]


def test_faithful_distill_passes():
    a = df.assess(FAITHFUL, SOURCE)
    assert a["low"] is False, a
    assert a["score"] > df.FIDELITY_THRESHOLD * 4


def test_empty_source_is_always_low():
    """The distiller had nothing to be faithful TO."""
    a = df.assess(FAITHFUL, "   \n")
    assert a["low"] is True
    assert "empty source" in a["reason"]


def test_short_body_is_reported_not_flagged():
    a = df.assess("DNS control point.", SOURCE)
    assert a["low"] is False
    assert a["reason"] == "body too short to judge"


def test_threshold_is_the_calibrated_value():
    """Changing it is a decision, not a refactor — re-run the calibration
    (commit message of #314) before moving it."""
    assert df.FIDELITY_THRESHOLD == 0.05


# ---- the marker ------------------------------------------------------------


def test_mark_sets_field_state_and_flag_only_when_low():
    row = {"field_states": {"serves": "confirmed"}, "review_flags": []}
    df.mark_machine_derived(row, df.assess(FAITHFUL, SOURCE), now_iso="t")
    assert row["field_states"] == {"serves": "confirmed", "body": df.MACHINE_DERIVED}
    assert row["review_flags"] == []

    df.mark_machine_derived(row, df.assess(FABRICATED, SOURCE),
                            source_label="Our AI Story.docx", now_iso="t")
    (flag,) = row["review_flags"]
    assert flag["field"] == "body" and flag["source"] == df.FLAG_SOURCE
    assert flag["was"] == "Our AI Story.docx"
    # Re-marking replaces this producer's flag, never stacks it.
    df.mark_machine_derived(row, df.assess(FABRICATED, SOURCE), now_iso="t2")
    assert len(row["review_flags"]) == 1


def test_provenance_clears_when_a_human_confirms_the_body():
    """Every human write path stamps body:'confirmed' — that IS the clear."""
    row = df.mark_machine_derived({})
    assert df.provenance_of(row) == df.MACHINE_DERIVED_LABEL
    row["field_states"]["body"] = "confirmed"
    assert df.provenance_of(row) is None


def test_disk_distilled_rows_read_as_machine_derived_until_confirmed():
    assert df.provenance_of({"origin": "distilled",
                             "field_states": {"body": "proposed"}}) \
        == df.MACHINE_DERIVED_LABEL
    assert df.provenance_of({"origin": "distilled",
                             "field_states": {"body": "confirmed"}}) is None
    # A person's authored body is not machine-derived.
    assert df.provenance_of({"origin": "authored", "field_states": {}}) is None


def test_body_head_skips_blanks_and_caps_width():
    assert df.body_head("\n\n-distillation\nOur AI memo\n\nthird\nfourth") == [
        "-distillation", "Our AI memo", "third"]
    assert len(df.body_head("x" * 500)[0]) == 200


# ---- recover: the path that wrote Carol v1 --------------------------------

NOW = "2026-06-19T00:00:00+00:00"


class _T:
    def __init__(self, store, name):
        self.store, self.name, self.f, self.op = store, name, [], "select"

    def select(self, cols):
        assert "*" not in cols
        return self

    def eq(self, c, v):
        self.f.append((c, v))
        return self

    def order(self, *a, **k):
        return self

    def upsert(self, rows, on_conflict=None):
        self.op = "upsert"
        self.store.setdefault(self.name, []).extend(dict(r) for r in rows)
        return self

    def execute(self):
        if self.op == "upsert":
            return type("R", (), {"data": []})()
        rows = self.store.get(self.name, [])
        return type("R", (), {"data": [r for r in rows
                                       if all(r.get(c) == v for c, v in self.f)]})()


class _C:
    def __init__(self):
        self.store = {"rag_assets": [{
            "id": "a1", "project_id": "pid", "title": "Our AI Story.docx",
            "source_type": "doc", "status": "active", "created_at": "2026-06-12"}]}

    def table(self, name):
        return _T(self.store, name)


def _legacy(project_dir: Path):
    d = project_dir / "spine" / "SourceMaterial"
    d.mkdir(parents=True)
    (d / "carol-our-ai-story.md").write_text(
        "---\n"
        "id: ibx-5153/sourcematerial/carol-our-ai-story\n"
        "project: ibx-5153\nlayer: SourceMaterial\n"
        "title: \"Carol's 'Our AI Story' narrative prose\"\n"
        "status: active\nlast_touched: 2026-06-12\n"
        "source:\n  - Reference Materials/Our AI Story.docx\n"
        "---\nCarol's prose narrative of the three-chapter framework.\n",
        encoding="utf-8")


def _run(tmp_path, *, distiller, pull_text, apply=True):
    _legacy(tmp_path)
    client = _C()
    report, rows = recover(
        client=client, project_id="pid", company_id="cid", project_dir=tmp_path,
        canonical_code="ibx-5153", now_iso=NOW, distiller=distiller,
        pull_text=pull_text, apply=apply)
    return report, rows, client


def test_recover_never_distills_from_empty_source_text(tmp_path):
    """CONTROL: against the unfixed code the distiller is called with an empty
    source and its fabrication is written as the element's body."""
    calls = []

    def distiller(prompt):
        calls.append(prompt)
        return FABRICATED

    (r,), (row,), _ = _run(tmp_path, distiller=distiller, pull_text=lambda a: "")
    assert calls == [], "distilled from nothing"
    assert r["mode"] == "carry"
    assert row["body"].startswith("Carol's prose narrative")
    assert r["fidelity_low"] is True
    assert "empty source" in r["fidelity_reason"]


def test_recover_flags_a_fabricated_redistill_and_marks_it(tmp_path):
    """CONTROL: against the unfixed code the row carries no marker, no flag,
    and the report says nothing — the Carol v1 exactly."""
    (r,), (row,), client = _run(tmp_path, distiller=lambda p: FABRICATED,
                                pull_text=lambda a: SOURCE)
    assert r["mode"] == "redistill"
    assert r["fidelity_low"] is True
    assert (row.get("field_states") or {}).get("body") == df.MACHINE_DERIVED
    flags = df.fidelity_flags_of(row)
    assert len(flags) == 1 and flags[0]["was"] == "Our AI Story.docx"
    # …and it is what was WRITTEN, not only what was returned.
    (written,) = client.store["spine_substance"]
    assert df.provenance_of(written) == df.MACHINE_DERIVED_LABEL
    assert df.fidelity_flags_of(written)


def test_recover_marks_a_faithful_redistill_without_flagging_it(tmp_path):
    (r,), (row,), _ = _run(tmp_path, distiller=lambda p: FAITHFUL,
                           pull_text=lambda a: SOURCE)
    assert r["fidelity_low"] is False and r["fidelity"] > df.FIDELITY_THRESHOLD
    assert row["field_states"]["body"] == df.MACHINE_DERIVED
    assert df.fidelity_flags_of(row) == []


def test_recover_leaves_carried_bodies_unmarked(tmp_path):
    (r,), (row,), _ = _run(tmp_path, distiller=None, pull_text=None)
    assert r["mode"] == "carry"
    assert df.provenance_of(row) is None


# ---- promote: the frame/promote distiller ---------------------------------


def _card():
    from cp_engine.spine_inbox import proposed_card
    return proposed_card(project_id="u1", project_code="ibx-5153",
                         source_ref="mtg-42", raw_distillation=SOURCE,
                         guessed_est_item_id="d1", guessed_type="deliverable")


def _distiller(body):
    return lambda prompt, *, model, api_key=None: body


def test_promote_reports_fidelity_before_writing(tmp_path):
    from cp_engine.spine_inbox import promote_card
    from tests._spine_fake import FakeClient
    seen = {}
    client = FakeClient(spine_substance=[], spine_steps=[], spine_inbox=[])
    client.fail["upsert"] = {"spine_substance"}     # the write never lands…
    with pytest.raises(RuntimeError):
        promote_card(_card(), framing="the DNS story", est_item_id="d1",
                     kind="deliverable", project_dir=tmp_path, sources=["mtg-42"],
                     distiller=_distiller(FABRICATED), model="m", client=client,
                     today="2026-06-20", on_fidelity=seen.update)
    assert seen["low"] is True                       # …the score was already out


def test_promote_create_path_stamps_the_authored_row(tmp_path, monkeypatch):
    """The #44 create path writes an AUTHORED row straight to MC-2 — the one
    promote shape sync never touches, so it must carry its own marker."""
    from cp_engine import mc2_db
    from cp_engine.spine_inbox import promote_card
    monkeypatch.setattr(mc2_db, "canonical_spine_code",
                        lambda client, pid, fallback: fallback)
    from tests._spine_fake import FakeClient
    client = FakeClient(spine_substance=[], spine_steps=[], spine_inbox=[])
    promote_card(_card(), framing="Planning", est_item_id="d1", kind="activity",
                 project_dir=tmp_path, sources=["mtg-1"], client=client,
                 distiller=_distiller(FAITHFUL), model="m", today="2026-06-20")
    promote_card(_card(), framing="Interview with Paul Wu", est_item_id="d1",
                 kind="activity", project_dir=tmp_path, sources=["mtg-2"],
                 distiller=_distiller(FABRICATED), model="m", client=client,
                 today="2026-07-09")
    (row,) = [r for r in client.store["spine_substance"] if r["origin"] == "authored"]
    assert row["field_states"]["body"] == df.MACHINE_DERIVED
    assert df.fidelity_flags_of(row)


# ---- the read side ---------------------------------------------------------


def test_pull_spine_surfaces_the_marker():
    from cp_engine.project_sources import _spine_element
    marked = df.mark_machine_derived({"est_item_id": "_authored/x"},
                                     df.assess(FABRICATED, SOURCE), now_iso="t")
    out = _spine_element(marked)
    assert out["provenance"] == df.MACHINE_DERIVED_LABEL
    assert out["fidelity_flags"]
    plain = _spine_element({"est_item_id": "_authored/y", "origin": "authored"})
    assert "provenance" not in plain and "fidelity_flags" not in plain


def test_pull_columns_select_the_marker():
    from cp_engine import mc2_db
    for col in ("origin", "field_states", "review_flags"):
        assert col in mc2_db.SPINE_PULL_COLUMNS
